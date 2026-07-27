"""Projecting expected points to the end of the season.

The gameweek scorer in `scoring.py` answers "what will this player score *this*
week". The wildcard optimiser needs a different question: "what will this player
score between now and May".

WHY NOT JUST RUN THE MONTE CARLO 38 TIMES
------------------------------------------
Because it would be both slow and dishonest.

Slow: 600 players x 38 gameweeks x 4,000 samples is 91 million draws, well past
a 120-second budget.

Dishonest is the more interesting objection. The Monte Carlo models a *specific*
fixture in detail - a particular opponent, a particular clean-sheet probability
from ClubElo. None of that detail exists for gameweek 31. Running the same
machinery over a fixture we know almost nothing about produces a number with
five significant figures and one of information.

WHAT WE DO INSTEAD
------------------
Separate what we know from what we do not:

1. **A neutral-fixture xP per player**, from one extra Monte Carlo pass against a
   synthetic average fixture (difficulty 3, home, league-average clean sheet).
   This captures everything player-specific - minutes, xG, set pieces, DefCon,
   position - with no fixture-specific noise.

2. **A fixture load per gameweek**, which is genuinely knowable: how many times
   does this team play, and how hard is each one? Blanks are zero, doubles are
   two, and FPL's own 1-5 difficulty scale multiplies each.

Season xP is then the product, summed over remaining gameweeks. Player quality
and fixture run stay separable, which is also how a human thinks about it.

THE DECAY, AND WHY IT IS NOT JUST CONSERVATISM
-----------------------------------------------
Each gameweek is weighted by `decay ** k`. Without it, the optimiser will happily
build a squad around a team's superb run in March - and that run will not survive
contact with reality. Injuries, form, rotation, managerial changes, cup
progression and rescheduling all intervene between now and then.

Weighting the near term more heavily produces a squad that is genuinely good now
and merely plausible later, which is the right trade for a decision you act on
this week. The knob is `horizon_decay_per_gameweek`; set it to 1.0 for a true
undiscounted sum.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from fplbot.config import ModelTunables
from fplbot.domain.fixtures import fixture_difficulty_multiplier
from fplbot.domain.minutes import MinutesDistribution
from fplbot.domain.scoring import ScoringContext, score_player
from fplbot.models.domain import (
    AvailabilitySignal,
    FixtureContext,
    TeamGameweek,
)
from fplbot.models.fpl import Element, Fixture
from fplbot.observability import logger

# The synthetic fixture used to derive a player's neutral-fixture expectation.
# Difficulty 3 is the middle of FPL's scale; the clean-sheet probability is
# roughly the league average for a mid-table side at home.
NEUTRAL_DIFFICULTY = 3
NEUTRAL_CLEAN_SHEET_PROBABILITY = 0.28


@dataclass(frozen=True)
class FixtureLoad:
    """How much football a team has left, gameweek by gameweek."""

    team_id: int
    # gameweek -> summed difficulty multiplier across that gameweek's fixtures.
    # A blank contributes 0.0; a double contributes the sum of both.
    by_gameweek: dict[int, float] = field(default_factory=dict)

    def weighted_total(self, decay: float, from_gameweek: int) -> float:
        """Total fixture load, discounted by distance into the future."""
        total = 0.0
        for gameweek, load in self.by_gameweek.items():
            steps = max(0, gameweek - from_gameweek)
            total += load * (decay**steps)
        return total

    @property
    def blank_gameweeks(self) -> list[int]:
        return sorted(gw for gw, load in self.by_gameweek.items() if load == 0.0)

    @property
    def double_gameweeks(self) -> list[int]:
        # A double shows up as a load above any single fixture's possible
        # multiplier. The easiest honest test is a count, so we keep counts too.
        return sorted(gw for gw, load in self.by_gameweek.items() if load > 1.45)


@dataclass
class SeasonProjection:
    """A player's projected points from now to the end of the season."""

    element_id: int
    neutral_xp: float  # expected points in one average fixture
    season_xp: float  # decayed sum over every remaining fixture
    gameweeks_remaining: int
    fixtures_remaining: int
    blank_gameweeks: list[int] = field(default_factory=list)
    double_gameweeks: list[int] = field(default_factory=list)

    @property
    def xp_per_gameweek(self) -> float:
        if self.gameweeks_remaining <= 0:
            return 0.0
        return self.season_xp / self.gameweeks_remaining


def build_fixture_loads(
    fixtures: list[Fixture],
    team_ids: list[int],
    from_gameweek: int,
    *,
    max_gameweeks: int | None = None,
) -> dict[int, FixtureLoad]:
    """Compute each team's remaining fixture load, gameweek by gameweek.

    Fixtures with `event is None` are excluded - they are unscheduled, and
    counting them would credit a team with a match that has no date. When such a
    fixture is eventually rescheduled it reappears in a real gameweek and is
    picked up on the next run.
    """
    horizon_end = None
    if max_gameweeks is not None:
        horizon_end = from_gameweek + max_gameweeks - 1

    per_team: dict[int, dict[int, float]] = {team_id: {} for team_id in team_ids}
    gameweeks_seen: set[int] = set()

    for fixture in fixtures:
        gameweek = fixture.event
        if gameweek is None or gameweek < from_gameweek:
            continue
        if horizon_end is not None and gameweek > horizon_end:
            continue
        gameweeks_seen.add(gameweek)

        for team_id, is_home in ((fixture.team_h, True), (fixture.team_a, False)):
            if team_id not in per_team:
                continue
            multiplier = fixture_difficulty_multiplier(fixture.difficulty_for(team_id), is_home)
            per_team[team_id][gameweek] = per_team[team_id].get(gameweek, 0.0) + multiplier

    # Every team gets an entry for every gameweek in range, including 0.0 for a
    # blank. Without the explicit zero a blank is indistinguishable from a
    # gameweek we simply did not look at, and the two mean very different things.
    for loads in per_team.values():
        for gameweek in gameweeks_seen:
            loads.setdefault(gameweek, 0.0)

    return {
        team_id: FixtureLoad(team_id=team_id, by_gameweek=loads)
        for team_id, loads in per_team.items()
    }


