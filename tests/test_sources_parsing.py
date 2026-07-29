"""Parsing tests for the third-party sources.

All against synthetic payloads that reproduce the documented structure - and,
more importantly, the documented traps. No network access anywhere.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from fplbot.config import HttpPolicy
from fplbot.http.client import HttpClient, HttpFetchError
from fplbot.sources.clubelo import _parse_fixture_row
from fplbot.sources.ffs import PHOTO_CODE_PATTERN, parse_lineups
from fplbot.sources.oddsapi import _parse_featured, match_probabilities
from fplbot.sources.premierinjuries import parse_injury_table, parse_uk_date
from fplbot.sources.understat import UnderstatData, _coerce_teams, _to_float, forecast_probabilities

# ---------------------------------------------------------------------------
# Fantasy Football Scout
# ---------------------------------------------------------------------------
FFS_HTML = """
<html><body>
  <div class="best-xi-widget">
    <ul class="row-1 !m-0"><li><img src="/photos/players/110x140/999999.png"></li></ul>
  </div>
  <section>
    <h2>Arsenal</h2>
    <div class="pitch">
      <ul class="row-1 !m-0"><li><img src="/photos/players/110x140/100008.png"></li></ul>
      <ul class="row-2 !m-0">
        <li><img src="/photos/players/110x140/100004.png"></li>
        <li><img src="/photos/players/110x140/100005.png"></li>
        <li><img src="/photos/players/110x140/100010.png"></li>
        <li><img src="/photos/players/110x140/100100.png"></li>
      </ul>
      <ul class="row-3 !m-0">
        <li><img src="/photos/players/110x140/100101.png"></li>
        <li><img src="/photos/players/110x140/100102.png"></li>
        <li><img src="/photos/players/110x140/100103.png"></li>
        <li><img src="/photos/players/110x140/100104.png"></li>
      </ul>
      <ul class="row-4 !m-0">
        <li><img src="/photos/players/110x140/100001.png"></li>
        <li><img src="/photos/players/110x140/100105.png"></li>
      </ul>
    </div>
  </section>
</body></html>
"""


class TestFfsLineups:
    def test_photo_code_regex_finds_every_code(self) -> None:
        """The primary path deliberately regexes raw HTML rather than the DOM.

        The page's CSS classes are volatile Tailwind (`class="!m-0"` above), so
        anchoring selectors on them invites breakage from a redesign that changes
        nothing meaningful. A regex over the image URL survives almost anything.
        """
        codes = [int(m.group(1)) for m in PHOTO_CODE_PATTERN.finditer(FFS_HTML)]

        assert 100001 in codes
        assert len(codes) == 12  # 11 in the lineup plus one in the best-XI widget

    def test_codes_join_exactly_to_element_ids(self) -> None:
        """An exact integer join - immune to every naming problem."""
        code_map = {100001: 1, 100004: 4, 100005: 5, 100008: 8, 100010: 10}

        data = parse_lineups(FFS_HTML, code_map)

        assert 1 in data.predicted_starters
        assert 4 in data.predicted_starters
        assert data.is_predicted_to_start(5) is True

    def test_unknown_codes_are_counted_not_guessed(self) -> None:
        data = parse_lineups(FFS_HTML, {100001: 1})

        assert data.codes_seen > data.codes_matched

    def test_formation_falls_out_of_the_row_cardinalities(self) -> None:
        """`ul.row-1` is the keeper band, `row-2..N` the outfield bands."""
        code_map = {100000 + i: i for i in range(200)}

        data = parse_lineups(FFS_HTML, code_map)

        lineup = next((line for line in data.lineups if line.formation_rows), None)
        assert lineup is not None
        assert lineup.formation_rows[0] == 1, "row-1 is the goalkeeper"
        assert lineup.formation == "4-4-2"

    def test_survives_a_complete_dom_change(self) -> None:
        """If the markup is unrecognisable, pass 1 must still deliver the codes."""
        mangled = '<div><img src="/photos/players/110x140/100001.png"></div>'

        data = parse_lineups(mangled, {100001: 1})

        assert 1 in data.predicted_starters


# ---------------------------------------------------------------------------
# PremierInjuries
# ---------------------------------------------------------------------------
INJURY_HTML = """
<table>
  <tr class="sub-head"><td>Player</td><td>Status</td><td>Condition</td>
      <td>Reason</td><td>Potential Return</td></tr>
  <tr class="heading" data-team-id="12"><td>Newcastle</td></tr>
  <tr class="player-row team_12">
    <td><div class="mob-title">Player</div>
        <a class="track" data-type="player" data-id="4471" data-name="Callum Wilson">
          Callum Wilson</a></td>
    <td><div class="mob-title">Status</div>50%</td>
    <td><div class="mob-title">Condition</div>Currently Being Assessed</td>
    <td><div class="mob-title">Reason</div>Hamstring</td>
    <td><div class="mob-title">Potential Return</div>05/09/2026</td>
  </tr>
  <tr class="player-row team_12">
    <td><div class="mob-title">Player</div>
        <a class="track" data-type="player" data-id="9912" data-name="Banned Player">
          Banned Player</a></td>
    <td><div class="mob-title">Status</div>Ruled Out</td>
    <td><div class="mob-title">Condition</div>Not Available</td>
    <td><div class="mob-title">Reason</div>Suspended</td>
    <td><div class="mob-title">Potential Return</div>12/09/2026</td>
  </tr>
