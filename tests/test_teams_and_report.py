"""Team alias resolution and report rendering."""

from __future__ import annotations

import re

import numpy as np

from fplbot.config import TUNABLES
from fplbot.domain.captaincy import build_captain_picks
from fplbot.domain.ranking import Board, build_buy_board
from fplbot.domain.squad import optimise_squad
from fplbot.domain.teams import assert_known_teams, canonical_team, resolve_team_id
from fplbot.models.domain import (
    AvailabilitySignal,
    DataQuality,
    Distribution,
    FixtureKind,
    PlayerScore,
    RunContext,
    SourceState,
)
from fplbot.report.email import build_message, build_subject
from fplbot.report.render import (
    COLOURS,
    esc,
    render_failure_html,
    render_html,
    render_text,
)
from tests.test_squad import a_pool as a_squad_pool


class TestTeamAliases:
    def test_common_aliases_resolve(self) -> None:
        """Never fuzzy-match team names.

        "Manchester City" and "Manchester United" are 82% similar by token ratio,
        and confusing them would corrupt every fixture in the gameweek. Twenty
        names is a lookup table, not a matching problem.
        """
        assert canonical_team("Man United") == "Man Utd"
        assert canonical_team("Manchester United") == "Man Utd"
        assert canonical_team("Tottenham Hotspur") == "Spurs"
        assert canonical_team("Nottingham Forest") == "Nott'm Forest"

    def test_the_two_manchester_clubs_stay_distinct(self) -> None:
        assert canonical_team("Man City") == "Man City"
        assert canonical_team("Man Utd") == "Man Utd"
        assert canonical_team("Man City") != canonical_team("Man Utd")

    def test_typographic_apostrophes_are_folded(self) -> None:
        assert canonical_team("Nott’m Forest") == "Nott'm Forest"

    def test_unknown_names_return_none(self) -> None:
        assert canonical_team("Real Madrid") is None

    def test_resolves_to_an_fpl_team_id(self) -> None:
        teams_by_name = {"Man Utd": 14, "Spurs": 18}

        assert resolve_team_id("Manchester United", teams_by_name) == 14
        assert resolve_team_id("Tottenham", teams_by_name) == 18
        assert resolve_team_id("Barcelona", teams_by_name) is None

    def test_promotion_surfaces_loudly(self) -> None:
        """This fires every August when promoted clubs arrive - by design.

        Two minutes updating a lookup table beats twenty quietly unmatched players.
        """
        unknown = assert_known_teams(["Arsenal", "Some Promoted Club"])

        assert unknown == ["Some Promoted Club"]


def a_score(element_id: int, **overrides) -> PlayerScore:
    rng = np.random.default_rng(element_id)
    defaults = {
        "element_id": element_id,
        "name": f"Player{element_id}",
        "team_short": "ARS",
        "team_id": 1,
        "position": "MID",
        "price": 8.0,
        "ownership": 12.0,
        "distribution": Distribution(samples=np.clip(rng.normal(6, 3, 2000), 0, None)),
        "availability": AvailabilitySignal(element_id=element_id, risk=0.05),
        "fixture_kind": FixtureKind.NORMAL,
        "fixture_count": 1,
        "components": {"goals": 2.5, "assists": 1.0, "clean_sheet": 0.4},
        "opponents": ["CHE(H)"],
        "ep_next": 5.0,
    }
    defaults.update(overrides)
    return PlayerScore(**defaults)


def a_context(*, confirmed: bool = False) -> RunContext:
    quality = DataQuality()
    quality.record("fpl", SourceState.OK)
    quality.record("understat", SourceState.DEGRADED, "timeout", age_seconds=7200)
    quality.add_caveat("Understat unavailable; using cached data from 2.0 hours ago.")

    return RunContext(
        now_epoch=1_786_575_600,
        season="2026-27",
        gameweek=3,
        deadline_epoch=1_786_662_000,
        seconds_to_deadline=86_400,
        tier="3h" if confirmed else "24h",
        is_confirmed_phase=confirmed,
        season_has_started=True,
        data_quality=quality,
    )


