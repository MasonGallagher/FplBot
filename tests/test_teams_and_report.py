"""Team alias resolution and report rendering."""

from __future__ import annotations

import numpy as np

from fplbot.config import TUNABLES
from fplbot.domain.ranking import Board, build_buy_board
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
from fplbot.report.render import esc, render_failure_html, render_html, render_text


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
        tier="3h" if confirmed else "48h",
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
            "Sell / avoid",
            "Injury-signal watchlist",
            "Returning from injury",
            "Caveats",
        ):
            assert heading in html

    def test_provisional_phase_is_prominent(self) -> None:
        """Phase 1 output must be labelled as such.

        It fires before most managers' press conferences, when over half the
        injury table is still 'Currently Being Assessed'.
        """
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

        assert message["From"] == "from@x.com"
        assert "one@x.com" in message["To"]
        assert "two@x.com" in message["To"]

    def test_subject_leads_with_the_phase(self) -> None:
        """Often all that gets read on a phone, and FINAL vs provisional is the
        single most decision-relevant bit we have."""
        assert build_subject(5, "3h", True, "Haaland (MCI)").startswith("fplBot GW5 (FINAL, T-3h)")
        assert "provisional" in build_subject(5, "48h", False, None)

    def test_subject_includes_the_top_pick(self) -> None:
        assert "Haaland (MCI)" in build_subject(5, "3h", True, "Haaland (MCI)")
