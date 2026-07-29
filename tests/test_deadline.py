"""Deadline detection - the foundation.

SPEC section 7 puts this second in the build order and says "get it right first".
These tests cover the four states FPL's data model handles badly: pre-season, the
post-deadline window where `is_next` is stale, the off-season, and season end.
"""

from __future__ import annotations

import pytest

from fplbot.domain.deadline import (
    DeadlineInfo,
    assert_timezone_is_utc,
    due_tier,
    is_confirmed_phase,
    next_deadline,
    season_has_started,
    tier_label,
)
from fplbot.models.fpl import Bootstrap

from .conftest import DEADLINE_EPOCH, HOUR


class TestNextDeadline:
    def test_finds_the_next_deadline(self, bootstrap: Bootstrap) -> None:
        now = DEADLINE_EPOCH - 10 * HOUR
        info = next_deadline(bootstrap, now)

        assert info is not None
        assert info.gameweek == 1
        assert info.seconds_remaining == 10 * HOUR

    def test_ignores_a_stale_is_next_flag(self, bootstrap: Bootstrap) -> None:
        """The post-deadline window.

        There is a real interval after a deadline passes during which FPL has not
        yet advanced `is_next`. Trusting the flag alone gives a deadline in the
        past and a negative time remaining, which then fails every tier test
        silently rather than loudly.
        """
        # One hour AFTER gameweek 1's deadline. `is_next` still points at GW1.
        now = DEADLINE_EPOCH + HOUR
        info = next_deadline(bootstrap, now)

        assert info is not None
        assert info.gameweek == 2, "should recompute rather than trust the stale flag"
        assert info.seconds_remaining > 0

    def test_returns_none_when_no_future_deadline(self, bootstrap_payload: dict) -> None:
        """Season end. Must be a clean exit, not an exception."""
        bootstrap = Bootstrap.model_validate(bootstrap_payload)
        far_future = DEADLINE_EPOCH + 400 * 24 * HOUR

        assert next_deadline(bootstrap, far_future) is None

    def test_returns_none_in_the_off_season(self, bootstrap_payload: dict) -> None:
        """No events published yet.

        The naive `[e for e in events if e['is_next']][0]` raises IndexError here
        - in July, in production, on the first deploy.
        """
        bootstrap_payload["events"] = []
        bootstrap = Bootstrap.model_validate(bootstrap_payload)

        assert next_deadline(bootstrap, DEADLINE_EPOCH) is None

    def test_handles_is_next_absent_entirely(self, bootstrap_payload: dict) -> None:
        for event in bootstrap_payload["events"]:
            event["is_next"] = False
        bootstrap = Bootstrap.model_validate(bootstrap_payload)

        info = next_deadline(bootstrap, DEADLINE_EPOCH - HOUR)

        assert info is not None
        assert info.gameweek == 1, "the epoch fallback must find it without the flag"

    def test_arithmetic_is_integer_seconds(self, bootstrap: Bootstrap) -> None:
        """No date arithmetic anywhere, so DST cannot participate."""
        info = next_deadline(bootstrap, DEADLINE_EPOCH - 47 * HOUR)

        assert info is not None
        assert isinstance(info.seconds_remaining, int)
        assert info.seconds_remaining == 47 * HOUR


class TestDueTier:
    @pytest.mark.parametrize(
        ("hours_remaining", "expected"),
        [
            (72, None),  # outside the window
            (49, None),  # the old 48h tier no longer fires
            (25, None),  # just outside 24h
            (23.5, "24h"),  # crossed the planning tier
            (4, "24h"),  # still the loosest uncrossed tier
            (2.5, "3h"),  # team news has landed - the one to act on
            (0.5, "3h"),
        ],
    )
    def test_tier_selection(self, hours_remaining: float, expected: str | None) -> None:
        assert due_tier(int(hours_remaining * HOUR)) == expected

    def test_returns_the_tightest_unsent_tier(self) -> None:
        """A late run should send current advice, not replay a stale view.

        If the 24h run already fired and we are now at T-2h, the right answer is
        the 3h tier - the one with the freshest team news - not a repeat of the
        planning report.
        """
        assert due_tier(2 * HOUR, already_sent={"24h"}) == "3h"

    def test_none_after_the_deadline(self) -> None:
        assert due_tier(-HOUR) is None
        assert due_tier(0) is None

    def test_respects_already_sent(self) -> None:
        assert due_tier(int(1.5 * HOUR), already_sent={"24h", "3h"}) is None


class TestPhase:
    def test_only_the_three_hour_tier_is_confirmed(self) -> None:
        """The T-24h report is provisional and must be labelled as such.

        For a Saturday 11:00 deadline it lands Friday 11:00, ahead of some of the
        Friday-afternoon press conferences that resolve 'Currently Being
        Assessed'. By T-3h those have happened.
        """
        assert is_confirmed_phase("3h") is True
        assert is_confirmed_phase("24h") is False
        assert is_confirmed_phase("48h") is False

    def test_tier_labels_are_stable(self) -> None:
        """The label is part of the idempotency key.

        Changing the format would orphan every existing lock and let a duplicate
        email through, so this is pinned deliberately.
        """
        assert tier_label(48 * HOUR) == "48h"
        assert tier_label(24 * HOUR) == "24h"
        assert tier_label(3 * HOUR) == "3h"


class TestSeasonGate:
    def test_pre_season_is_detected(self, bootstrap: Bootstrap) -> None:
        """The gate protecting us from mixing two seasons' data.

        In pre-season `minutes`, `total_points`, `bps` and `starts` hold LAST
        season's values while `form` and the transfer counters are zero.
        """
        assert season_has_started(bootstrap, DEADLINE_EPOCH - HOUR) is False

    def test_in_season_is_detected(self, bootstrap: Bootstrap) -> None:
        assert season_has_started(bootstrap, DEADLINE_EPOCH + HOUR) is True


class TestTimezoneAssertion:
    def test_passes_on_utc(self, bootstrap: Bootstrap) -> None:
        assert_timezone_is_utc(bootstrap)  # must not raise

    def test_raises_loudly_on_anything_else(self, bootstrap_payload: dict) -> None:
        """Every epoch assumption rests on this, so it must scream, not degrade."""
        bootstrap_payload["game_config"]["settings"]["timezone"] = "Europe/London"
        bootstrap = Bootstrap.model_validate(bootstrap_payload)

        with pytest.raises(RuntimeError, match="expected 'UTC'"):
            assert_timezone_is_utc(bootstrap)


class TestDeadlineInfo:
    def test_within_window(self, bootstrap: Bootstrap) -> None:
        event = bootstrap.events[0]

        assert DeadlineInfo(event, 23 * HOUR).within_window is True
        # The window is derived from the loosest tier, which is now 24h -
        # 47h was inside it when a 48h tier existed.
        assert DeadlineInfo(event, 47 * HOUR).within_window is False
        assert DeadlineInfo(event, -HOUR).within_window is False
