"""Business invariants.

These are the assertions types cannot make. Pydantic can tell you a field is a
string; it cannot tell you that ownership across all players ought to sum to
about 1500 because a squad has fifteen slots.
"""

from __future__ import annotations

from fplbot.domain.invariants import (
    check_bootstrap,
    check_clubelo_fixture_columns,
    check_injury_vocabulary,
    check_understat_players,
)
from fplbot.models.domain import DataQuality
from fplbot.models.fpl import Bootstrap


def results_by_name(results) -> dict:
    return {result.name: result for result in results}


class TestBootstrapInvariants:
    def test_all_pass_on_healthy_data(self, bootstrap: Bootstrap) -> None:
        results = results_by_name(check_bootstrap(bootstrap))

        assert results["ownership_sums_to_1500"].passed
        assert results["twenty_teams"].passed
        assert results["thirty_eight_events"].passed
        assert results["no_gameweek_rule_overrides"].passed
        assert results["unique_photo_codes"].passed

    def test_ownership_canary_catches_a_unit_change(self, bootstrap_payload: dict) -> None:
        """The beautiful one.

        If ownership were ever expressed as a fraction rather than a percentage,
        the payload would still parse perfectly and every rank-attacking
        calculation would be silently wrong. This catches it with one comparison.
        """
        for element in bootstrap_payload["elements"]:
            element["selected_by_percent"] = str(float(element["selected_by_percent"]) / 100)
        bootstrap = Bootstrap.model_validate(bootstrap_payload)

        results = results_by_name(check_bootstrap(bootstrap))

        assert not results["ownership_sums_to_1500"].passed

    def test_catches_a_short_team_list(self, bootstrap_payload: dict) -> None:
        bootstrap_payload["teams"] = bootstrap_payload["teams"][:18]
        bootstrap = Bootstrap.model_validate(bootstrap_payload)

        results = results_by_name(check_bootstrap(bootstrap))

        assert not results["twenty_teams"].passed

    def test_catches_a_gameweek_rule_override(self, bootstrap_payload: dict) -> None:
        """A non-empty `overrides.rules` means FPL changed the rules mid-season.

        We do not model per-gameweek rule variation, so the correct response is a
        human reading the override rather than the bot guessing.
        """
        bootstrap_payload["events"][5]["overrides"] = {"rules": {"squad_squadsize": 16}}
        bootstrap = Bootstrap.model_validate(bootstrap_payload)

        results = results_by_name(check_bootstrap(bootstrap))

        assert not results["no_gameweek_rule_overrides"].passed
        assert "6" in results["no_gameweek_rule_overrides"].detail

    def test_catches_duplicate_photo_codes(self, bootstrap_payload: dict) -> None:
        """A duplicate would silently attach one player's lineup to another's stats."""
        bootstrap_payload["elements"][1]["code"] = bootstrap_payload["elements"][0]["code"]
        bootstrap = Bootstrap.model_validate(bootstrap_payload)

        results = results_by_name(check_bootstrap(bootstrap))

        assert not results["unique_photo_codes"].passed

    def test_failures_are_recorded_in_data_quality(self, bootstrap_payload: dict) -> None:
        """A failed invariant reaches the reader, not just the logs."""
        bootstrap_payload["teams"] = bootstrap_payload["teams"][:5]
        bootstrap = Bootstrap.model_validate(bootstrap_payload)
        quality = DataQuality()

        check_bootstrap(bootstrap, quality)

        assert any("twenty_teams" in failure for failure in quality.invariant_failures)


class TestSourceSchemaAssertions:
    def test_understat_keys_are_checked(self) -> None:
        quality = DataQuality()

        assert check_understat_players([{"id": "1", "player_name": "X"}], quality) is False
        assert quality.invariant_failures

    def test_empty_understat_is_acceptable(self) -> None:
        """Legitimately empty in pre-season - not a failure."""
        assert check_understat_players([]) is True

    def test_clubelo_column_count(self) -> None:
        assert check_clubelo_fixture_columns([f"c{i}" for i in range(44)]) is True
        assert check_clubelo_fixture_columns([f"c{i}" for i in range(30)]) is False

    def test_injury_vocabulary_is_closed(self) -> None:
        """An unrecognised value is a semantic change, not a parse error.

        Better to map it by hand than to let it fall through to a default of
        'fit'.
        """
        assert check_injury_vocabulary({"Ruled Out", "50%"}, {"Not Available"}) is True
        assert check_injury_vocabulary({"Probably Fine"}, {"Not Available"}) is False
        assert check_injury_vocabulary({"50%"}, {"Vibes Based Assessment"}) is False