</table>
"""


class TestPremierInjuries:
    def test_mob_title_labels_are_stripped(self) -> None:
        """THE trap in this source.

        Every `<td>` is prefixed with `<div class="mob-title">Label</div>` for
        the mobile layout. A naive `.text()` yields "StatusRuled Out" - a
        plausible-looking string that is subtly wrong, so nothing throws.
        """
        data = parse_injury_table(INJURY_HTML)
        record = data.by_source_id()["4471"]

        assert record.status == "50%"
        assert "Status" not in (record.status or "")
        assert record.condition == "Currently Being Assessed"
        assert record.reason == "Hamstring"

    def test_uses_the_stable_data_id_as_the_alias_anchor(self) -> None:
        """Resolve once, cache against the source's own id, never re-match."""
        data = parse_injury_table(INJURY_HTML)

        assert set(data.by_source_id()) == {"4471", "9912"}

    def test_prefers_data_name_over_polluted_cell_text(self) -> None:
        data = parse_injury_table(INJURY_HTML)

        assert data.by_source_id()["4471"].name == "Callum Wilson"

    def test_team_class_rather_than_document_order(self) -> None:
        """`tr.player-row.team_{id}` makes each row self-describing."""
        data = parse_injury_table(INJURY_HTML)

        assert all(record.team_id == "12" for record in data.records)

    def test_awaiting_press_conference_flag(self) -> None:
        """This defines the Phase 2 re-check set."""
        data = parse_injury_table(INJURY_HTML)

        awaiting = data.awaiting_press_conference
        assert len(awaiting) == 1
        assert awaiting[0].source_id == "4471"

    def test_suspensions_are_distinguished_from_injuries(self) -> None:
        """A ban has a deterministic end date; it is not a fitness question."""
        data = parse_injury_table(INJURY_HTML)

        assert data.by_source_id()["9912"].is_suspension is True
        assert data.by_source_id()["4471"].is_suspension is False

    def test_status_maps_onto_fpls_scale(self) -> None:
        data = parse_injury_table(INJURY_HTML)

        assert data.by_source_id()["4471"].chance_of_playing == 50
        assert data.by_source_id()["9912"].chance_of_playing == 0

    def test_vocabularies_are_captured_for_assertion(self) -> None:
        data = parse_injury_table(INJURY_HTML)

        assert data.statuses_seen == {"50%", "Ruled Out"}
        assert data.conditions_seen == {"Currently Being Assessed", "Not Available"}


