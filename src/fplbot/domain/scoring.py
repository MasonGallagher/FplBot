"""The scoring model - expected points as a distribution.

    xP = P(appears) x [ xP_attacking + xP_defensive + xP_bonus + xP_defcon ]
         + xP_appearance

STATUS: PROVISIONAL, and deliberately so. SPEC section 5 says every coefficient
here is a placeholder pending a fit against historical gameweeks (SPEC 5.5), and
that is why they all live in one `ModelTunables` object rather than being
scattered through these functions. Replacing hand-picked priors with fitted
values should be a one-object change.

HOW IT WORKS
------------
Rather than composing expectations analytically, we run a **Monte Carlo**. For
each player we draw `N` samples; each sample walks the whole causal chain:

    minutes bucket -> minutes -> goals, assists, goals conceded, defensive
    actions, cards -> points

The reason is SPEC section 5.4. A rank-attacking objective needs the *shape* of
the outcome distribution, not its mean, and the shape here is a mixture: the
points distribution conditional on "started" looks nothing like the one
conditional on "came on for the last twenty minutes". There is no tidy closed
form, and there is no need for one - four thousand samples per player is a few
milliseconds in numpy.

Sampling also gets the correlations right for free. Goals conceded and the clean
sheet are the same underlying event, so drawing conceded goals once and deriving
the clean sheet from it makes them consistent by construction. Doing this
analytically means remembering to handle that dependency by hand, every time.

THE V2 SEAM
-----------
`score_player` is pure: same inputs, same distribution, no I/O and no clock. A
future squad-aware ILP optimiser (SPEC 6.4) consumes these distributions
unchanged - which is the whole reason ranking lives in a separate module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from fplbot.config import (
    FALLBACK_CLEAN_SHEET_POINTS,
    FALLBACK_GOAL_POINTS,
    POINTS_ASSIST,
    ModelTunables,
)
from fplbot.domain.fixtures import fixture_difficulty_multiplier
from fplbot.domain.minutes import MinutesDistribution
from fplbot.models.domain import (
    AvailabilitySignal,
    Distribution,
    FixtureContext,
    PlayerScore,
    TeamGameweek,
)
from fplbot.models.fpl import Element, ScoringRules

# League-average goals per team per match. Used to convert an absolute expected
# team-goals figure into a multiplier on a player's baseline rate.
LEAGUE_AVERAGE_TEAM_GOALS = 1.42

# Rough per-match yellow card rate for a starting outfield player.
YELLOW_CARD_RATE = 0.12

# Baseline saves per match for a starting goalkeeper, scaled by how much the
# team is expected to concede.
BASELINE_KEEPER_SAVES = 3.1


def _by_position(rule: dict[str, int] | int | None, position: str) -> int | None:
    """Read a ScoringRules value that may be per-position or a single flat int.

    FPL has been known to collapse a per-position stat to one flat value (bonus
    and saves always were; assists joined them on 2026-07-28). A flat value
    applies to every position equally.
    """
    if isinstance(rule, dict):
        return rule.get(position)
    return rule


@dataclass
class ScoringContext:
    """Everything the scorer needs that is not the player or the fixture.

    Assembled once per run. Holding it as an explicit object rather than reaching
    for globals is what keeps `score_player` pure and therefore testable without
    any mocking at all.
    """

    element_types: dict[int, str]
    scoring_rules: ScoringRules
    season_has_started: bool
    tunables: ModelTunables
    rng: np.random.Generator

    # Optional enrichments. Each is None-safe: the model degrades to a coarser
    # estimate rather than failing when a source is unavailable.
    xg90_by_element: dict[int, float] = field(default_factory=dict)
    xa90_by_element: dict[int, float] = field(default_factory=dict)
    defcon90_by_element: dict[int, float] = field(default_factory=dict)
    goalscorer_probability: dict[int, float] = field(default_factory=dict)
    team_games_played: dict[int, int] = field(default_factory=dict)
    top_bps_elements: set[int] = field(default_factory=set)

    def goal_points(self, position: str) -> int:
        return _by_position(self.scoring_rules.goals_scored, position) or FALLBACK_GOAL_POINTS.get(
            position, 4
        )

    def clean_sheet_points(self, position: str) -> int:
        return _by_position(
            self.scoring_rules.clean_sheets, position
        ) or FALLBACK_CLEAN_SHEET_POINTS.get(position, 0)

    def assist_points(self, position: str) -> int:
        return _by_position(self.scoring_rules.assists, position) or POINTS_ASSIST

    def defcon_points(self, position: str) -> int:
        return _by_position(self.scoring_rules.defensive_contribution, position) or 0


# ---------------------------------------------------------------------------
# Shrinkage
# ---------------------------------------------------------------------------
def shrink_per_90(
    raw_per_90: float | None,
    minutes_played: int,
    prior: float,
    prior_minutes: float,
) -> float:
    """Empirical-Bayes shrinkage of a per-90 rate towards a positional prior.

    Small samples are the dominant early-season failure mode. A player with 90
    minutes and 1.0 xG is **not** a 1.0 xG/90 player - he is a player about whom
    we know almost nothing, whose best estimate is barely distinguishable from
    the positional average.

    The weight is expressed in "equivalent minutes":

        w = minutes / (minutes + prior_minutes)
        estimate = w * observed + (1 - w) * prior

    With `prior_minutes = 450` (five full matches), a player with 450 minutes gets
    a 50/50 blend and a player with 90 minutes gets 17% weight on his own record.
    That is the intended behaviour: it takes real evidence to move away from the
    prior, and one hot afternoon is not real evidence.
    """
    if raw_per_90 is None or minutes_played <= 0:
        return prior
    weight = minutes_played / (minutes_played + prior_minutes)
    return weight * raw_per_90 + (1 - weight) * prior


def attacking_rates(
    element: Element,
    position: str,
    context: ScoringContext,
) -> tuple[float, float]:
    """Shrunk (xG/90, xA/90) for a player.

    Source preference: Understat's non-penalty xG first (penalties are modelled
    separately via `penalties_order`, so counting them in the base rate would
    double-count designated takers), then FPL's own Opta-sourced per-90s, then
    the positional prior alone.
    """
    prior_xg = context.tunables.prior_xg90.get(position, 0.1)
    prior_xa = context.tunables.prior_xa90.get(position, 0.1)

    if not context.season_has_started:
        # Pre-season. The counters (`total_points`, `event_points`, transfers)
        # have been reset while `minutes` still holds LAST season's total, so
        # anything cumulative is untrustworthy here.
        #
        # The per-90 RATES are the exception, and treating them like the counters
        # was throwing away the only player-specific signal available for the GW1
        # board. A rate is not season-contaminated the way a total is: FPL
        # reports 0.78 xG/90 for Haaland, and replacing that with the average
        # forward's 0.35 does not make the model more careful, it makes it blind.
        #
        # What genuinely cannot be trusted is `minutes` as the shrinkage WEIGHT -
        # a full season of it would trust last year's form as though it were
        # this year's. So the weight is capped, which does two jobs at once:
        # it discounts cross-season evidence for everyone, and it still shrinks
        # a small sample hard. That second part is not hypothetical - one
        # midfielder currently shows 3.60 xG/90 off a handful of minutes, and an
        # uncapped rate would promote him to the top of the board.
        capped_minutes = int(min(element.minutes, context.tunables.preseason_equivalent_minutes))
        return (
            shrink_per_90(
                element.xg_per_90 or 0.0,
                capped_minutes,
                prior_xg,
                context.tunables.shrinkage_prior_minutes,
            ),
            shrink_per_90(
                element.xa_per_90 or 0.0,
                capped_minutes,
                prior_xa,
                context.tunables.shrinkage_prior_minutes,
            ),
        )

    xg90 = context.xg90_by_element.get(element.id, element.xg_per_90)
    xa90 = context.xa90_by_element.get(element.id, element.xa_per_90)

    return (
        shrink_per_90(xg90, element.minutes, prior_xg, context.tunables.shrinkage_prior_minutes),
        shrink_per_90(xa90, element.minutes, prior_xa, context.tunables.shrinkage_prior_minutes),
    )


def set_piece_bonus(element: Element, context: ScoringContext) -> tuple[float, float]:
    """Additional xG and xA from set-piece and penalty duty.

    `penalties_order == 1` is worth a large, separate bump - a first-choice taker
    at a side that wins penalties picks up roughly 0.12 xG per match, which is
    more than most defenders' entire open-play rate.

    Use the `*_order` integers. The matching `*_text` fields are empty strings for
    every player, so anyone reaching for them gets nothing and may not notice.
    """
    xg_bonus = 0.0
    xa_bonus = 0.0

    if element.penalties_order == 1:
        xg_bonus += context.tunables.penalty_taker_xg_bonus
    elif element.penalties_order == 2:
        xg_bonus += context.tunables.penalty_second_taker_xg_bonus

    if element.direct_freekicks_order == 1:
        xg_bonus += context.tunables.direct_freekick_xg_bonus
    if element.corners_and_indirect_freekicks_order == 1:
        xa_bonus += context.tunables.corner_taker_xa_bonus

    return xg_bonus, xa_bonus


# ---------------------------------------------------------------------------
# The per-fixture sampler
# ---------------------------------------------------------------------------
def sample_fixture_points(
    element: Element,
    position: str,
    fixture: FixtureContext,
    minutes_dist: MinutesDistribution,
    context: ScoringContext,
    *,
    samples: int,
    fixture_index: int = 0,
) -> tuple[np.ndarray, dict[str, float]]:
    """Draw `samples` point outcomes for one player in one fixture.

    Returns the sample array and a component breakdown of the means, which is
    what makes the report able to say *why* rather than merely *what*.
    """
    rng = context.rng
    tunables = context.tunables

    # -- 1. Minutes -------------------------------------------------------
    probabilities = np.array(minutes_dist.as_tuple(), dtype=np.float64)

    if fixture_index > 0:
        # A double gameweek is not two independent starts. Managers rotate across
        # a congested week, so the second fixture carries materially more
        # rotation risk than the first. Shifting 15% of the starter mass into
        # rotation is a prior, not a measurement - it is flagged in DECISIONS.md
        # as a candidate for fitting.
        shift = probabilities[0] * 0.15
        probabilities = probabilities.copy()
        probabilities[0] -= shift
        probabilities[1] += shift

    probabilities = probabilities / probabilities.sum()
    buckets = rng.choice(4, size=samples, p=probabilities)

    bucket_means = np.array(
        [
            tunables.minutes_by_bucket["starter"],
            tunables.minutes_by_bucket["rotation"],
            tunables.minutes_by_bucket["cameo"],
            tunables.minutes_by_bucket["out"],
        ]
    )
    bucket_spread = np.array([9.0, 20.0, 11.0, 0.0])

    minutes = bucket_means[buckets] + rng.normal(0, 1, samples) * bucket_spread[buckets]
    minutes = np.clip(minutes, 0, 90)
    minutes[buckets == 3] = 0.0

    played = minutes > 0
    played_sixty = minutes >= 60
    minutes_share = minutes / 90.0

    # -- 2. Appearance points ---------------------------------------------
    appearance = np.where(played_sixty, 2, np.where(played, 1, 0)).astype(np.float64)

    # -- 3. Attacking ------------------------------------------------------
    xg90, xa90 = attacking_rates(element, position, context)
    xg_set_piece, xa_set_piece = set_piece_bonus(element, context)

    strength = _attacking_multiplier(fixture)

    # Set-piece xG is per *match* rather than per 90 of open play, but it still
    # requires being on the pitch, so it scales with minutes like everything else.
    lambda_goals = np.maximum(0.0, (xg90 * strength + xg_set_piece) * minutes_share)
    lambda_assists = np.maximum(0.0, (xa90 * strength + xa_set_piece) * minutes_share)

    # If a bookmaker has priced this player's anytime-goalscorer market, that is a
    # sharper estimate than our xG chain: it is a liquid market's view, already
    # devigged with the power method. We calibrate lambda so that P(>=1 goal)
    # matches the market, i.e. lambda = -ln(1 - p).
    market_probability = context.goalscorer_probability.get(element.id)
    if market_probability is not None and 0 < market_probability < 0.95:
        market_lambda = -math.log(1 - market_probability)
        # Blend rather than replace: the market prices a full 90 minutes, whereas
        # our minutes model knows about rotation. 70/30 towards the market on the
        # scale, with our minutes share still applied.
        lambda_goals = 0.7 * market_lambda * minutes_share + 0.3 * lambda_goals

    goals = rng.poisson(lambda_goals)
    assists = rng.poisson(lambda_assists)

    goal_points = goals * context.goal_points(position)
    assist_points = assists * context.assist_points(position)

    # -- 4. Clean sheets and goals conceded --------------------------------
    # Drawn as ONE event so they cannot contradict each other. We calibrate the
    # Poisson rate so that P(concede 0) equals ClubElo's clean-sheet probability
    # exactly:  P(0) = e^-lambda  =>  lambda = -ln(P(0)).
    # That keeps a real model's headline number intact while giving us a
    # consistent distribution over the rest of the scorelines.
    if fixture.clean_sheet_probability is not None:
        cs_probability = min(0.95, max(0.02, fixture.clean_sheet_probability))
        lambda_conceded = -math.log(cs_probability)
    elif fixture.expected_goals_conceded is not None:
        lambda_conceded = max(0.1, fixture.expected_goals_conceded)
    else:
        lambda_conceded = _conceded_from_difficulty(fixture)

    conceded = rng.poisson(lambda_conceded, size=samples)

    clean_sheet = played_sixty & (conceded == 0)
    clean_sheet_points = clean_sheet * context.clean_sheet_points(position)

    # -1 per two goals conceded, for goalkeepers and defenders only.
    if position in {"GKP", "DEF"}:
        conceded_penalty = np.where(played_sixty, -(conceded // 2), 0)
    else:
        conceded_penalty = np.zeros(samples)

    # -- 5. Goalkeeper saves -----------------------------------------------
    if position == "GKP":
        # A keeper behind a leaky defence faces more shots, so saves scale with
        # expected goals conceded rather than being a flat rate.
        save_rate = BASELINE_KEEPER_SAVES * (lambda_conceded / LEAGUE_AVERAGE_TEAM_GOALS)
        saves = rng.poisson(np.maximum(0.2, save_rate) * minutes_share)
        save_points = saves // 3  # 1 point per 3 saves
    else:
        save_points = np.zeros(samples)

    # -- 6. Defensive contribution -----------------------------------------
    defcon_points = _sample_defcon(element, position, minutes_share, context, samples)

    # -- 7. Bonus ----------------------------------------------------------
    bonus_points = _sample_bonus(element, position, goals, assists, clean_sheet, context, samples)

    # -- 8. Cards ----------------------------------------------------------
    if position != "GKP":
        yellows = (rng.random(samples) < YELLOW_CARD_RATE * minutes_share).astype(np.float64)
        card_points = -yellows
    else:
        card_points = np.zeros(samples)

    total = (
        appearance
        + goal_points
        + assist_points
        + clean_sheet_points
        + conceded_penalty
        + save_points
        + defcon_points
        + bonus_points
        + card_points
    )

    components = {
        "appearance": float(np.mean(appearance)),
        "goals": float(np.mean(goal_points)),
        "assists": float(np.mean(assist_points)),
        "clean_sheet": float(np.mean(clean_sheet_points)),
        "conceded": float(np.mean(conceded_penalty)),
        "saves": float(np.mean(save_points)),
        "defcon": float(np.mean(defcon_points)),
        "bonus": float(np.mean(bonus_points)),
        "cards": float(np.mean(card_points)),
        "expected_minutes": float(np.mean(minutes)),
    }

    return total.astype(np.float64), components


def _attacking_multiplier(fixture: FixtureContext) -> float:
    """How much this fixture scales a player's baseline attacking rate.

    Preference order matters. A real scoreline model beats a 1-5 difficulty
    scale, and both beat nothing:

    1. **Expected team goals** from ClubElo's scoreline distribution or from
       devigged odds. A ratio against the league average is directly meaningful.
    2. **FPL's `team_h_difficulty` / `team_a_difficulty`.** Populated and on a
       clean 1-5 scale.

    Note what is NOT in this list: `teams[].strength_attack_*`. Those are zero for
    all twenty teams, so a model built on them would rate every fixture
    identically while appearing to work perfectly.
    """
    if fixture.expected_team_goals is not None and fixture.expected_team_goals > 0:
        ratio = fixture.expected_team_goals / LEAGUE_AVERAGE_TEAM_GOALS
        # Bound it: a 4.0-goal expectation should not quadruple a player's rate,
        # because a rout distributes goals across a squad rather than multiplying
        # one player's involvement.
        return float(np.clip(ratio, 0.55, 1.9))
    return fixture_difficulty_multiplier(fixture.difficulty, fixture.is_home)


def _conceded_from_difficulty(fixture: FixtureContext) -> float:
    """Fallback expected goals conceded when no probability model is available."""
    by_difficulty = {1: 0.85, 2: 1.10, 3: 1.40, 4: 1.75, 5: 2.10}
    base = by_difficulty.get(fixture.difficulty, 1.40)
    return base * (0.92 if fixture.is_home else 1.08)


def _sample_defcon(
    element: Element,
    position: str,
    minutes_share: np.ndarray,
    context: ScoringContext,
    samples: int,
) -> np.ndarray:
    """Defensive contribution points.

    THE KEY INSIGHT (SPEC 5.2): model **P(hitting the threshold)**, not the mean
    rate. A player averaging 11 actions with high variance and a player steady at
    11 have very different hit rates against a threshold of 10 or 12 - the
    volatile one clears 12 far more often, and under a threshold rule that is all
    that matters. Scoring on the mean would rate them identically.

    Two caveats worth keeping in view:

    * The **thresholds are UNVERIFIED** - DEF 10, MID/FWD 12 is community
      consensus, not API-sourced. They live in `ModelTunables` with that comment.
    * Actions are **over-dispersed** relative to Poisson. A side under sustained
      pressure racks up clearances in clusters, so a pure Poisson understates the
      tail and therefore the hit rate. We use a gamma-Poisson mixture (a negative
      binomial) to widen it.
    """
    points_available = context.defcon_points(position)
    threshold = context.tunables.defcon_threshold_by_position.get(position, 999)
    if points_available <= 0 or threshold >= 999:
        return np.zeros(samples)

    rate90 = context.defcon90_by_element.get(element.id)
    if rate90 is None:
        if context.season_has_started and element.minutes > 0:
            rate90 = element.defensive_contribution_per_90 or (
                element.defensive_contribution * 90.0 / element.minutes
            )
        else:
            # No in-season sample and no archive prior. All five DefCon fields in
            # the live API are currently zero for everyone, so this is the common
            # case in GW1-5.
            rate90 = {"DEF": 8.5, "MID": 7.0, "FWD": 3.0}.get(position, 0.0)

    if rate90 <= 0:
        return np.zeros(samples)

    expected = rate90 * minutes_share

    # Gamma-Poisson mixture. Shape k controls the over-dispersion: variance
    # becomes lambda + lambda^2 / k, so smaller k means a fatter tail.
    overdispersion = max(1.01, context.tunables.defcon_overdispersion)
    k = 1.0 / (overdispersion - 1.0)
    gamma_noise = context.rng.gamma(shape=k, scale=1.0 / k, size=samples)
    actions = context.rng.poisson(np.maximum(0.0, expected * gamma_noise))

    return (actions >= threshold) * points_available


def _sample_bonus(
    element: Element,
    position: str,
    goals: np.ndarray,
    assists: np.ndarray,
    clean_sheet: np.ndarray,
    context: ScoringContext,
    samples: int,
) -> np.ndarray:
    """Bonus points, modelled only for the players where it is predictable.

    SPEC 5.2: BPS is predictable enough to be worth modelling for the top ~50
    players and is essentially noise below that. Modelling it for everyone would
    add variance without adding information, and would systematically flatter
    fringe players who occasionally top a low-BPS match.

    Conditional on returns rather than free-standing, because that is what
    actually drives BPS: goals and assists dominate the tally, and a defender's
    clean sheet plus a high action count is the other common route.
    """
    if element.id not in context.top_bps_elements:
        return np.zeros(samples)

    involvement = goals + assists
    rng = context.rng
    draw = rng.random(samples)

    bonus = np.zeros(samples)
    # Two or more returns: very likely to take maximum bonus.
    two_plus = involvement >= 2
    bonus[two_plus & (draw < 0.62)] = 3
    bonus[two_plus & (draw >= 0.62) & (draw < 0.85)] = 2
    bonus[two_plus & (draw >= 0.85) & (draw < 0.95)] = 1

    # Exactly one return: a real chance, far from a certainty.
    one = involvement == 1
    bonus[one & (draw < 0.18)] = 3
    bonus[one & (draw >= 0.18) & (draw < 0.38)] = 2
    bonus[one & (draw >= 0.38) & (draw < 0.60)] = 1

    # Defensive route: clean sheet, no attacking return.
    if position in {"GKP", "DEF"}:
        defensive = (involvement == 0) & clean_sheet
        bonus[defensive & (draw < 0.10)] = 3
        bonus[defensive & (draw >= 0.10) & (draw < 0.24)] = 2
        bonus[defensive & (draw >= 0.24) & (draw < 0.42)] = 1

    return bonus


# ---------------------------------------------------------------------------
# The public entry point - pure, and the v2 seam
# ---------------------------------------------------------------------------
def score_player(
    element: Element,
    team_gameweek: TeamGameweek,
    minutes_dist: MinutesDistribution,
    availability: AvailabilitySignal,
    context: ScoringContext,
) -> tuple[Distribution, dict[str, float]]:
    """Score one player for one gameweek. Pure.

    Returns the points distribution and the mean component breakdown.

    Blanks return a degenerate distribution at zero - a player whose team does
    not play scores nothing, and that is a certainty rather than an expectation.
    Doubles **sum across both fixtures**, with independent draws per fixture and
    extra rotation risk on the second.

    This function does no I/O, reads no clock and touches no globals, which is
    exactly what makes it the seam a v2 squad optimiser can build on (SPEC 6.4).
    """
    samples = context.tunables.monte_carlo_samples
    position = context.element_types.get(element.element_type, "MID")

    # Blank gameweek: zero, with certainty.
    if not team_gameweek.fixtures:
        return Distribution.constant(0.0, samples), {"blank": 1.0}

    totals = np.zeros(samples, dtype=np.float64)
    components: dict[str, float] = {}

    for index, fixture in enumerate(team_gameweek.fixtures):
        fixture_samples, fixture_components = sample_fixture_points(
            element,
            position,
            fixture,
            minutes_dist,
            context,
            samples=samples,
            fixture_index=index,
        )
        totals += fixture_samples
        for key, value in fixture_components.items():
            components[key] = components.get(key, 0.0) + value

    # Availability risk multiplies into expected points. Applied as a Bernoulli
    # gate on the whole gameweek rather than as a scaling of the mean, because
    # scaling the mean would quietly shrink the ceiling too - and the ceiling is
    # precisely what a rank-attacking objective is buying.
    if availability.risk > 0:
        plays = context.rng.random(samples) >= availability.risk
        totals = totals * plays
        components["availability_risk"] = availability.risk

    return Distribution(samples=totals), components


def build_player_score(
    element: Element,
    team_short: str,
    team_gameweek: TeamGameweek,
    minutes_dist: MinutesDistribution,
    availability: AvailabilitySignal,
    context: ScoringContext,
    opponent_names: list[str],
) -> PlayerScore:
    """Wrap `score_player` into the richer object the report consumes."""
    distribution, components = score_player(
        element, team_gameweek, minutes_dist, availability, context
    )
    position = context.element_types.get(element.element_type, "MID")

    return PlayerScore(
        element_id=element.id,
        name=element.display_name(),
        team_short=team_short,
        team_id=element.team,
        position=position,
        price=element.price,
        ownership=element.ownership,
        distribution=distribution,
        availability=availability,
        fixture_kind=team_gameweek.kind,
        fixture_count=team_gameweek.count,
        components={k: round(v, 3) for k, v in components.items()},
        opponents=opponent_names,
        ep_next=element.ep_next_value,
    )