def neutral_fixture(team_id: int) -> TeamGameweek:
    """A synthetic average fixture, used to isolate player quality from schedule."""
    return TeamGameweek(
        team_id=team_id,
        gameweek=0,
        fixtures=(
            FixtureContext(
                fixture_id=-1,
                team_id=team_id,
                opponent_id=-1,
                is_home=True,
                difficulty=NEUTRAL_DIFFICULTY,
                clean_sheet_probability=NEUTRAL_CLEAN_SHEET_PROBABILITY,
            ),
        ),
    )


def project_player(
    element: Element,
    minutes_dist: MinutesDistribution,
    availability: AvailabilitySignal,
    fixture_load: FixtureLoad,
    context: ScoringContext,
    tunables: ModelTunables,
    from_gameweek: int,
) -> SeasonProjection:
    """Project one player's remaining season.

    The neutral-fixture pass reuses the full Monte Carlo scorer, so everything it
    knows about minutes, shrunk xG, set pieces, DefCon and availability carries
    through. Only the fixture is synthetic.
    """
    distribution, _ = score_player(
        element, neutral_fixture(element.team), minutes_dist, availability, context
    )
    neutral_xp = distribution.mean

    # `fixture_difficulty_multiplier` returns 1.0 for a neutral fixture before
    # the venue adjustment, so dividing the load by the neutral multiplier keeps
    # the two consistent. Without this the projection is biased by however the
    # neutral fixture happens to be defined.
    neutral_multiplier = fixture_difficulty_multiplier(NEUTRAL_DIFFICULTY, is_home=True)
    weighted_load = fixture_load.weighted_total(tunables.horizon_decay_per_gameweek, from_gameweek)

    season_xp = neutral_xp * (weighted_load / neutral_multiplier)

    fixtures_remaining = sum(1 for load in fixture_load.by_gameweek.values() if load > 0)
    # Count doubles properly: a gameweek's load above ~1.45 implies two fixtures.
    extra = sum(1 for load in fixture_load.by_gameweek.values() if load > 1.45)

    return SeasonProjection(
        element_id=element.id,
        neutral_xp=round(neutral_xp, 3),
        season_xp=round(season_xp, 2),
        gameweeks_remaining=len(fixture_load.by_gameweek),
        fixtures_remaining=fixtures_remaining + extra,
        blank_gameweeks=fixture_load.blank_gameweeks,
        double_gameweeks=fixture_load.double_gameweeks,
    )


def project_all(
    elements: list[Element],
    minutes_by_element: dict[int, MinutesDistribution],
    availability_by_element: dict[int, AvailabilitySignal],
    fixture_loads: dict[int, FixtureLoad],
    context: ScoringContext,
    tunables: ModelTunables,
    from_gameweek: int,
) -> dict[int, SeasonProjection]:
    """Project every player. One extra Monte Carlo pass in total."""
    projections: dict[int, SeasonProjection] = {}

    for element in elements:
        load = fixture_loads.get(element.team)
        if load is None:
            continue
        minutes = minutes_by_element.get(element.id)
        availability = availability_by_element.get(element.id)
        if minutes is None or availability is None:
            continue

        projections[element.id] = project_player(
            element, minutes, availability, load, context, tunables, from_gameweek
        )

    if projections:
        best = max(projections.values(), key=lambda p: p.season_xp)
        logger.info(
            "Projected season expected points",
            extra={
                "players": len(projections),
                "gameweeks_remaining": best.gameweeks_remaining,
                "decay": tunables.horizon_decay_per_gameweek,
                "top_season_xp": best.season_xp,
            },
        )
    return projections


def summarise_horizon(projections: dict[int, SeasonProjection], tunables: ModelTunables) -> str:
    """One line for the report, stating the assumption rather than hiding it."""
    if not projections:
        return "No season projection available."

    gameweeks = max(p.gameweeks_remaining for p in projections.values())
    decay = tunables.horizon_decay_per_gameweek

    if decay >= 1.0:
        return f"Projected over {gameweeks} remaining gameweeks, undiscounted."

    # Show what the decay actually does, so the reader can judge it rather than
    # take it on trust.
    far = decay ** max(0, gameweeks - 1)
    return (
        f"Projected over {gameweeks} remaining gameweeks, discounted "
        f"{(1 - decay) * 100:.1f}% per gameweek - the final gameweek carries "
        f"{far:.0%} of the weight of this one. Distant fixtures are real but "
        f"the squads that play them are not yet knowable."
    )


def percentile_of(values: list[float], value: float) -> float:
    """Where a number sits in a distribution, for explanatory text."""
    if not values:
        return 0.0
    return float(np.mean(np.array(values) <= value)) * 100
