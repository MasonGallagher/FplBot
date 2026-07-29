"""Captain picks.

CAPTAINCY IS NOT THE SAME DECISION AS A TRANSFER
------------------------------------------------
It is tempting to reuse the rank-attacking objective from `ranking.py` and take
the top five. That would be wrong, in a specific and costly direction.

The transfer objective deliberately discounts ownership, because a template
player's points largely wash out against the field. That logic is sound for
*owning* someone. It inverts for the armband.

The armband doubles a player's score, which amplifies the mean and the variance
together. Three things follow:

1. **The mean dominates.** Doubling makes raw expectation matter roughly twice as
   much as it does in any other decision.

2. **The floor matters far more than usual.** A captain blank is a *double* zero
   and is the single most costly outcome available in a gameweek. Nothing in a
   transfer decision punishes you like it. So we penalise a weak floor
   explicitly, rather than only rewarding a strong ceiling.

3. **Ownership should be penalised much more gently.** The template captain is
   usually the template captain because he is genuinely the best option. A
   captaincy differential is a high-variance rank play that loses ground faster
   than it gains it: when the template captain hauls and yours blanks you drop
   hard, and that happens more often than the reverse. `lambda` here is about a
   third of its transfer value - present, because a differential captain who
   comes off is the single biggest rank swing available, but nothing like
   dominant.

A CAVEAT WE STATE RATHER THAN HIDE
-----------------------------------
The bot does not know your squad (SPEC section 1), so these picks are drawn from
the whole player pool. **You can only captain someone you already own.** The
section is therefore best read as "these are the players worth having the armband
on this week", and the report says so.

`events[].most_captained` gives us FPL's own single most-captained player, which
is enough to label the template pick without needing captaincy percentages that
the public API does not expose.
"""

from __future__ import annotations

from dataclasses import dataclass

from fplbot.config import ModelTunables
from fplbot.models.domain import (
    Confidence,
    FixtureKind,
    PlayerScore,
    RiskLevel,
)
from fplbot.observability import logger

# Points threshold for a "haul". Doubled, a 10-point haul is 20 - the sort of
# gameweek that moves a rank meaningfully.
HAUL_THRESHOLD = 10.0


@dataclass
class CaptainPick:
    """One captaincy recommendation."""

    score: PlayerScore
    captain_score: float  # the captaincy objective, not expected points
    expected_points: float  # 2 x mean - what you actually expect to bank
    haul_probability: float  # P(20+ captained)
    is_template: bool  # FPL's most-captained player
    is_differential: bool
    confidence: Confidence
    why: str
    warnings: list[str]

    @property
    def element_id(self) -> int:
        return self.score.element_id

    @property
    def captained_ceiling(self) -> float:
        return self.score.ceiling * 2

    @property
    def captained_floor(self) -> float:
        return self.score.floor * 2


def captain_score(score: PlayerScore, tunables: ModelTunables) -> float:
    """The captaincy objective.

        2*mean
          + w_ceiling * ceiling
          - w_downside * (mean - floor)          <- spread below expectation
          - w_floor    * max(0, target - floor)  <- risk of returning nothing
          - lambda_c   * (ownership * 2*mean)

    TWO downside terms, and both earn their place.

    The **downside** term penalises how far a bad week falls below expectation,
    and it is the one that does the real work because it scales with the size of
    the downside. Without it the objective cannot punish volatility at all: the
    shortfall term is bounded by `captain_floor_target` (at most about 0.9
    points) while the ceiling bonus is unbounded, so a 50/50 of 0 and 12 would
    out-rank a certain 6 at identical mean. For the armband that is exactly
    backwards.

    The **shortfall** term then handles the distinct absolute risk of returning
    nothing at all, which is worse than a merely wide distribution.

    Between them, a genuine premium - high mean, high ceiling, some variance -
    still comfortably beats a safe mid-price option, because the doubled mean
    dominates. What they rule out is treating a coin flip as equivalent to a
    certainty.
    """
    doubled = 2 * score.mean
    ownership_fraction = min(1.0, score.ownership / 100.0)

    ceiling_bonus = tunables.captain_ceiling_weight * score.ceiling

    downside = max(0.0, score.mean - score.floor)
    downside_penalty = tunables.captain_downside_weight * downside

    shortfall = max(0.0, tunables.captain_floor_target - score.floor)
    floor_penalty = tunables.captain_floor_weight * shortfall

    ownership_penalty = tunables.captain_ownership_lambda * ownership_fraction * doubled

    return doubled + ceiling_bonus - downside_penalty - floor_penalty - ownership_penalty