class TestRendering:
    def test_html_contains_every_required_section(self) -> None:
        board = Board(
            buys_by_position=build_buy_board([a_score(1), a_score(2)], TUNABLES),
            sells=[],
            watchlist=[],
            returning=[],
        )

        html = render_html(a_context(), board)

        for heading in (
            "Buy board",
            "Captain picks",
            "Sell / avoid",
            "Injury-signal watchlist",
            "Returning from injury",
            "Best wildcard squad",
            "Caveats",
        ):
            assert heading in html

    def test_provisional_phase_is_prominent(self) -> None:
        """The T-24h report must be labelled as provisional, and must point at
        the confirmed one that follows."""
        board = Board({}, [], [], [])

        html = render_html(a_context(confirmed=False), board)

        assert "PROVISIONAL" in html
        assert "T-3h" in html

    def test_confirmed_phase_says_act_on_this(self) -> None:
        board = Board({}, [], [], [])

        html = render_html(a_context(confirmed=True), board)

        assert "CONFIRMED" in html
        assert "act on" in html

    def test_data_quality_names_degraded_sources(self) -> None:
        board = Board({}, [], [], [])

        html = render_html(a_context(), board)

        assert "understat" in html
        assert "Degraded" in html or "degraded" in html

    def test_user_content_is_escaped(self) -> None:
        """Injury `news` comes from third-party HTML; a stray bracket must not
        be able to break the layout."""
        score = a_score(1, name="<script>alert('x')</script>")
        board = Board(build_buy_board([score], TUNABLES), [], [], [])

        html = render_html(a_context(), board)

        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_esc_handles_non_strings(self) -> None:
        assert esc(42) == "42"
        assert esc(None) == "None"

    def test_text_alternative_is_produced(self) -> None:
        """Worth the effort: some clients prefer it, and a message with a real
        text part is markedly less likely to be treated as spam."""
        board = Board(build_buy_board([a_score(1)], TUNABLES), [], [], [])

        text = render_text(a_context(), board)

        assert "GAMEWEEK 3 TRANSFER BOARD" in text
        assert "BUY BOARD" in text
        assert "Why:" in text
        assert "<" not in text.replace("<-", "")

    def test_failure_email_explains_itself(self) -> None:
        """We email about the failure rather than about transfers.

        Silence would be worse - the user would assume the bot ran and found
        nothing worth saying.
        """
        html = render_failure_html(a_context(), "data is 30 hours old")

        assert "could not produce a reliable board" in html
        assert "30 hours old" in html
        assert "Source status" in html


class TestSpreadChart:
    """The chart is table cells with percentage widths - no JS, no SVG, no image.

    Gmail strips <svg> and refuses `data:` URIs, and a server-rendered PNG would
    mean carrying matplotlib for one picture. These tests pin the arithmetic, not
    the markup.
    """

    def test_chart_appears_with_the_buy_board(self) -> None:
        board = Board(build_buy_board([a_score(1), a_score(2)], TUNABLES), [], [], [])

        html = render_html(a_context(), board)

        assert "Outcome spread" in html
        assert "floor to mean" in html

    def test_chart_is_omitted_when_there_is_nothing_to_plot(self) -> None:
        """An empty chart frame reads as a rendering failure, so draw nothing."""
        html = render_html(a_context(), Board({}, [], [], []))

        assert "Outcome spread" not in html

    def test_segment_widths_never_exceed_the_track(self) -> None:
        """Widths are percentages of one shared scale. Summing past 100% would
        push the tail segment onto a second row and break every bar."""
        scores = [a_score(i, price=6.0 + i) for i in range(1, 7)]
        board = Board(build_buy_board(scores, TUNABLES), [], [], [])

        html = render_html(a_context(), board)

        # Each bar is one <tr> of segments inside a fixed-layout table.
        for row in re.findall(r'table-layout:fixed.*?<tr>(.*?)</tr>', html, re.S):
            widths = [float(w) for w in re.findall(r'width="([\d.]+)%"', row)]
            assert widths
            assert sum(widths) <= 100.01

    def test_the_scale_is_shared_across_players(self) -> None:
        """Per-row scaling would make a narrow spread look as wide as a broad one,
        which inverts the only thing the chart exists to communicate."""
        wide = a_score(1, distribution=Distribution(samples=np.linspace(0.0, 20.0, 2000)))
        narrow = a_score(2, distribution=Distribution(samples=np.linspace(4.0, 6.0, 2000)))
        board = Board(build_buy_board([wide, narrow], TUNABLES), [], [], [])

        html = render_html(a_context(), board)
        rows = re.findall(r'table-layout:fixed.*?<tr>(.*?)</tr>', html, re.S)

        def coloured_width(row: str) -> float:
            return sum(
                float(w)
                for w, colour in re.findall(r'width="([\d.]+)%"[^>]*background:(#[0-9a-f]+)', row)
                if colour != COLOURS["track"]
            )

        assert len(rows) == 2
        assert coloured_width(rows[0]) > coloured_width(rows[1])


