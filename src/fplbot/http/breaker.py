"""A small per-source circuit breaker.

Why bother, given a scheduled singleton with reserved concurrency 1?

Because the failure we care about is not "this one request failed" but "this
source is down and every further attempt costs us 3 retries times 20 seconds of
a 120-second budget". Understat being unreachable must not stop us emailing a
report built from FPL, ClubElo and PremierInjuries. The breaker converts a slow,
repeated failure into a fast, single failure, and the caller degrades to
last-known-good data.

State is per-process. On a warm container it persists between invocations, which
is exactly what we want: if Understat blocked our IP an hour ago it is probably
still blocking it now. On a cold start we get a clean slate and one honest
attempt, which is also what we want. Nothing here is persisted to DynamoDB - the
last-known-good pointer already covers the durable half of the problem, and a
distributed breaker for a singleton would be ceremony without benefit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from fplbot.config import HTTP_POLICY
from fplbot.observability import Metric, count, logger


class BreakerOpen(RuntimeError):
    """Raised instead of attempting a request the breaker has cut off."""

    def __init__(self, source: str, retry_after_seconds: float) -> None:
        super().__init__(
            f"Circuit breaker open for {source}; "
            f"retrying in {retry_after_seconds:.0f}s at the earliest"
        )
        self.source = source
        self.retry_after_seconds = retry_after_seconds


@dataclass
class _SourceState:
    consecutive_failures: int = 0
    opened_at: float | None = None


@dataclass
class CircuitBreaker:
    """Tracks consecutive failures per named source.

    Deliberately a plain two-state breaker (closed / open) with a timed reset
    rather than the textbook three-state closed/open/half-open. With at most a
    handful of requests per source per invocation, the half-open probing state
    would never be exercised meaningfully - it would be untested code guarding
    a case that does not arise.
    """

    failure_threshold: int = HTTP_POLICY.breaker_failure_threshold
    reset_seconds: float = HTTP_POLICY.breaker_reset_seconds
    _state: dict[str, _SourceState] = field(default_factory=dict)

    def _state_for(self, source: str) -> _SourceState:
        return self._state.setdefault(source, _SourceState())

    def check(self, source: str) -> None:
        """Raise BreakerOpen if this source is currently cut off."""
        state = self._state_for(source)
        if state.opened_at is None:
            return

        elapsed = time.monotonic() - state.opened_at
        if elapsed >= self.reset_seconds:
            # Cool-down elapsed. Reset fully and let the next request through.
            logger.info("Circuit breaker reset", extra={"source": source})
            state.opened_at = None
            state.consecutive_failures = 0
            return

        count(Metric.CIRCUIT_BREAKER_OPEN, source=source)
        raise BreakerOpen(source, self.reset_seconds - elapsed)

    def record_success(self, source: str) -> None:
        """A success closes the breaker and clears the failure count."""
        state = self._state_for(source)
        if state.consecutive_failures or state.opened_at is not None:
            logger.info("Circuit breaker cleared by success", extra={"source": source})
        state.consecutive_failures = 0
        state.opened_at = None

    def record_failure(self, source: str) -> None:
        """A failure increments the count, and may trip the breaker."""
        state = self._state_for(source)
        state.consecutive_failures += 1
        if state.consecutive_failures >= self.failure_threshold and state.opened_at is None:
            state.opened_at = time.monotonic()
            logger.warning(
                "Circuit breaker tripped",
                extra={
                    "source": source,
                    "consecutive_failures": state.consecutive_failures,
                    "reset_seconds": self.reset_seconds,
                },
            )

    def is_open(self, source: str) -> bool:
        """Non-raising variant, for reporting data quality in the email."""
        try:
            self.check(source)
        except BreakerOpen:
            return True
        return False

    def open_sources(self) -> list[str]:
        """Which sources are currently cut off - feeds the data-quality line."""
        return sorted(name for name in self._state if self.is_open(name))
