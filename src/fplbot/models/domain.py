"""Our own domain objects - the vocabulary the model and the report share.

`Distribution` is the most important type here, and it is deliberately the
*interface* between scoring and everything downstream. SPEC section 6.4 asks for
a clean seam so that a squad-aware ILP optimiser can be added in v2 without a
rewrite: keep `score_player(player, fixture, context) -> Distribution` pure, and
keep ranking separate from it. A future optimiser consumes exactly these
distributions, unchanged.

The reason scoring returns a distribution rather than a number is SPEC section
5.4. Under a rank-attacking objective, the spread *is* the signal. A 6.0 xP
player who scores 6 every week and a 6.0 xP player who scores 0 or 15 are
completely different propositions, and collapsing both to "6.0" throws away the
only thing that distinguishes them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np


# ---------------------------------------------------------------------------
# Distributions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Distribution:
    """An empirical distribution of points, held as Monte Carlo samples.

    Empirical rather than parametric because the underlying process is a mixture:
    a minutes bucket is drawn first, and the points distribution conditional on
    "started" looks nothing like the one conditional on "cameo". There is no
    tidy closed form for that mixture, and there is no need for one - four
    thousand samples per player costs milliseconds in numpy and gives us every
    percentile for free.
    """

    samples: np.ndarray

    @property
    def mean(self) -> float:
        return float(np.mean(self.samples))

    @property
    def std(self) -> float:
        return float(np.std(self.samples))

    def percentile(self, p: float) -> float:
        return float(np.percentile(self.samples, p))

    @property
    def ceiling(self) -> float:
        """P90. The upside a differential is bought for."""
        return self.percentile(90)

    @property
    def floor(self) -> float:
        """P10. The downside a captain choice has to survive."""
        return self.percentile(10)

    def probability_above(self, threshold: float) -> float:
        """P(points > threshold). Used for haul probabilities in the report."""
        return float(np.mean(self.samples > threshold))

    @property
    def haul_probability(self) -> float:
        """P(10 or more points) - the conventional definition of a haul."""
        return float(np.mean(self.samples >= 10))

    def summary(self) -> dict[str, float]:
        return {
            "mean": round(self.mean, 2),
            "floor_p10": round(self.floor, 2),
            "ceiling_p90": round(self.ceiling, 2),
            "std": round(self.std, 2),
            "haul_probability": round(self.haul_probability, 3),
        }

    @classmethod
    def constant(cls, value: float, size: int = 1) -> Distribution:
        """A degenerate distribution. Used for blanks, where the answer is zero."""
        return cls(samples=np.full(size, value, dtype=np.float64))


# ---------------------------------------------------------------------------
# Fixture context
# ---------------------------------------------------------------------------
class FixtureKind(StrEnum):
    NORMAL = "normal"
    BLANK = "blank"  # team has no fixture this gameweek
    DOUBLE = "double"  # two or more fixtures
    PROVISIONAL = "provisional"  # kickoff time is a placeholder ("TBC")


@dataclass(frozen=True)
class FixtureContext:
    """Everything the scorer needs to know about one fixture for one team."""

    fixture_id: int
    team_id: int
    opponent_id: int
    is_home: bool
    difficulty: int  # FPL's 1-5 scale, populated and clean
    provisional: bool = False
    # From ClubElo scoreline probabilities, or from odds when we have them.
    clean_sheet_probability: float | None = None
    expected_team_goals: float | None = None
    expected_goals_conceded: float | None = None
    win_probability: float | None = None


@dataclass(frozen=True)
class TeamGameweek:
    """A team's fixtures for one gameweek, classified.

    Blanks and doubles are the highest-leverage fixture signal available and are
    trivial to compute, which makes getting them wrong an unforced error. Note
    the double test is `>= 2`, not `== 2`: triple gameweeks exist.
    """

    team_id: int
    gameweek: int
    fixtures: tuple[FixtureContext, ...]

    @property
    def kind(self) -> FixtureKind:
        if not self.fixtures:
            return FixtureKind.BLANK
        if len(self.fixtures) >= 2:
            return FixtureKind.DOUBLE
        if self.fixtures[0].provisional:
            return FixtureKind.PROVISIONAL
        return FixtureKind.NORMAL

    @property
    def count(self) -> int:
        return len(self.fixtures)


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------
class RiskLevel(StrEnum):
    CLEAR = "clear"
    WATCH = "watch"
    DOUBT = "doubt"
    SERIOUS = "serious"
    OUT = "out"


@dataclass
class AvailabilitySignal:
    """The composite availability picture for one player.

    `risk` in [0, 1] multiplies into expected points. The rest is evidence: the
    report has to be legible enough to argue with (SPEC section 6.1), which means
    every number that moved a recommendation needs to be visible.
    """

    element_id: int
    risk: float  # 0 = certain to play, 1 = certain not to
    level: RiskLevel = RiskLevel.CLEAR

    # -- FPL's own view ----------------------------------------------------
    fpl_chance_pct: int = 100
    fpl_status: str = "a"
    fpl_news: str = ""
    news_age_hours: float | None = None
    scout_news_link: str | None = None

    # -- transfer-flow inference ------------------------------------------
    # Net flow as a fraction of *current owners*, not an absolute count. A 3%-owned
    # and a 40%-owned player at the same absolute net-out are telling completely
    # different stories. SPEC section 5.3 step 1.
    net_flow_per_owner: float | None = None
    flow_zscore: float | None = None
    flow_cause: str | None = None  # bad_news | price_bandwagon | fixture | chip_churn
    snapshots_available: int = 0

    # -- third-party -------------------------------------------------------
    injury_status: str | None = None  # PremierInjuries Status
    injury_condition: str | None = None  # "Currently Being Assessed" | "Not Available"
    injury_reason: str | None = None  # includes "Suspended"
    potential_return: str | None = None  # day-first DD/MM/YYYY upstream
    predicted_to_start: bool | None = None  # from FFS predicted line-ups

    # -- meta --------------------------------------------------------------
    # Agreement across two independent signals should sharply raise confidence;
    # disagreement should lower it and be surfaced rather than silently resolved.
    corroborating_sources: list[str] = field(default_factory=list)
    conflicting_sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_suspension(self) -> bool:
        """Suspensions ride in the same table as injuries but are not injuries.

        A ban has a known, deterministic end date. Modelling its "return
        probability" as though it were a fitness question is simply wrong.
        SPEC section 4.3.
        """
        return (self.injury_reason or "").strip().lower() == "suspended" or self.fpl_status == "s"

    @property
    def awaiting_press_conference(self) -> bool:
        """The flag that defines the Phase 2 re-check set.

        On the live table 23 of 44 listed players are "Currently Being Assessed".
        That status is precisely what a manager's press conference resolves, and
        pressers land after the 48h run. SPEC section 3.
        """
        return (self.injury_condition or "") == "Currently Being Assessed"


# ---------------------------------------------------------------------------
# Scoring output
# ---------------------------------------------------------------------------
@dataclass
class PlayerScore:
    """The scorer's full output for one player in one gameweek."""

    element_id: int
    name: str
    team_short: str
    # The numeric FPL team id. `team_short` is what the report prints, but the
    # wildcard optimiser needs the id for the three-players-per-club rule, and
    # resolving it back from a short name would be a lookup that can silently
    # fail on a rename.
    team_id: int
    position: str
    price: float
    ownership: float

    distribution: Distribution
    availability: AvailabilitySignal
    fixture_kind: FixtureKind
    fixture_count: int

    # Component breakdown, kept for explainability. The report needs to say
    # *why*, and "5.1 xP of which 2.3 is clean-sheet equity" is an argument;
    # "5.1 xP" is an assertion.
    components: dict[str, float] = field(default_factory=dict)
    opponents: list[str] = field(default_factory=list)

    # FPL's own expected points for the next gameweek - our benchmark.
    ep_next: float = 0.0

    @property
    def mean(self) -> float:
        return self.distribution.mean

    @property
    def ceiling(self) -> float:
        return self.distribution.ceiling

    @property
    def floor(self) -> float:
        return self.distribution.floor

    @property
    def value(self) -> float:
        """Expected points per million. The classic budget-efficiency metric."""
        return self.mean / self.price if self.price else 0.0


