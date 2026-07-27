"""Minutes as a distribution, not a point estimate.

SPEC section 5.1 insists on this and the reasoning is worth spelling out, because
"expected minutes" is what almost every FPL model uses.

Consider two midfielders, both with an expected 60 minutes:

* Player A starts every week and plays 60 minutes exactly.
* Player B starts half the time (90 minutes) and is an unused substitute the
  other half (0 minutes).

Their expected minutes are identical. Their *point distributions* are not remotely
alike: A is a steady 4-5 points, B is a bimodal mixture of 8 and 0. Under a
rank-attacking objective (SPEC 5.4) the difference is the entire story, because
it is the upside tail that gains rank and the zero that loses it.

Collapsing to a scalar destroys exactly the information the objective needs. So we
model four buckets - starter, rotation, cameo, out - each with a probability, and
the scorer samples a bucket before sampling anything else.

Where the probabilities come from, in order of trustworthiness:

1. **Predicted line-ups** (Fantasy Football Scout). A named starter is a strong
   signal, and it is the only source that reflects a manager's press conference.
2. **Availability** (FPL `chance_of_playing_next_round` cross-checked against
   PremierInjuries). This caps everything - a 25% player cannot be a likely
   starter no matter what a line-up predicted three days ago.
3. **Recent starts** (`starts` and `minutes` from the season so far). Only usable
   once the season has actually started; in pre-season these hold last season's
   values and must not be trusted. SPEC section 0.
"""

from __future__ import annotations

from dataclasses import dataclass

from fplbot.config import TUNABLES
from fplbot.models.domain import AvailabilitySignal
from fplbot.models.fpl import Element


@dataclass(frozen=True)
class MinutesDistribution:
    """Probabilities over the four minutes buckets. Always sums to 1."""

    starter: float
    rotation: float
    cameo: float
    out: float

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.starter, self.rotation, self.cameo, self.out)

    @property
    def expected_minutes(self) -> float:
        """The scalar we deliberately do not use for sampling.

        Kept for reporting and for sanity-checking against FPL's own `ep_next`,
        which is itself a point estimate.
        """
        means = TUNABLES.minutes_by_bucket
        return (
            self.starter * means["starter"]
            + self.rotation * means["rotation"]
            + self.cameo * means["cameo"]
        )

    @property
    def probability_of_playing(self) -> float:
        return 1.0 - self.out

    @property
    def probability_of_sixty(self) -> float:
        """P(reaching 60 minutes), which is the appearance-point and clean-sheet
        threshold. Rotation risk bites hardest exactly here."""
        return self.starter + self.rotation * 0.45

    def normalised(self) -> MinutesDistribution:
        total = self.starter + self.rotation + self.cameo + self.out
        if total <= 0:
            return MinutesDistribution(0.0, 0.0, 0.0, 1.0)
        return MinutesDistribution(
            self.starter / total,
            self.rotation / total,
            self.cameo / total,
            self.out / total,
        )


