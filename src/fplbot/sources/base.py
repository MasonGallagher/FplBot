"""Shared plumbing for source adapters: fetch, archive, parse, degrade.

Every source follows the same lifecycle, and writing it once here means a new
source cannot accidentally skip the archive step or forget to fall back:

    1. Fetch through the hardened client (retry, spacing, breaker, redirect ban).
    2. Archive the **verbatim bytes** to S3 before parsing anything.
    3. Parse.
    4. On success, store the parsed result as last-known-good.
    5. On failure, load last-known-good and mark the source degraded.

Step 5 is the interesting one and it embodies SPEC section 6.2: *always produce
output*. A recommendation built partly on yesterday's xG, clearly labelled, is
worth far more to someone three hours from a deadline than an apologetic silence.
The only case where we refuse is when the data is older than the hard staleness
ceiling, and then we email about the failure rather than about transfers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from fplbot.http import BreakerOpen, FetchResult, HttpClient, HttpFetchError
from fplbot.models.domain import DataQuality, SourceState
from fplbot.observability import Metric, count, logger
from fplbot.storage import DynamoStore, RawArchive

T = TypeVar("T")


@dataclass
class SourceContext:
    """The collaborators every source needs. Built once per invocation."""

    http: HttpClient
    archive: RawArchive
    store: DynamoStore
    quality: DataQuality

    def fetch_and_archive(
        self,
        source: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        expect_content_type: str | tuple[str, ...] = "application/json",
        ext: str = "json",
    ) -> FetchResult:
        """Fetch, then archive the raw bytes before anyone parses them.

        Archiving before parsing is deliberate. If the parse throws - which is
        exactly what happens when a provider redesigns - we still have the bytes
        that caused it, which turns a mystery into a five-minute diagnosis.
        """
        result = self.http.fetch(
            source,
            url,
            headers=headers,
            params=params,
            expect_content_type=expect_content_type,
        )
        self.archive.put(
            source,
            url,
            result.raw,
            content_type=result.headers.get("content-type", "application/octet-stream"),
            ext=ext,
        )
        return result


class SourceResult(Generic[T]):
    """A source's output plus an honest account of where it came from."""

    def __init__(
        self,
        name: str,
        data: T | None,
        state: SourceState,
        detail: str = "",
        age_seconds: int | None = None,
    ) -> None:
        self.name = name
        self.data = data
        self.state = state
        self.detail = detail
        self.age_seconds = age_seconds

    @property
    def ok(self) -> bool:
        return self.state is SourceState.OK

    @property
    def usable(self) -> bool:
        return self.data is not None and self.state in {SourceState.OK, SourceState.DEGRADED}

    def __repr__(self) -> str:
        return f"<SourceResult {self.name} state={self.state} detail={self.detail!r}>"


def with_fallback(
    context: SourceContext,
    name: str,
    fetch: Callable[[], T],
    *,
    serialise: Callable[[T], dict[str, Any]] | None = None,
    deserialise: Callable[[dict[str, Any]], T] | None = None,
    required: bool = False,
) -> SourceResult[T]:
    """Run a source's fetch, degrading to last-known-good on failure.

    Args:
        fetch: does the work and returns parsed data. Raises on failure.
        serialise / deserialise: how to round-trip the parsed data through
            DynamoDB. Omit both to skip last-known-good entirely - appropriate
            for sources where stale data is worse than none (predicted line-ups
            from a previous gameweek, for instance, are actively misleading).
        required: when True, a failure with no fallback aborts the run. Only the
            FPL API is required; everything else degrades a feature rather than
            the whole report.

    Returns:
        A SourceResult that always tells you which of the three worlds you are in.
    """
    try:
        data = fetch()
    except BreakerOpen as exc:
        logger.warning("Source skipped - breaker open", extra={"source": name})
        return _degrade(context, name, str(exc), deserialise, required)
    except HttpFetchError as exc:
        logger.warning(
            "Source fetch failed",
            extra={"source": name, "reason": exc.reason, "status": exc.status},
        )
        return _degrade(context, name, exc.reason, deserialise, required)
    except Exception as exc:
        logger.exception("Source parse failed", extra={"source": name})
        return _degrade(context, name, f"parse error: {exc}", deserialise, required)

    if serialise is not None:
        try:
            context.store.put_last_known_good(name, serialise(data))
        except Exception as exc:
            # Failing to *save* LKG must never fail a run that already succeeded.
            logger.warning(
                "Could not store last-known-good", extra={"source": name, "error": str(exc)}
            )

    context.quality.record(name, SourceState.OK)
    return SourceResult(name, data, SourceState.OK)


def _degrade(
    context: SourceContext,
    name: str,
    reason: str,
    deserialise: Callable[[dict[str, Any]], T] | None,
    required: bool,
) -> SourceResult[T]:
    """Try last-known-good; otherwise mark the source failed."""
    if deserialise is not None:
        payload, age = context.store.get_last_known_good(name)
        if payload is not None and age is not None:
            hours = age / 3600
            logger.info(
                "Serving source from last-known-good",
                extra={"source": name, "age_hours": round(hours, 1)},
            )
            count(Metric.SOURCE_DEGRADED, source=name)
            detail = f"{reason}; using data {hours:.1f}h old"
            context.quality.record(name, SourceState.DEGRADED, detail, age)
            context.quality.add_caveat(
                f"{name}: unavailable, using cached data from {hours:.1f} hours ago."
            )
            return SourceResult(name, deserialise(payload), SourceState.DEGRADED, detail, age)

    context.quality.record(name, SourceState.FAILED, reason)
    context.quality.add_caveat(f"{name}: unavailable and no cached data. Feature disabled.")

    if required:
        raise RuntimeError(f"Required source {name} failed with no fallback: {reason}")

    return SourceResult(name, None, SourceState.FAILED, reason)