# ---------------------------------------------------------------------------
# Ranking output
# ---------------------------------------------------------------------------
class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Recommendation:
    """One entry on the buy board or the sell list.

    Every field after `rank_score` exists because SPEC section 6.1 requires that
    each pick carries a confidence, a runner-up and a human-readable "why". The
    user intends to compete against these recommendations; a pick they cannot
    reason about is a pick they cannot disagree with, which defeats the purpose.
    """

    score: PlayerScore
    rank_score: float  # the rank-attacking objective, not xP
    confidence: Confidence
    why: str
    runner_up: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def element_id(self) -> int:
        return self.score.element_id


# ---------------------------------------------------------------------------
# Run context and data quality
# ---------------------------------------------------------------------------
class SourceState(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"  # served from last-known-good
    FAILED = "failed"  # unavailable and no fallback
    SKIPPED = "skipped"  # deliberately not called this run


@dataclass
class SourceStatus:
    name: str
    state: SourceState
    detail: str = ""
    age_seconds: int | None = None

    @property
    def is_usable(self) -> bool:
        return self.state in {SourceState.OK, SourceState.DEGRADED}


@dataclass
class DataQuality:
    """The honest account of what we knew when we made the recommendations.

    Rendered into the email header. SPEC section 6.2 sets the policy: always
    produce output, and be explicit about what was stale. A recommendation from
    imperfect data, clearly flagged, beats silence three hours before a deadline.
    """

    sources: dict[str, SourceStatus] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)
    unresolved_players: list[str] = field(default_factory=list)
    invariant_failures: list[str] = field(default_factory=list)

    def record(
        self,
        name: str,
        state: SourceState,
        detail: str = "",
        age_seconds: int | None = None,
    ) -> None:
        self.sources[name] = SourceStatus(name, state, detail, age_seconds)

    def add_caveat(self, text: str) -> None:
        if text not in self.caveats:
            self.caveats.append(text)

    @property
    def degraded_sources(self) -> list[str]:
        return sorted(name for name, s in self.sources.items() if s.state is not SourceState.OK)

    @property
    def worst_age_seconds(self) -> int:
        """Age of the stalest source we relied on.

        Compared against the hard staleness ceiling (24h) to decide whether to
        send transfer advice at all, or to send a failure notice instead.
        """
        ages = [s.age_seconds for s in self.sources.values() if s.age_seconds is not None]
        return max(ages) if ages else 0

    def summary_line(self) -> str:
        if not self.degraded_sources:
            return "All sources healthy."
        return "Degraded: " + ", ".join(
            f"{name} ({self.sources[name].state})" for name in self.degraded_sources
        )


@dataclass
class RunContext:
    """Everything one invocation needs to know about itself."""

    now_epoch: int
    season: str
    gameweek: int
    deadline_epoch: int
    seconds_to_deadline: int
    tier: str  # "48h" | "24h" | "3h"
    is_confirmed_phase: bool  # the scheduled T-24h run; the one to act on
    season_has_started: bool
    data_quality: DataQuality = field(default_factory=DataQuality)
    tunables: dict[str, Any] = field(default_factory=dict)

    @property
    def hours_to_deadline(self) -> float:
        return self.seconds_to_deadline / 3600.0

    @property
    def phase_label(self) -> str:
        return "CONFIRMED" if self.is_confirmed_phase else "PROVISIONAL"
