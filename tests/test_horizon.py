"""Season-horizon projection.

The projection separates two things that a single-gameweek score conflates:
*player quality*, which is stable, and *fixture load*, which is knowable but
varies week to week. These tests pin that separation, and pin the handling of
blanks and doubles across the horizon - the cases that make a fixture run
genuinely good or bad.
"""

from __future__ import annotations

import numpy as np
import pytest

from fplbot.config import TUNABLES, ModelTunables
from fplbot.domain.horizon import (
    FixtureLoad,
    build_fixture_loads,
    neutral_fixture,
    project_player,
    summarise_horizon,
)
from fplbot.domain.minutes import MinutesDistribution
from fplbot.domain.scoring import ScoringContext
from fplbot.models.domain import AvailabilitySignal
from fplbot.models.fpl import Element, Fixture, ScoringRules

NAILED = MinutesDistribution(0.9, 0.06, 0.03, 0.01)
CLEAR = AvailabilitySignal(element_id=1, risk=0.0)


def a_context(**overrides) -> ScoringContext:
    defaults = {
        "element_types": {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"},
        "scoring_rules": ScoringRules(),
        "season_has_started": True,
        "tunables": TUNABLES,
        "rng": np.random.default_rng(42),
    }
    defaults.update(overrides)
    return ScoringContext(**defaults)


def a_fixture(fixture_id: int, event: int | None, home: int, away: int, **kwargs) -> Fixture:
    payload = {
        "id": fixture_id,
        "event": event,
        "team_h": home,
        "team_a": away,
        "team_h_difficulty": 3,
        "team_a_difficulty": 3,
        "finished": False,
        "provisional_start_time": False,
        "stats": [],
    }
    payload.update(kwargs)
    return Fixture.model_validate(payload)


class TestFixtureLoads:
    def test_a_blank_is_recorded_as_zero_not_omitted(self) -> None:
        """An explicit zero and a missing key mean very different things.

        Omitted, a blank is indistinguishable from a gameweek we did not look at.
        """
        fixtures = [a_fixture(1, 10, home=1, away=2)]

        loads = build_fixture_loads(fixtures, [1, 2, 3], from_gameweek=10)

        assert loads[3].by_gameweek[10] == 0.0
        assert loads[3].blank_gameweeks == [10]

    def test_a_double_sums_both_fixtures(self) -> None:
        fixtures = [
            a_fixture(1, 10, home=1, away=2),
            a_fixture(2, 10, home=3, away=1),
        ]

        loads = build_fixture_loads(fixtures, [1, 2, 3], from_gameweek=10)

        assert loads[1].by_gameweek[10] > loads[2].by_gameweek[10]
        assert loads[1].double_gameweeks == [10]

    def test_past_gameweeks_are_excluded(self) -> None:
        fixtures = [a_fixture(1, 5, home=1, away=2), a_fixture(2, 12, home=1, away=3)]

        loads = build_fixture_loads(fixtures, [1, 2, 3], from_gameweek=10)

        assert 5 not in loads[1].by_gameweek
        assert 12 in loads[1].by_gameweek

    def test_unscheduled_fixtures_are_excluded(self) -> None:
        """`event is None` means postponed or not yet scheduled.

        Counting it would credit a team with a match that has no date.
        """
        fixtures = [a_fixture(1, None, home=1, away=2), a_fixture(2, 11, home=1, away=3)]

        loads = build_fixture_loads(fixtures, [1, 2, 3], from_gameweek=10)

        assert list(loads[1].by_gameweek) == [11]

    def test_the_horizon_can_be_capped(self) -> None:
        fixtures = [a_fixture(i, 10 + i, home=1, away=2) for i in range(8)]

        loads = build_fixture_loads(fixtures, [1, 2], from_gameweek=10, max_gameweeks=3)

        assert max(loads[1].by_gameweek) <= 12

    def test_easier_fixtures_carry_more_load(self) -> None:
        easy = [a_fixture(1, 10, home=1, away=2, team_h_difficulty=1)]
        hard = [a_fixture(1, 10, home=1, away=2, team_h_difficulty=5)]

        easy_load = build_fixture_loads(easy, [1, 2], 10)[1].by_gameweek[10]
        hard_load = build_fixture_loads(hard, [1, 2], 10)[1].by_gameweek[10]

        assert easy_load > hard_load


class TestDecay:
    def test_distant_gameweeks_are_discounted(self) -> None:
        """Not merely conservatism.

        Without decay the optimiser builds a squad around fixtures five months
        away, and those fixtures will not survive contact with injuries, form,
        rotation and rescheduling.
        """
        load = FixtureLoad(team_id=1, by_gameweek={10: 1.0, 30: 1.0})

        undiscounted = load.weighted_total(1.0, from_gameweek=10)
        discounted = load.weighted_total(0.985, from_gameweek=10)

        assert undiscounted == pytest.approx(2.0)
        assert discounted < undiscounted
        assert discounted > 1.0, "distant fixtures still count for something"

    def test_the_current_gameweek_is_undiscounted(self) -> None:
        load = FixtureLoad(team_id=1, by_gameweek={10: 1.0})

        assert load.weighted_total(0.9, from_gameweek=10) == pytest.approx(1.0)

    def test_decay_of_one_is_a_true_sum(self) -> None:
        load = FixtureLoad(team_id=1, by_gameweek={10: 1.0, 20: 2.0, 30: 3.0})

        assert load.weighted_total(1.0, from_gameweek=10) == pytest.approx(6.0)


class TestProjection:
    def test_a_longer_fixture_run_projects_higher(self) -> None:
        """Player quality held constant, schedule varies. That is the whole idea."""
        element = Element.model_validate(
            {"id": 1, "code": 1, "element_type": 3, "team": 1, "web_name": "X", "now_cost": 80}
        )
        context = a_context()

        short_run = FixtureLoad(team_id=1, by_gameweek={10: 1.0})
        long_run = FixtureLoad(team_id=1, by_gameweek=dict.fromkeys(range(10, 20), 1.0))

        short = project_player(element, NAILED, CLEAR, short_run, context, TUNABLES, 10)
        long = project_player(element, NAILED, CLEAR, long_run, context, TUNABLES, 10)

        assert long.season_xp > short.season_xp
        assert long.gameweeks_remaining == 10

    def test_blanks_reduce_the_projection(self) -> None:
        element = Element.model_validate(
            {"id": 1, "code": 1, "element_type": 3, "team": 1, "web_name": "X", "now_cost": 80}
        )
        context = a_context()

        plays_every_week = FixtureLoad(team_id=1, by_gameweek=dict.fromkeys(range(10, 15), 1.0))
        blanks_twice = FixtureLoad(
            team_id=1, by_gameweek={10: 1.0, 11: 0.0, 12: 1.0, 13: 0.0, 14: 1.0}
        )

        full = project_player(element, NAILED, CLEAR, plays_every_week, context, TUNABLES, 10)
        sparse = project_player(element, NAILED, CLEAR, blanks_twice, context, TUNABLES, 10)

        assert sparse.season_xp < full.season_xp
        assert sparse.blank_gameweeks == [11, 13]

    def test_neutral_xp_is_fixture_independent(self) -> None:
        """The per-fixture expectation should not move with the schedule."""
        element = Element.model_validate(
            {"id": 1, "code": 1, "element_type": 3, "team": 1, "web_name": "X", "now_cost": 80}
        )

        short = project_player(
            element, NAILED, CLEAR, FixtureLoad(1, {10: 1.0}), a_context(), TUNABLES, 10
        )
        long = project_player(
            element,
            NAILED,
            CLEAR,
            FixtureLoad(1, dict.fromkeys(range(10, 20), 1.0)),
            a_context(),
            TUNABLES,
            10,
        )

        assert short.neutral_xp == pytest.approx(long.neutral_xp, rel=0.02)

    def test_an_unavailable_player_projects_near_zero(self) -> None:
        element = Element.model_validate(
            {"id": 1, "code": 1, "element_type": 3, "team": 1, "web_name": "X", "now_cost": 80}
        )
        out = AvailabilitySignal(element_id=1, risk=1.0)

        projection = project_player(
            element,
            NAILED,
            out,
            FixtureLoad(1, dict.fromkeys(range(10, 20), 1.0)),
            a_context(),
            TUNABLES,
            10,
        )

        assert projection.season_xp == pytest.approx(0.0, abs=0.5)


class TestNeutralFixture:
    def test_is_a_single_average_home_fixture(self) -> None:
        fixture = neutral_fixture(team_id=7)

        assert len(fixture.fixtures) == 1
        assert fixture.fixtures[0].is_home is True
        assert fixture.fixtures[0].difficulty == 3
        assert fixture.fixtures[0].clean_sheet_probability is not None


class TestSummary:
    def test_states_the_assumption_rather_than_hiding_it(self) -> None:
        projections = {
            1: type("P", (), {"gameweeks_remaining": 20, "season_xp": 100.0})(),
        }

        note = summarise_horizon(projections, TUNABLES)

        assert "20 remaining gameweeks" in note
        assert "discounted" in note

    def test_undiscounted_is_stated_plainly(self) -> None:
        tunables = ModelTunables(horizon_decay_per_gameweek=1.0)
        projections = {
            1: type("P", (), {"gameweeks_remaining": 20, "season_xp": 100.0})(),
        }

        assert "undiscounted" in summarise_horizon(projections, tunables)

    def test_empty_projections_are_handled(self) -> None:
        assert "No season projection" in summarise_horizon({}, TUNABLES)
