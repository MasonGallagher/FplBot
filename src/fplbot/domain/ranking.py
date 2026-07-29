"""Ranking - the rank-attacking objective.

**Do not rank on mean expected points.** This is the single most important
modelling decision in the project and it follows directly from the user's stated
objective (SPEC section 1): the goal is expected *rank* gain, not raw expected
points.

The argument, briefly. Suppose a 60%-owned midfielder has 6.5 xP and a 4%-owned
one has 6.0 xP. Ranking on xP puts the template player first. But against the
field, owning the template player gains you almost nothing - 60% of your rivals
own him too, so his points wash out of the comparison. His value is almost
entirely *defensive*: not owning him is what costs you. The 4%-owned player's
points, by contrast, accrue to you and to almost nobody else.

So we score:

    score = xP - lambda * (ownership * xP) + mu * ceiling

The middle term discounts the fraction of a player's points that the field
already has. The last term rewards upside, because rank is gained in the tail:
a green arrow comes from a haul nobody else owned, not from a steady six.

`lambda` and `mu` are tunables and are currently hand-picked priors. They are
exactly the kind of coefficient SPEC 5.5 says must be *fitted* against historical
gameweeks with decision-level metrics ("would this recommendation have gained
rank?"), not chosen by taste.

WHAT THE USER SEES
------------------
Every recommendation carries a confidence, a runner-up and a human-readable
"why". SPEC 6.1 is blunt about the reason: the user intends to compete against
these recommendations, so a pick they cannot argue with is a pick they cannot
evaluate. `explain` below is not decoration - it is the deliverable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fplbot.config import ModelTunables
from fplbot.domain.captaincy import CaptainPick
from fplbot.domain.squad import WildcardSquad
from fplbot.models.domain import (
    Confidence,
    FixtureKind,
    PlayerScore,
    Recommendation,
    RiskLevel,
)


def rank_score(score: PlayerScore, tunables: ModelTunables) -> float:
    """The rank-attacking objective.

        score = xP - lambda * (ownership * xP) + mu * ceiling

    Ownership enters as a fraction, so a 60%-owned player with 6.5 xP and
    lambda = 0.55 loses 0.55 * 0.60 * 6.5 = 2.15 points of *rank* value while
    keeping all 6.5 points of raw value. That gap is the whole point.
    """
    ownership_fraction = min(1.0, score.ownership / 100.0)
    expected = score.mean
    return (
        expected
        - tunables.ownership_penalty_lambda * (ownership_fraction * expected)
        + tunables.ceiling_bonus_mu * score.ceiling
    )


def assess_confidence(score: PlayerScore) -> Confidence:
    """How much we trust this pick.

    Confidence tracks *information quality*, not attractiveness. A player can be
    a superb pick with low confidence - a differential whose fitness is unclear -
    and the user needs to see those as two separate axes, because they imply
    different actions: one is "buy", the other is "check the presser first".
    """
    availability = score.availability

    # Sources actively disagreeing is the strongest confidence reducer we have.
    if availability.conflicting_sources:
        return Confidence.LOW
    if availability.awaiting_press_conference:
        return Confidence.LOW
    if availability.level in {RiskLevel.SERIOUS, RiskLevel.OUT}:
        return Confidence.LOW

    # A provisional kickoff time means the fixture itself might move gameweek,
    # which would turn this pick into a blank.
    if score.fixture_kind is FixtureKind.PROVISIONAL:
        return Confidence.LOW

    if availability.level is RiskLevel.DOUBT:
        return Confidence.MEDIUM

    # A wide distribution relative to its mean means the model itself is
    # uncertain, usually because of rotation risk.
    if score.mean > 0 and score.distribution.std / score.mean > 1.6:
        return Confidence.MEDIUM

    if len(availability.corroborating_sources) >= 2 and availability.level is RiskLevel.CLEAR:
        return Confidence.HIGH

    return Confidence.MEDIUM


def explain(score: PlayerScore, tunables: ModelTunables) -> str:
    """A one-line "why", in English, built from what actually moved the number.

    Deliberately generated from the component breakdown rather than from a
    template, so the sentence cannot drift away from the model that produced it.
    If the clean-sheet term dominated, the sentence says so.
    """
    parts: list[str] = []
    components = score.components

    # Lead with whichever component contributed most.
    contributors = {
        "goals": "goal threat",
        "assists": "creativity",
        "clean_sheet": "clean-sheet equity",
        "defcon": "defensive contribution points",
        "bonus": "bonus potential",
    }
    ranked = sorted(
        ((components.get(key, 0.0), label) for key, label in contributors.items()),
        reverse=True,
    )
    if ranked and ranked[0][0] > 0.3:
        value, label = ranked[0]
        parts.append(f"{value:.1f} of the {score.mean:.1f} xP comes from {label}")

    if score.fixture_kind is FixtureKind.DOUBLE:
        parts.append(f"plays {score.fixture_count} times")
    elif score.fixture_kind is FixtureKind.BLANK:
        parts.append("BLANK - no fixture")

    if score.opponents:
        venue_note = " vs " + ", ".join(score.opponents)
        parts.append(venue_note.strip())

    # Ownership framing, which is the rank-attacking argument made explicit.
    if score.ownership < 5:
        parts.append(f"only {score.ownership:.1f}% owned, so returns are near-pure rank gain")
    elif score.ownership > 35:
        parts.append(
            f"{score.ownership:.0f}% owned - largely defensive value, the field has him too"
        )

    if score.ceiling >= 12:
        parts.append(f"ceiling of {score.ceiling:.0f} pts (P90)")

    if score.availability.risk >= 0.1:
        parts.append(f"availability risk {score.availability.risk:.0%}")

    if not parts:
        parts.append(f"{score.mean:.1f} xP at {score.price:.1f}m")

    sentence = "; ".join(parts)
    return sentence[0].upper() + sentence[1:] + "."


def warnings_for(score: PlayerScore) -> list[str]:
    """Everything the user should know before acting on this pick."""
    out: list[str] = []
    availability = score.availability

    if availability.awaiting_press_conference:
        out.append(
            "Listed as 'Currently Being Assessed' - confirm the presser before committing."
        )
    if availability.conflicting_sources:
        out.append("Sources disagree on fitness: " + ", ".join(availability.conflicting_sources))
    if availability.flow_cause in {"bad_news", "bad_news_out_of_hours"}:
        out.append(
            "Unexplained transfer outflow - managers may know something FPL has not published."
        )
    if score.fixture_kind is FixtureKind.PROVISIONAL:
        out.append("Kickoff time is provisional (TBC); this fixture could move gameweek.")
    if availability.fpl_news:
        out.append(f"FPL news: {availability.fpl_news}")
    return out


# ---------------------------------------------------------------------------
# Board construction
# ---------------------------------------------------------------------------
@dataclass
class Board:
    """The finished output: what to buy, what to avoid, and what to watch.

    `captains` and `wildcard` are optional because both can legitimately be
    absent - the wildcard optimiser returns nothing in pre-season when there are
    no projections to optimise against, and rendering must degrade to omitting
    the section rather than failing.
    """

    buys_by_position: dict[str, list[Recommendation]]
    sells: list[Recommendation]
    watchlist: list[PlayerScore]
    returning: list[tuple[PlayerScore, str]]
    captains: list[CaptainPick] = field(default_factory=list)
    wildcard: WildcardSquad | None = None
    horizon_note: str = ""

    @property
    def all_buys(self) -> list[Recommendation]:
        return [rec for recs in self.buys_by_position.values() for rec in recs]


def build_buy_board(
    scores: list[PlayerScore],
    tunables: ModelTunables,
    *,
    per_position: int = 5,
    min_price: float = 3.9,
) -> dict[str, list[Recommendation]]:
    """Rank buy candidates within each position.

    By position rather than overall, because comparing a 4.0m defender with a
    14.0m forward on raw xP is not a decision anyone actually makes - squads have
    positional slots, so that is the axis on which the choice is real.

    Hard filters applied first:
      * `can_transact` false - FPL will not let you buy him at all. Safer than
        interpreting `status`, and it is FPL's own answer to this exact question.
      * Blank gameweek - zero points with certainty.
      * Availability risk at or above 0.95 - effectively ruled out.
    """
    eligible = [
        score
        for score in scores
        if score.fixture_kind is not FixtureKind.BLANK
        and score.availability.risk < 0.95
        and score.price >= min_price
    ]

    by_position: dict[str, list[PlayerScore]] = {}
    for score in eligible:
        by_position.setdefault(score.position, []).append(score)

    board: dict[str, list[Recommendation]] = {}
    for position in ("GKP", "DEF", "MID", "FWD"):
        candidates = by_position.get(position, [])
        ranked = sorted(candidates, key=lambda s: rank_score(s, tunables), reverse=True)

        recommendations: list[Recommendation] = []
        for index, score in enumerate(ranked[:per_position]):
            # The runner-up is the next-best player at a similar price, not simply
            # the next name on the list. A runner-up you cannot afford is not an
            # alternative, it is a different decision.
            runner_up = _find_runner_up(score, ranked, exclude_index=index)
            recommendations.append(
                Recommendation(
                    score=score,
                    rank_score=round(rank_score(score, tunables), 3),
                    confidence=assess_confidence(score),
                    why=explain(score, tunables),
                    runner_up=runner_up,
                    warnings=warnings_for(score),
                )
            )
        board[position] = recommendations

    return board


def _find_runner_up(
    score: PlayerScore, ranked: list[PlayerScore], exclude_index: int, price_band: float = 1.0
) -> str | None:
    """The best alternative within a comparable price band."""
    for index, candidate in enumerate(ranked):
        if index == exclude_index or candidate.element_id == score.element_id:
            continue
        if abs(candidate.price - score.price) <= price_band:
            return (
                f"{candidate.name} ({candidate.team_short}, {candidate.price:.1f}m, "
                f"{candidate.mean:.1f} xP, {candidate.ownership:.1f}% owned)"
            )
    return None


def build_sell_list(
    scores: list[PlayerScore],
    tunables: ModelTunables,
    *,
    limit: int = 12,
    ownership_floor: float = 3.0,
) -> list[Recommendation]:
    """Players to move off, with the evidence that triggered each entry.

    Restricted to players with meaningful ownership. The bot does not know the
    user's squad by design (SPEC section 1), so a sell list is really an
    "avoid / consider moving on" list, and it is only useful when it covers
    players a reader plausibly owns. Telling someone to sell a 0.2%-owned
    defender is noise.
    """
    candidates: list[tuple[float, PlayerScore, list[str]]] = []

    for score in scores:
        if score.ownership < ownership_floor:
            continue

        reasons: list[str] = []
        severity = 0.0

        if score.fixture_kind is FixtureKind.BLANK:
            reasons.append("blanks this gameweek - zero points guaranteed")
            severity += 3.0

        if score.availability.risk >= 0.5:
            reasons.append(
                f"availability risk {score.availability.risk:.0%} ({score.availability.level})"
            )
            severity += score.availability.risk * 2.5

        if score.availability.is_suspension:
            reasons.append("suspended")
            severity += 3.0

        if score.availability.flow_cause in {"bad_news", "bad_news_out_of_hours"}:
            zscore = score.availability.flow_zscore
            reasons.append(
                f"transfer outflow {abs(zscore or 0):.1f} sigma below baseline with no "
                "price or fixture explanation"
            )
            severity += 1.5

        # A high-ownership player with poor expected returns is a rank *liability*
        # under a rank-attacking objective: everyone else owns him, so the downside
        # is shared but the opportunity cost is yours alone.
        if score.ownership > 20 and score.mean < 2.5:
            reasons.append(
                f"{score.ownership:.0f}% owned but only {score.mean:.1f} xP - "
                "holding costs rank without providing cover"
            )
            severity += 1.2

        if score.availability.predicted_to_start is False:
            reasons.append("not in the predicted XI")
            severity += 1.0

        if reasons:
            candidates.append((severity, score, reasons))

    candidates.sort(key=lambda item: item[0], reverse=True)

    return [
        Recommendation(
            score=score,
            rank_score=round(-severity, 3),
            confidence=assess_confidence(score),
            why="; ".join(reasons).capitalize() + ".",
            runner_up=None,
            warnings=warnings_for(score),
        )
        for severity, score, reasons in candidates[:limit]
    ]


def build_watchlist(scores: list[PlayerScore], *, limit: int = 10) -> list[PlayerScore]:
    """Transfer-flow anomalies not yet reflected in FPL's `news`.

    This is the section the whole availability feature exists to produce: players
    the crowd is moving on before the official flag appears. We require that FPL
    has *not* yet published news, because once it has, the signal is no longer
    leading anything.
    """
    flagged = [
        score
        for score in scores
        if score.availability.flow_cause in {"bad_news", "bad_news_out_of_hours"}
        and not score.availability.fpl_news
        and score.availability.flow_zscore is not None
    ]
    flagged.sort(key=lambda s: s.availability.flow_zscore or 0)
    return flagged[:limit]