def estimate_minutes(
    element: Element,
    availability: AvailabilitySignal,
    *,
    season_has_started: bool,
    predicted_to_start: bool | None = None,
    games_played: int = 0,
) -> MinutesDistribution:
    """Build a minutes distribution for one player in one fixture.

    Args:
        season_has_started: gates the use of `starts` and `minutes`. In pre-season
            those fields hold LAST season's values while everything around them
            has been reset, so using them silently mixes two seasons.
        predicted_to_start: from FFS predicted line-ups, or None if unavailable.
        games_played: team's games so far, for the starts ratio denominator.
    """
    # -- 1. Base rates from history, or a neutral prior ---------------------
    if season_has_started and games_played > 0 and element.minutes > 0:
        start_rate = min(1.0, element.starts / games_played)
        minutes_per_game = element.minutes / games_played

        if start_rate >= 0.8 and minutes_per_game >= 70:
            base = MinutesDistribution(0.82, 0.12, 0.04, 0.02)
        elif start_rate >= 0.5:
            base = MinutesDistribution(0.55, 0.28, 0.12, 0.05)
        elif start_rate >= 0.2:
            base = MinutesDistribution(0.25, 0.30, 0.35, 0.10)
        elif minutes_per_game >= 10:
            base = MinutesDistribution(0.08, 0.17, 0.60, 0.15)
        else:
            base = MinutesDistribution(0.03, 0.07, 0.35, 0.55)
    else:
        # No usable history: pre-season, a new signing, or a promoted club's
        # squad. Price is the best available proxy for squad status - clubs do
        # not pay 12.0m for a substitute - and it is at least honest about being
        # a prior rather than a measurement.
        if element.price >= 9.0:
            base = MinutesDistribution(0.72, 0.16, 0.08, 0.04)
        elif element.price >= 6.0:
            base = MinutesDistribution(0.52, 0.25, 0.16, 0.07)
        elif element.price >= 4.5:
            base = MinutesDistribution(0.34, 0.26, 0.28, 0.12)
        else:
            base = MinutesDistribution(0.18, 0.22, 0.35, 0.25)

    # -- 2. Predicted line-ups override the base rate ----------------------
    # This is the freshest signal we have: FFS updates it after press conferences,
    # so it reflects information that no season aggregate can.
    if predicted_to_start is True:
        # Set the starter probability to a floor and redistribute the remainder
        # across the other buckets in their existing proportions. Clamping each
        # bucket independently and then normalising would NOT preserve the floor:
        # the four clamped values sum to more than 1, so normalising drags the
        # starter probability back below 0.85 - quietly undoing the override.
        base = _with_starter_floor(base, 0.85)
    elif predicted_to_start is False:
        # Predicted NOT to start is weaker evidence than predicted to start:
        # line-ups are eleven names and a squad is twenty-five, so being absent
        # from the XI is partly just a shortlist artefact. We shift towards the
        # bench without slamming the door.
        base = MinutesDistribution(
            starter=base.starter * 0.30,
            rotation=base.rotation,
            cameo=base.cameo + 0.15,
            out=base.out + 0.05,
        ).normalised()

    # -- 3. Availability caps everything -----------------------------------
    # A 25% player cannot be an 85% starter, whatever a line-up predicted before
    # the manager spoke. This is a hard cap rather than a blend, deliberately:
    # the injury signal is the most recent and most decisive of the three.
    availability_ceiling = availability.fpl_chance_pct / 100.0
    if availability.injury_status is not None:
        # Cross-validated against PremierInjuries. Where the two disagree we take
        # the more pessimistic view and the disagreement is surfaced in the
        # report rather than silently resolved. SPEC 5.3 step 4.
        third_party_ceiling = _injury_status_ceiling(availability.injury_status)
        availability_ceiling = min(availability_ceiling, third_party_ceiling)

    if availability_ceiling < 1.0:
        playing = base.probability_of_playing * availability_ceiling
        scale = playing / base.probability_of_playing if base.probability_of_playing else 0.0
        base = MinutesDistribution(
            starter=base.starter * scale,
            rotation=base.rotation * scale,
            cameo=base.cameo * scale,
            out=1.0 - playing,
        )

    # -- 4. Suspension is deterministic ------------------------------------
    # A ban is not a fitness question. There is no probability to model: the
    # player is not available, full stop. SPEC section 4.3.
    if availability.is_suspension:
        return MinutesDistribution(0.0, 0.0, 0.0, 1.0)

    if not element.is_transactable:
        return MinutesDistribution(0.0, 0.0, 0.0, 1.0)

    return base.normalised()


def _with_starter_floor(base: MinutesDistribution, floor: float) -> MinutesDistribution:
    """Raise the starter probability to `floor`, rescaling the other buckets.

    The remaining mass is shared out in the buckets' existing proportions, so a
    player whose non-starting mass was mostly "cameo" keeps that shape. The
    result always sums to exactly 1.
    """
    starter = max(base.starter, floor)
    remainder = 1.0 - starter
    others = base.rotation + base.cameo + base.out
    if others <= 0:
        return MinutesDistribution(starter, 0.0, 0.0, remainder)
    scale = remainder / others
    return MinutesDistribution(
        starter=starter,
        rotation=base.rotation * scale,
        cameo=base.cameo * scale,
        out=base.out * scale,
    )


def _injury_status_ceiling(status: str) -> float:
    """PremierInjuries `Status` -> maximum probability of playing."""
    return {
        "Ruled Out": 0.0,
        "25%": 0.25,
        "50%": 0.50,
        "75%": 0.75,
    }.get(status, 1.0)
