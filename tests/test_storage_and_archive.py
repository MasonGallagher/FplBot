"""Storage helpers and the historical archive parser.

Both modules are mostly thin adapters, but each contains one genuinely
trap-laden piece of logic that is worth pinning:

* DynamoDB has no floating-point type. Getting the `float` <-> `Decimal`
  conversion wrong is the most common DynamoDB papercut in Python, and it fails
  at write time with an unhelpful error.
* `vaastav`'s `fixtures.csv` embeds the FPL `stats` blob as a **Python repr
  string**, not JSON. `json.loads` fails on it, and the tempting workaround is
  `eval`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from fplbot.sources.vaastav import _parse_gameweek_row, defensive_contribution_priors
from fplbot.storage.dynamo import _from_dynamo, _to_dynamo
from fplbot.storage.keys import Keys, iso_from_epoch, iso_now
from fplbot.storage.s3 import RawArchive, _slugify


class TestDynamoConversion:
    def test_floats_become_decimals_via_their_string_form(self) -> None:
        """`Decimal(0.1)` is 0.1000000000000000055511151231257827.

        `Decimal("0.1")` is 0.1. Always go via the string, or you store binary
        floating-point noise in a decimal column and it never quite compares equal.
        """
        assert _to_dynamo(0.1) == Decimal("0.1")
        # ruff rightly flags `Decimal(<float>)`. That is the entire point of this
        # assertion: it demonstrates the hazard the conversion exists to avoid.
        assert _to_dynamo(0.1) != Decimal(0.1)  # noqa: RUF032

    def test_conversion_is_recursive(self) -> None:
        result = _to_dynamo({"a": [1.5, {"b": 2.5}], "c": (3.5,)})

        assert result["a"][0] == Decimal("1.5")
        assert result["a"][1]["b"] == Decimal("2.5")
        assert result["c"][0] == Decimal("3.5")

    def test_integer_ness_survives_the_round_trip(self) -> None:
        """`selected` counts are integers.

        Turning them into floats on the way out makes every log line uglier and
        every equality comparison riskier.
        """
        assert _from_dynamo(Decimal("42")) == 42
        assert isinstance(_from_dynamo(Decimal("42")), int)
        assert _from_dynamo(Decimal("42.5")) == 42.5
        assert isinstance(_from_dynamo(Decimal("42.5")), float)

    def test_round_trip_preserves_values(self) -> None:
        original = {"price": 5.5, "owners": 120_000, "name": "Haaland", "flags": [True, False]}

        assert _from_dynamo(_to_dynamo(original)) == original

    def test_empty_strings_are_preserved(self) -> None:
        """DynamoDB has allowed empty string values since 2020.

        The old "must be null" advice is out of date, and following it would lose
        information here: FPL uses `''` meaningfully - a fit player's
        `chance_of_playing_next_round` is `''`, not null.
        """
        assert _to_dynamo({"chance": ""}) == {"chance": ""}


class TestKeys:
    def test_iso_sorts_chronologically(self) -> None:
        """The entire reason for ISO-8601 sort keys.

        Lexicographic order equals chronological order, so a range query over
        "everything since yesterday" needs no filtering and no sorting in Python.
        """
        stamps = [iso_from_epoch(e) for e in (1_700_000_000, 1_800_000_000, 1_600_000_000)]

        assert sorted(stamps) == [stamps[2], stamps[0], stamps[1]]

    def test_iso_now_is_fixed_width_utc(self) -> None:
        stamp = iso_now()

        assert stamp.endswith("Z")
        assert len(stamp) == 20
        datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    def test_prefixes_keep_namespaces_apart(self) -> None:
        """Single-table design: five item types, distinguished by the pk prefix."""
        keys = {
            Keys.snapshot_pk("2026-27"),
            Keys.player_pk("2026-27", 1),
            Keys.notify_pk("2026-27", 1, "3h"),
            Keys.lkg_pk("2026-27"),
            Keys.alias_pk("understat"),
        }

        assert len(keys) == 5, "no two item types may share a partition key"

    def test_the_notify_key_has_no_time_component(self) -> None:
        """Keyed on gameweek and tier, NEVER on wall-clock time.

        A retry has a different timestamp but the same gameweek and tier - which
        is exactly what makes the conditional write suppress it.
        """
        first = Keys.notify_pk("2026-27", 12, "3h")
        second = Keys.notify_pk("2026-27", 12, "3h")

        assert first == second
        assert first == "NOTIFY#2026-27#12#3h"

    def test_seasons_are_separated(self) -> None:
        assert Keys.snapshot_pk("2026-27") != Keys.snapshot_pk("2027-28")


class TestArchiveKeys:
    def test_keys_are_date_partitioned(self) -> None:
        """Date-partitioned because that is how you query it, and because a
        lifecycle rule to Glacier is then a one-line prefix rule."""
        archive = RawArchive.__new__(RawArchive)
        archive._bucket = "bucket"
        archive._season = "2026-27"

        key = archive.key_for(
            "fpl",
            "https://fantasy.premierleague.com/api/bootstrap-static/",
            when=datetime(2026, 8, 15, 11, 7, 0, tzinfo=UTC),
            ext="json",
        )

        assert key.startswith("raw/fpl/2026/08/15/")
        assert key.endswith(".json.gz")
        assert "bootstrap-static" in key

    def test_slugify_is_safe_and_bounded(self) -> None:
        assert _slugify("api/bootstrap-static/") == "api-bootstrap-static"
        assert _slugify("!!!") == "root"
        assert len(_slugify("x" * 300)) <= 80


class TestVaastavParsing:
    def test_gameweek_rows_join_on_the_fpl_element_id(self) -> None:
        """`gws/gwN.csv` carries `element` - an exact join, no name matching."""
        row = _parse_gameweek_row(
            {
                "element": "233",
                "name": "Mohamed Salah",
                "position": "MID",
                "team": "Liverpool",
                "round": "12",
                "minutes": "90",
                "total_points": "13",
                "goals_scored": "2",
                "assists": "1",
                "clean_sheets": "0",
                "bps": "62",
                "bonus": "3",
                "value": "130",
                "selected": "4500000",
                "transfers_in": "120000",
                "transfers_out": "8000",
                "transfers_balance": "112000",
                "was_home": "True",
                "opponent_team": "7",
                "expected_goals": "1.24",
                "expected_assists": "0.41",
                "defensive_contribution": "4",
            }
        )

        assert row.element == 233
        assert row.was_home is True
        assert row.expected_goals == pytest.approx(1.24)
        assert row.defensive_contribution == 4

    def test_malformed_values_do_not_raise(self) -> None:
        """One bad row must not sink a season's load."""
        row = _parse_gameweek_row({"element": "", "minutes": "n/a", "was_home": ""})

        assert row.element == 0
        assert row.minutes == 0
        assert row.was_home is False

    def test_defcon_priors_need_a_meaningful_sample(self) -> None:
        """The whole point of the archive.

        All five DefCon fields in the live FPL API are zero for every player, so
        without this there is no prior at all for GW1-5. Players under 450
        minutes are omitted rather than given a noisy rate.
        """

        def rows(element: int, minutes: int, defcon: int, count: int):
            return [
                _parse_gameweek_row(
                    {
                        "element": str(element),
                        "minutes": str(minutes),
                        "defensive_contribution": str(defcon),
                    }
                )
                for _ in range(count)
            ]

        gameweeks = {
            # 900 minutes total - a real sample.
            1: rows(1, 90, 10, 1)[0:1] + rows(2, 90, 12, 1)[0:1],
        }
        for week in range(2, 11):
            gameweeks[week] = rows(1, 90, 10, 1)[0:1] + rows(2, 30, 4, 1)[0:1]

        priors = defensive_contribution_priors(gameweeks)

        assert 1 in priors, "900 minutes is a usable sample"
        assert priors[1] == pytest.approx(10.0)
        assert 2 not in priors, "under 450 minutes should be omitted, not guessed"