def explain_captain(pick_score: PlayerScore, tunables: ModelTunables, *, is_template: bool) -> str:
    """A one-line "why", built from what actually drove the number."""
    parts: list[str] = [
        f"{2 * pick_score.mean:.1f} pts expected with the armband ({pick_score.mean:.1f} x2)"
    ]

    haul = pick_score.distribution.haul_probability
    if haul >= 0.10:
        parts.append(f"{haul:.0%} chance of a 20+ captained haul")

    if pick_score.fixture_kind is FixtureKind.DOUBLE:
        parts.append(f"plays {pick_score.fixture_count} times")

    if pick_score.opponents:
        parts.append("vs " + ", ".join(pick_score.opponents))

    if is_template:
        parts.append("the template armband - captaining him is the safe, rank-neutral move")
    elif pick_score.ownership < tunables.captain_differential_ownership:
        parts.append(
            f"only {pick_score.ownership:.1f}% owned, so a haul here is a large rank swing "
            f"- and a blank is an equally large one"
        )

    if pick_score.floor < tunables.captain_floor_target:
        parts.append(f"floor of {2 * pick_score.floor:.0f} captained - there is real blank risk")

    sentence = "; ".join(parts)
    return sentence[0].upper() + sentence[1:] + "."


def assess_captain_confidence(score: PlayerScore, tunables: ModelTunables) -> Confidence:
    """Confidence in the *captaincy* specifically.

    Stricter than the transfer equivalent, because the cost of being wrong is
    doubled. Anything that would make a transfer merely uncertain makes a
    captaincy pick genuinely risky.
    """
    availability = score.availability

    if availability.conflicting_sources or availability.awaiting_press_conference:
        return Confidence.LOW
    if availability.level in {RiskLevel.DOUBT, RiskLevel.SERIOUS, RiskLevel.OUT}:
        return Confidence.LOW
    if score.fixture_kind is FixtureKind.PROVISIONAL:
        return Confidence.LOW
    # A weak floor is a captaincy-specific confidence problem: it is precisely
    # the double-zero scenario.
    if score.floor < tunables.captain_floor_target:
        return Confidence.MEDIUM
    if availability.level is RiskLevel.WATCH:
        return Confidence.MEDIUM
    if availability.risk <= 0.02 and score.floor >= tunables.captain_floor_target * 1.5:
        return Confidence.HIGH
    return Confidence.MEDIUM


def captain_warnings(score: PlayerScore, tunables: ModelTunables) -> list[str]:
    """What to know before handing over the armband."""
    warnings: list[str] = []
    availability = score.availability

    if availability.awaiting_press_conference:
        warnings.append(
            "Listed as 'Currently Being Assessed' - confirm the presser before captaining."
        )
    if availability.risk >= 0.1:
        warnings.append(
            f"Availability risk {availability.risk:.0%} - a captain who does not start "
            "is the worst outcome in the gameweek."
        )
    if score.floor < tunables.captain_floor_target:
        warnings.append(
            f"P10 floor is {2 * score.floor:.0f} captained. Roughly one week in ten "
            "returns close to nothing."
        )
    if score.fixture_kind is FixtureKind.PROVISIONAL:
        warnings.append("Kickoff time is provisional (TBC); the fixture could move gameweek.")
    if availability.conflicting_sources:
        warnings.append(
            "Sources disagree on fitness: " + ", ".join(availability.conflicting_sources)
        )
    return warnings


def build_captain_picks(
    scores: list[PlayerScore],
    tunables: ModelTunables,
    *,
    most_captained_element: int | None = None,
    limit: int | None = None,
) -> list[CaptainPick]:
    """Rank the best captaincy options for this gameweek.

    Args:
        scores: every scored player.
        most_captained_element: FPL's `events[].most_captained`, used only to
            label the template pick.
        limit: how many to return. Defaults to the tunable (5).

    Filters applied first, all of them hard:
      * blanks - a captained blank is zero, doubled;
      * availability risk at or above 0.5, which is far stricter than the buy
        board's 0.95 because the downside is doubled;
      * players FPL will not let you transact.
    """
    limit = limit or tunables.captain_picks

    eligible = [
        score
        for score in scores
        if score.fixture_kind is not FixtureKind.BLANK
        and score.availability.risk < 0.5
        and score.mean > 0
    ]

    ranked = sorted(eligible, key=lambda s: captain_score(s, tunables), reverse=True)

    picks: list[CaptainPick] = []
    for score in ranked[:limit]:
        is_template = (
            most_captained_element is not None and score.element_id == most_captained_element
        )
        picks.append(
            CaptainPick(
                score=score,
                captain_score=round(captain_score(score, tunables), 3),
                expected_points=round(2 * score.mean, 2),
                haul_probability=round(score.distribution.haul_probability, 3),
                is_template=is_template,
                is_differential=score.ownership < tunables.captain_differential_ownership,
                confidence=assess_captain_confidence(score, tunables),
                why=explain_captain(score, tunables, is_template=is_template),
                warnings=captain_warnings(score, tunables),
            )
        )

    if picks:
        logger.info(
            "Built captain picks",
            extra={
                "count": len(picks),
                "top": picks[0].score.name,
                "top_expected": picks[0].expected_points,
                "differentials": sum(1 for p in picks if p.is_differential),
            },
        )
    return picks