class TestUkDates:
    def test_parses_day_first(self) -> None:
        """`Potential Return` is DD/MM/YYYY.

        Parsing month-first corrupts every date with a day of 12 or lower -
        roughly 40% of them - and yields another perfectly valid date, so nothing
        throws. 05/09 becomes 09/05 and the bot recommends buying four months early.
        """
        assert parse_uk_date("05/09/2026") == date(2026, 9, 5)
        assert parse_uk_date("12/09/2026") == date(2026, 9, 12)

    def test_returns_none_rather_than_guessing(self) -> None:
        assert parse_uk_date("") is None
        assert parse_uk_date("soon") is None


# ---------------------------------------------------------------------------
# Understat
# ---------------------------------------------------------------------------
class TestUnderstat:
    def test_league_data_accepts_understats_javascript_content_type(self) -> None:
        """Understat serves JSON as `text/javascript;charset=utf-8`.

        Only the header is unusual - the body is ordinary JSON. Demanding
        `application/json` rejected every response before the parser saw it and
        left the source permanently degraded in production, reported in the email
        as "understat (failed)" on a request that had actually returned 200.

        The assertion still has to reject an HTML challenge or maintenance page,
        which is the whole reason it exists, so that case is pinned here too.
        """
        client = HttpClient(policy=HttpPolicy(max_attempts=1, min_host_spacing_seconds=0))
        url = "https://understat.com/getLeagueData/EPL/2026"
        accepted = ("application/json", "text/javascript")

        with respx.mock:
            respx.get(url).mock(
                return_value=httpx.Response(
                    200,
                    content=b'{"teams":[],"players":[],"dates":[]}',
                    headers={"content-type": "text/javascript;charset=utf-8"},
                )
            )
            result = client.fetch("understat", url, expect_content_type=accepted)

        assert result.json() == {"teams": [], "players": [], "dates": []}

        with respx.mock:
            respx.get(url).mock(
                return_value=httpx.Response(
                    200,
                    content=b"<html>maintenance</html>",
                    headers={"content-type": "text/html"},
                )
            )
            with pytest.raises(HttpFetchError):
                client.fetch("understat", url, expect_content_type=accepted)

    def test_empty_teams_is_an_array_not_an_object(self) -> None:
        """The live pre-season landmine.

        `/getLeagueData/EPL/2026` currently returns `{"teams":[],...}`. Any code
        doing `payload["teams"].items()` raises AttributeError in pre-season.
        """
        assert _coerce_teams([]) == {}
        assert _coerce_teams({}) == {}
        assert _coerce_teams(None) == {}

    def test_teams_as_a_populated_array_is_normalised(self) -> None:
        result = _coerce_teams([{"id": "81", "title": "Arsenal"}])

        assert result["81"]["title"] == "Arsenal"

    def test_string_typed_floats_are_coerced(self) -> None:
        """`players[]` values are all strings, including the floats."""
        assert _to_float("0.42") == pytest.approx(0.42)
        assert _to_float("") == 0.0
        assert _to_float(None) == 0.0

    def test_forecasts_are_already_vig_free(self) -> None:
        """`dates[].forecast` gives model probabilities, not bookmaker prices.

        Pushing them through the devigger would distort perfectly good numbers -
        there is no margin to remove.
        """
        data = UnderstatData(
            forecasts=[
                {
                    "h": {"title": "Arsenal"},
                    "a": {"title": "Chelsea"},
                    "forecast": {"w": "0.55", "d": "0.25", "l": "0.20"},
                }
            ]
        )

        probabilities = forecast_probabilities(data)

        win, draw, loss = probabilities[("Arsenal", "Chelsea")]
        assert win + draw + loss == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# ClubElo
