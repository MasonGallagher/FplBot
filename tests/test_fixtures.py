"""Blank, double and postponed gameweek detection."""

from __future__ import annotations

from fplbot.domain.fixtures import (
    build_team_gameweeks,
    describe_gameweek,
    fixture_difficulty_multiplier,
    fixtures_for_gameweek,
    postponed_fixtures,
    provisional_fixtures,
)
from fplbot.models.domain import FixtureKind
from fplbot.models.fpl import Fixture


class TestGameweekClassification:
    def test_detects_a_double(self, fixtures_payload: list[dict]) -> None:
        """Team 1 appears in two gameweek-1 fixtures.

        The test is `>= 2`, not `== 2`: triple gameweeks exist, and `== 2` would
        silently downgrade one to a single.
        """
        fixtures = [Fixture.model_validate(f) for f in fixtures_payload]

        gameweeks = build_team_gameweeks(fixtures, 1, list(range(1, 21)))

        assert gameweeks[1].kind is FixtureKind.DOUBLE
        assert gameweeks[1].count == 2

    def test_detects_a_blank(self, fixtures_payload: list[dict]) -> None:
        """Team 4's only fixture has `event: null`, so it blanks.

        The highest-leverage fixture signal available: a blanking player scores
        zero regardless of how good he is.
        """
        fixtures = [Fixture.model_validate(f) for f in fixtures_payload]

        gameweeks = build_team_gameweeks(fixtures, 1, list(range(1, 21)))

        assert gameweeks[4].kind is FixtureKind.BLANK
        assert gameweeks[4].count == 0

    def test_every_team_gets_an_entry(self, fixtures_payload: list[dict]) -> None:
        """A missing team would be indistinguishable from a lookup failure."""
        fixtures = [Fixture.model_validate(f) for f in fixtures_payload]

        gameweeks = build_team_gameweeks(fixtures, 1, list(range(1, 21)))

        assert len(gameweeks) == 20

    def test_detects_a_provisional_kickoff(self, fixtures_payload: list[dict]) -> None:
        """`provisional_start_time` means FPL's own UI renders 'TBC'.

        Such a fixture can still move gameweek, which would turn a
        recommendation into a blank.
        """
        fixtures = [Fixture.model_validate(f) for f in fixtures_payload]

        assert gameweek_kind(fixtures, 3) is FixtureKind.PROVISIONAL

    def test_ten_fixtures_is_not_a_reliable_test(self, fixtures_payload: list[dict]) -> None:
        """`len(fixtures) > 10` is neither necessary nor sufficient.

        Our gameweek 1 has three scheduled fixtures and contains BOTH a double
        and a blank. Counting per team is the only correct approach.
        """
        fixtures = [Fixture.model_validate(f) for f in fixtures_payload]
        scheduled = fixtures_for_gameweek(fixtures, 1)

        assert len(scheduled) == 3

        gameweeks = build_team_gameweeks(fixtures, 1, list(range(1, 21)))
        kinds = {gw.kind for gw in gameweeks.values()}
        assert FixtureKind.DOUBLE in kinds
        assert FixtureKind.BLANK in kinds


def gameweek_kind(fixtures: list[Fixture], team_id: int) -> FixtureKind:
    return build_team_gameweeks(fixtures, 1, list(range(1, 21)))[team_id].kind


class TestPostponements:
    def test_unscheduled_fixtures_are_identified(self, fixtures_payload: list[dict]) -> None:
        fixtures = [Fixture.model_validate(f) for f in fixtures_payload]

        postponed = postponed_fixtures(fixtures)

        assert len(postponed) == 1
        assert postponed[0].id == 4

    def test_unscheduled_fixtures_are_excluded_from_a_gameweek(
        self, fixtures_payload: list[dict]
    ) -> None:
        """Including them would invent matches that do not exist."""
        fixtures = [Fixture.model_validate(f) for f in fixtures_payload]

        assert all(f.event == 1 for f in fixtures_for_gameweek(fixtures, 1))

    def test_provisional_fixtures_are_listed(self, fixtures_payload: list[dict]) -> None:
        fixtures = [Fixture.model_validate(f) for f in fixtures_payload]

        assert [f.id for f in provisional_fixtures(fixtures, 1)] == [2]


class TestDescription:
    def test_describes_an_irregular_gameweek(self, fixtures_payload: list[dict]) -> None:
        fixtures = [Fixture.model_validate(f) for f in fixtures_payload]
        gameweeks = build_team_gameweeks(fixtures, 1, list(range(1, 21)))

        description = describe_gameweek(gameweeks)

        assert "Irregular" in description
        assert "double" in description
        assert "blank" in description


class TestDifficultyMultiplier:
    def test_easier_fixtures_score_higher(self) -> None:
        easy = fixture_difficulty_multiplier(1, is_home=True)
        hard = fixture_difficulty_multiplier(5, is_home=True)

        assert easy > hard

    def test_home_advantage_is_applied(self) -> None:
        home = fixture_difficulty_multiplier(3, is_home=True)
        away = fixture_difficulty_multiplier(3, is_home=False)

        assert home > away

    def test_unknown_difficulty_is_neutral(self) -> None:
        assert fixture_difficulty_multiplier(99, is_home=True) == 1.06