class TestCaptainSection:
    def test_reports_the_doubled_numbers(self) -> None:
        """The armband doubles the score, so the doubled figures are what we show.

        Printing the single-score numbers here would make the reader do the
        multiplication themselves, and the doubling is the entire point.
        """
        picks = build_captain_picks([a_score(1)], TUNABLES)
        board = Board({}, [], [], [], captains=picks)

        html = render_html(a_context(), board)

        assert "Captained xP" in html
        assert f"{picks[0].expected_points:.2f}" in html

    def test_states_the_squad_caveat(self) -> None:
        """You can only captain someone you already own - the bot cannot know that."""
        board = Board({}, [], [], [], captains=build_captain_picks([a_score(1)], TUNABLES))

        html = render_html(a_context(), board)

        assert "only captain someone you already" in html

    def test_labels_the_template_pick(self) -> None:
        picks = build_captain_picks([a_score(1)], TUNABLES, most_captained_element=1)
        board = Board({}, [], [], [], captains=picks)

        assert "TEMPLATE" in render_html(a_context(), board)

    def test_empty_captains_degrade_gracefully(self) -> None:
        html = render_html(a_context(), Board({}, [], [], [], captains=[]))

        assert "Captain picks" in html
        assert "No captain candidates" in html


class TestWildcardSection:
    def test_renders_the_squad(self) -> None:
        squad = optimise_squad(a_squad_pool(), TUNABLES, seed=1)
        board = Board({}, [], [], [], wildcard=squad, horizon_note="Projected over 20 gameweeks.")

        html = render_html(a_context(), board)

        assert "Best wildcard squad" in html
        assert squad is not None
        assert squad.formation in html
        assert "In the bank" in html
        assert "Bench (in autosub order)" in html

    def test_explains_why_the_bench_is_cheap(self) -> None:
        """Otherwise a reader reasonably assumes the optimiser made a mistake."""
        squad = optimise_squad(a_squad_pool(), TUNABLES, seed=1)
        board = Board({}, [], [], [], wildcard=squad)

        html = render_html(a_context(), board)

        assert "only eleven players score" in html

    def test_absent_squad_degrades_gracefully(self) -> None:
        """Expected in pre-season, when there are no projections to optimise."""
        html = render_html(a_context(), Board({}, [], [], [], wildcard=None))

        assert "Best wildcard squad" in html
        assert "No wildcard squad could be built" in html

    def test_text_alternative_includes_both_new_sections(self) -> None:
        squad = optimise_squad(a_squad_pool(), TUNABLES, seed=1)
        board = Board(
            {},
            [],
            [],
            [],
            captains=build_captain_picks([a_score(1)], TUNABLES),
            wildcard=squad,
            horizon_note="Projected over 20 gameweeks.",
        )

        text = render_text(a_context(), board)

        assert "CAPTAIN PICKS" in text
        assert "BEST WILDCARD SQUAD" in text
        assert "captained" in text
        assert "bench (autosub order)" in text
        assert "<" not in text.replace("<-", "")


class TestEmailAssembly:
    def test_multipart_orders_text_before_html(self) -> None:
        """In multipart/alternative the LAST part is preferred.

        Getting this backwards makes every capable client show the plain text.
        """
        message = build_message("Subject", "<p>html</p>", "text", "a@b.com", ["c@d.com"])
        parts = message.get_payload()

        assert parts[0].get_content_type() == "text/plain"
        assert parts[1].get_content_type() == "text/html"

    def test_headers_are_set(self) -> None:
        message = build_message("S", "<p>h</p>", "t", "from@x.com", ["one@x.com", "two@x.com"])

        # From carries a display name, so the address is a substring rather than
        # the whole header: "fplBot <from@x.com>".
        assert "from@x.com" in message["From"]
        assert "fplBot" in message["From"]
        assert "one@x.com" in message["To"]
        assert "two@x.com" in message["To"]

    def test_deliverability_headers_are_present(self) -> None:
        """Gmail's bulk-sender guidance names the unsubscribe pair explicitly, and
        Date/Message-ID are what a well-formed message is expected to arrive with.

        None of this rescues an unauthenticated @gmail.com sender - see the
        module docstring - but their absence is a needless negative signal.
        """
        message = build_message("S", "<p>h</p>", "t", "bot@example.com", ["you@example.com"])

        assert message["Reply-To"] == "bot@example.com"
        assert message["Date"]
        assert message["Message-ID"].endswith("@example.com>")
        assert message["Auto-Submitted"] == "auto-generated"
        assert message["List-Unsubscribe"] == "<mailto:bot@example.com?subject=unsubscribe>"
        assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        assert "example.com" in message["List-Id"]

    def test_subject_leads_with_the_phase(self) -> None:
        """Often all that gets read on a phone, and FINAL vs provisional is the
        single most decision-relevant bit we have."""
        assert build_subject(5, "3h", True, "Haaland (MCI)").startswith("fplBot GW5 (FINAL, T-3h)")
        assert "provisional" in build_subject(5, "48h", False, None)

    def test_subject_includes_the_top_pick(self) -> None:
        assert "Haaland (MCI)" in build_subject(5, "3h", True, "Haaland (MCI)")