# ---------------------------------------------------------------------------
class TestClubElo:
    def test_clean_sheet_is_the_sum_of_the_scoreline_columns(self) -> None:
        """Home clean sheet = sum of R:x-0. Exactly the FPL input, for free.

        These are model probabilities and are already vig-free.
        """
        row = {
            "Home": "Arsenal",
            "Away": "Chelsea",
            "Country": "ENG",
            "R:0-0": "0.08",
            "R:1-0": "0.12",
            "R:2-0": "0.09",
            "R:0-1": "0.07",
            "R:1-1": "0.14",
            "R:2-1": "0.11",
            "R:0-2": "0.05",
        }

        result = _parse_fixture_row(row)

        assert result is not None
        # Home keeps a clean sheet when the AWAY side scores zero: 0-0, 1-0, 2-0.
        assert result.home_clean_sheet == pytest.approx(0.29)
        # Away keeps one when the HOME side scores zero: 0-0, 0-1, 0-2.
        assert result.away_clean_sheet == pytest.approx(0.20)

    def test_1x2_comes_from_summing_by_goal_difference(self) -> None:
        row = {
            "Home": "Arsenal",
            "Away": "Chelsea",
            "Country": "ENG",
            "R:0-0": "0.2",
            "R:1-0": "0.5",
            "R:0-1": "0.3",
        }

        result = _parse_fixture_row(row)

        assert result is not None
        assert result.home_win == pytest.approx(0.5)
        assert result.draw == pytest.approx(0.2)
        assert result.away_win == pytest.approx(0.3)

    def test_expected_goals_are_derived_from_the_distribution(self) -> None:
        row = {
            "Home": "Arsenal",
            "Away": "Chelsea",
            "Country": "ENG",
            "R:2-0": "0.5",
            "R:0-2": "0.5",
        }

        result = _parse_fixture_row(row)

        assert result is not None
        assert result.expected_home_goals == pytest.approx(1.0)
        assert result.expected_away_goals == pytest.approx(1.0)

    def test_rows_without_teams_are_skipped(self) -> None:
        assert _parse_fixture_row({"R:0-0": "0.5"}) is None


# ---------------------------------------------------------------------------
# The Odds API
# ---------------------------------------------------------------------------
class TestOddsApiFeaturedMarket:
    """The 1X2 market key is `h2h`, not `h2h_3_way`.

    The naming is genuinely misleading - `h2h` reads like a two-way market and
    `h2h_3_way` reads like the soccer one - so this is pinned against a payload
    shaped exactly like the live response. Requesting `h2h_3_way` returns
    422 INVALID_MARKET, which meant every odds fetch failed and the report fell
    back to ClubElo without the market anchor on goal probabilities.
    """

    pass


ODDS_PAYLOAD = [
    {
        "id": "eb2553d1",
        "sport_key": "soccer_epl",
        "commence_time": "2026-08-21T19:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Coventry City",
        "bookmakers": [
            {
                "key": "betfair",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Arsenal", "price": 1.30},
                            {"name": "Coventry City", "price": 11.0},
                            {"name": "Draw", "price": 6.0},
                        ],
                    }
                ],
            }
        ],
    }
]


class TestOddsApiParsing:
    def test_the_three_way_prices_are_read(self) -> None:
        [odds] = _parse_featured(ODDS_PAYLOAD)

        assert odds.home_win == 1.30
        assert odds.away_win == 11.0
        assert odds.draw == 6.0

    def test_probabilities_come_out_devigged(self) -> None:
        """Bookmaker prices carry margin; the implied probabilities must sum to
        one after devigging, not to the overround."""
        [odds] = _parse_featured(ODDS_PAYLOAD)

        probabilities = match_probabilities(odds)

        assert probabilities is not None
        assert sum(probabilities) == pytest.approx(1.0)
        home, _draw, away = probabilities
        assert home > away, "the 1.30 favourite must be likeliest"

    def test_the_old_market_key_no_longer_parses(self) -> None:
        """Guards the regression directly: if someone restores `h2h_3_way`,
        the request 422s and nothing is read."""
        stale = [dict(ODDS_PAYLOAD[0])]
        stale[0]["bookmakers"] = [
            {
                "key": "betfair",
                "markets": [
                    {
                        "key": "h2h_3_way",
                        "outcomes": [
                            {"name": "Arsenal", "price": 1.30},
                            {"name": "Coventry City", "price": 11.0},
                            {"name": "Draw", "price": 6.0},
                        ],
                    }
                ],
            }
        ]

        [odds] = _parse_featured(stale)

        assert odds.home_win is None
