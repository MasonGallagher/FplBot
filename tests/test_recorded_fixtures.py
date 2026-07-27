"""Replay the hand-crafted fixtures in `tests/fixtures/`.

These are the branches SPEC section 7 singles out as "the most likely to be wrong
and the least likely to be exercised before they matter": blank gameweeks,
triples, postponements, the off-season, season end, and FPL's `/updating/`
maintenance page.

Each arrives at an inconvenient moment - a blank in March, `/updating/` an hour
before a deadline - so the only way to have confidence in them is to construct
the situation deliberately. `scripts/record_fixtures.py` writes these files;
this module is what stops them becoming decoration.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from fplbot.config import HttpPolicy
from fplbot.domain.deadline import next_deadline
from fplbot.domain.fixtures import build_team_gameweeks, postponed_fixtures
from fplbot.domain.invariants import check_bootstrap
from fplbot.http.client import HttpClient, HttpFetchError
from fplbot.models.domain import FixtureKind
from fplbot.models.fpl import Bootstrap, Event, Fixture
from fplbot.sources.understat import _coerce_teams

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class TestIrregularGameweeks:
    def test_triple_gameweek_is_not_downgraded(self) -> None:
        """The fixture has THREE matches for team 1, not two.

        An `== 2` implementation would silently classify this as a single
        gameweek and halve the player's expected points. `>= 2` is the test.
        """
        fixtures = [Fixture.model_validate(f) for f in load("double_gameweek.json")["fixtures"]]

        gameweeks = build_team_gameweeks(fixtures, 20, list(range(1, 21)))

        assert gameweeks[1].kind is FixtureKind.DOUBLE
        assert gameweeks[1].count == 3

    def test_blank_gameweek_with_six_fixtures(self) -> None:
        """Six fixtures, so eight teams blank.

        Note this also disproves `len(fixtures) > 10` as an irregularity test:
        six is under ten AND the gameweek is irregular.
        """
        fixtures = [Fixture.model_validate(f) for f in load("blank_gameweek.json")["fixtures"]]

        gameweeks = build_team_gameweeks(fixtures, 29, list(range(1, 21)))
        blanking = [tid for tid, gw in gameweeks.items() if gw.kind is FixtureKind.BLANK]

        assert len(fixtures) == 6
        assert len(blanking) == 8

    def test_postponement_shapes(self) -> None:
        fixtures = [Fixture.model_validate(f) for f in load("postponed_fixture.json")["fixtures"]]

        unscheduled = postponed_fixtures(fixtures)
        assert [f.id for f in unscheduled] == [400]

        gameweeks = build_team_gameweeks(fixtures, 30, list(range(1, 21)))
        assert gameweeks[7].kind is FixtureKind.PROVISIONAL


class TestSeasonBoundaries:
    def test_off_season_exits_cleanly(self) -> None:
        """No events at all. Must return None, not raise IndexError."""
        bootstrap = Bootstrap.model_validate(load("off_season_bootstrap.json"))

        assert next_deadline(bootstrap, 1_786_662_000) is None

    def test_season_end_exits_cleanly(self) -> None:
        payload = load("season_end_bootstrap.json")
        bootstrap = Bootstrap.model_validate(payload)

        # Well after the final deadline in the fixture.
        assert next_deadline(bootstrap, 2_000_000_000) is None

    def test_season_end_fixture_really_has_38_past_events(self) -> None:
        """Guards the fixture itself, so a bad edit cannot make the test vacuous."""
        bootstrap = Bootstrap.model_validate(load("season_end_bootstrap.json"))

        assert len(bootstrap.events) == 38
        assert all(event.finished for event in bootstrap.events)


class TestChipContamination:
    def test_wildcard_counts_are_readable(self) -> None:
        event = Event.model_validate(load("chip_week_event.json"))

        assert event.wildcards_played == 1_200_000
        assert event.chip_count("freehit") == 340_000
        assert event.chip_count("nonexistent_chip") == 0


class TestRuleOverride:
    def test_a_rule_override_trips_the_invariant(self) -> None:
        """FPL altered the rules for one gameweek.

        We do not model per-gameweek rule variation, so the right response is a
        loud failure and a human reading the override - not a silent guess.
        """
        event_payload = load("rule_override_event.json")
        bootstrap = Bootstrap.model_validate(
            {
                "elements": [],
                "events": [event_payload],
                "teams": [],
                "element_types": [],
                "game_config": {"settings": {"timezone": "UTC"}},
            }
        )

        results = {r.name: r for r in check_bootstrap(bootstrap)}

        assert not results["no_gameweek_rule_overrides"].passed


class TestUnderstatPreSeason:
    def test_empty_teams_array_does_not_crash(self) -> None:
        """Live right now for the 2026 season.

        `payload["teams"].items()` raises AttributeError against this payload,
        because `teams` is `[]` rather than `{}`.
        """
        payload = load("understat_empty_preseason.json")

        assert isinstance(payload["teams"], list)
        assert _coerce_teams(payload["teams"]) == {}


class TestUpdatingPage:
    @respx.mock
    def test_the_maintenance_page_is_rejected(self) -> None:
        """FPL's `/updating/` page returns **HTTP 200 with an HTML body**.

        Status-code checking alone does not catch it. The content-type assertion
        is what turns a confusing downstream parse error into a clear, named
        failure at the point of fetch.
        """
        html = (FIXTURE_DIR / "updating.html").read_text(encoding="utf-8")

        respx.get("https://fantasy.premierleague.com/api/bootstrap-static/").mock(
            return_value=httpx.Response(
                200, content=html.encode(), headers={"content-type": "text/html; charset=utf-8"}
            )
        )

        with (
            HttpClient(policy=HttpPolicy(min_host_spacing_seconds=0.0, max_attempts=1)) as client,
            pytest.raises(HttpFetchError, match="content-type"),
        ):
            client.fetch("fpl", "https://fantasy.premierleague.com/api/bootstrap-static/")

    @respx.mock
    def test_the_redirect_to_updating_is_rejected(self) -> None:
        """The other half: a 3xx pointing at the maintenance page."""
        respx.get("https://fantasy.premierleague.com/api/fixtures/").mock(
            return_value=httpx.Response(302, headers={"location": "/updating/"})
        )

        with (
            HttpClient(policy=HttpPolicy(min_host_spacing_seconds=0.0, max_attempts=1)) as client,
            pytest.raises(HttpFetchError) as exc_info,
        ):
            client.fetch("fpl", "https://fantasy.premierleague.com/api/fixtures/")

        assert "/updating/" in str(exc_info.value)
