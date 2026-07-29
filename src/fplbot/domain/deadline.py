"""Deadline detection and notification tiering.

The rule that governs this entire module: **all deadline arithmetic is integer
comparison against `deadline_time_epoch`.** We never parse `deadline_time` (the
ISO string) for maths. Doing so would drag in timezone handling, which would drag
in DST, and the UK changes clocks in late October and late March - both squarely
inside the season. Comparing Unix integers sidesteps the entire category.

The other thing this module exists to get right is the set of states FPL's own
data model does not handle gracefully:

* **Off-season.** `is_current` is False for every event and `is_next` may be
  absent entirely. The naive `[e for e in events if e["is_next"]][0]` raises
  IndexError, in July, in production, on the first deploy.
* **The post-deadline window.** There is a real interval after a deadline passes
  during which FPL has not yet advanced `is_next`. Trusting the flag alone gives
  you a deadline in the past and a negative time-remaining, which then fails
  every tier test silently.
* **Season end.** No future events at all.

Hence the fallback in `next_deadline` is mandatory, not defensive padding.
"""

from __future__ import annotations

from dataclasses import dataclass

from fplbot.config import CONFIRMED_TIER_SECONDS, NOTIFY_TIERS_SECONDS
from fplbot.models.fpl import Bootstrap, Event
from fplbot.observability import logger

# The outermost tier. Beyond this we snapshot but do not notify. Derived rather
# than written out, because a hardcoded window that disagrees with the tier list
# fails silently in the worse direction: too small and the loosest tier can never
# fire at all.
WINDOW_SECONDS = max(NOTIFY_TIERS_SECONDS)


@dataclass(frozen=True)
class DeadlineInfo:
    """The answer to "what are we working towards, and how long have we got?"."""

    event: Event
    seconds_remaining: int

    @property
    def gameweek(self) -> int:
        return self.event.id

    @property
    def deadline_epoch(self) -> int:
        return self.event.deadline_time_epoch

    @property
    def hours_remaining(self) -> float:
        return self.seconds_remaining / 3600.0

    @property
    def within_window(self) -> bool:
        return 0 < self.seconds_remaining <= WINDOW_SECONDS


def next_deadline(bootstrap: Bootstrap, now_epoch: int) -> DeadlineInfo | None:
    """Find the next deadline and the seconds remaining, or None.

    Returns None in exactly two situations, and the caller must treat both as
    "snapshot and exit cleanly", not as errors:
      * the off-season, before FPL has published the new season's events;
      * after the final deadline of a season.

    Args:
        bootstrap: parsed bootstrap-static payload.
        now_epoch: current Unix time. Passed in rather than read, so tests can
            place themselves anywhere in the season without patching the clock.
    """
    events = bootstrap.events
    if not events:
        logger.warning("Bootstrap contained no events at all - treating as off-season")
        return None

    candidate = next((e for e in events if e.is_next), None)

    # The fallback is mandatory. `is_next` is None in the off-season, AND there is
    # a real window after a deadline passes where FPL has not yet advanced it.
    # Recomputing from epochs is authoritative in both cases.
    if candidate is None or candidate.deadline_time_epoch <= now_epoch:
        future = sorted(
            (e for e in events if e.deadline_time_epoch > now_epoch),
            key=lambda e: e.deadline_time_epoch,
        )
        if candidate is not None and candidate.deadline_time_epoch <= now_epoch:
            logger.info(
                "is_next points at a deadline that has already passed; recomputing",
                extra={"stale_gameweek": candidate.id, "now_epoch": now_epoch},
            )
        candidate = future[0] if future else None

    if candidate is None:
        logger.info("No future deadline found - off-season or season complete")
        return None

    return DeadlineInfo(
        event=candidate,
        seconds_remaining=candidate.deadline_time_epoch - now_epoch,
    )


def due_tier(seconds_remaining: int, already_sent: set[str] | None = None) -> str | None:
    """Which notification tier, if any, this run should fire.

    There is one tier, 24h. A run "crosses" it when the remaining time has dropped
    at or below the threshold, so with hourly polling the first run inside T-24h
    fires and every later one finds the lock already taken.

    We return the **tightest** tier crossed and not yet sent, so a run that fires
    late (a missed schedule, a manual invoke at T-4h) sends the most current
    advice rather than replaying a stale 48h view.

    Args:
        seconds_remaining: from `next_deadline`.
        already_sent: tier labels already delivered for this gameweek. In practice
            the DynamoDB conditional write is the real guard; this is the cheap
            check that avoids doing the work at all.

    Returns:
        "48h" | "24h" | "3h", or None when no tier is due.
    """
    if seconds_remaining <= 0:
        # Deadline has passed. Nothing to advise on.
        return None
    already_sent = already_sent or set()

    # Ascending threshold order gives us the tightest tier first.
    for threshold in sorted(NOTIFY_TIERS_SECONDS):
        label = tier_label(threshold)
        if seconds_remaining <= threshold and label not in already_sent:
            return label
    return None


def tier_label(threshold_seconds: int) -> str:
    """Canonical tier label. Used as part of the idempotency key, so it must be
    stable forever - changing the format would orphan every existing lock and
    allow a duplicate send."""
    return f"{threshold_seconds // 3600}h"


def is_confirmed_phase(tier: str) -> bool:
    """Whether this tier is the one the user should act on.

    With a single T-24h notification the answer is yes for the scheduled tier and
    no for anything else, which in practice means a `force_tier` invocation from
    outside the window. "Wait for the next report" is no longer advice we can
    give, so the scheduled run is by definition the actionable one.

    This does mean acting on less team news than the old T-3h tier had. For a
    Saturday 11:00 deadline T-24h is Friday 11:00: some managers' press
    conferences have happened by then and some have not, and the ones that have
    not leave players sitting at "Currently Being Assessed" with nothing later to
    resolve them. That uncertainty is surfaced in the caveats rather than being
    hidden behind a phase label. SPEC section 3.
    """
    return tier == tier_label(CONFIRMED_TIER_SECONDS)


def season_has_started(bootstrap: Bootstrap, now_epoch: int) -> bool:
    """Whether any gameweek deadline has passed yet this season.

    This gate exists because of the pre-season data trap in SPEC section 0, and
    it is not a nicety. In pre-season `bootstrap-static` is a *mixture*: `minutes`,
    `total_points`, `bps` and `starts` still hold **last season's** values, while
    `form`, `event_points` and all four `transfers_*` fields have been reset to
    zero. Code written and tested in July against that data will silently mix two
    seasons in August.

    Every aggregate feature must be gated on this. Where it returns False we fall
    back to positional priors and the vaastav archive rather than trusting
    carried-over totals.
    """
    return any(e.deadline_time_epoch <= now_epoch for e in bootstrap.events)


def assert_timezone_is_utc(bootstrap: Bootstrap) -> None:
    """Fail loudly if FPL ever stops serving UTC.

    Every epoch assumption in this codebase rests on this. If it changes we want
    a screaming failure at startup, not subtly wrong deadlines discovered by a
    user who transferred a player in an hour late.
    """
    timezone = bootstrap.game_config.settings.timezone
    if timezone != "UTC":
        raise RuntimeError(
            f"game_config.settings.timezone is {timezone!r}, expected 'UTC'. "
            "Every deadline calculation in this codebase assumes UTC epochs. "
            "Do not proceed until this has been re-examined."
        )
