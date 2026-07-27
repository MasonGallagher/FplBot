"""Logging, metrics and the alarm surface.

We use AWS Lambda Powertools for three things:

* `Logger`  - JSON structured logs, so CloudWatch Logs Insights can query them.
* `Metrics` - EMF metrics, which are emitted as a specially-shaped log line and
  turned into CloudWatch metrics for free. No PutMetricData API call, no latency,
  no cost per metric.
* `Tracer`  - X-Ray segments around each source fetch, so a slow run is
  diagnosable without adding timing logs everywhere.

The metric names below are not decoration. SPEC section 6.3 makes the point that
silent schema drift is a worse failure than an outright outage, because it yields
confident, wrong recommendations. `SchemaDriftDetected` and `InvariantViolated`
are the early-warning system; the SAM template alarms on both.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, ParamSpec, TypeVar

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit, single_metric

SERVICE_NAME = "fplbot"

logger = Logger(service=SERVICE_NAME)
tracer = Tracer(service=SERVICE_NAME)
metrics = Metrics(namespace="fplBot", service=SERVICE_NAME)


class Metric:
    """Metric names, as constants, so a typo cannot silently create a new metric.

    That failure mode is nastier than it sounds: a mistyped metric name produces
    a metric that exists, is never alarmed on, and looks fine on a dashboard you
    forgot to update.
    """

    SOURCE_FETCH_OK = "SourceFetchOk"
    SOURCE_FETCH_FAILED = "SourceFetchFailed"
    SOURCE_FETCH_DURATION = "SourceFetchDurationMs"
    SOURCE_DEGRADED = "SourceDegraded"  # fell back to last-known-good
    CIRCUIT_BREAKER_OPEN = "CircuitBreakerOpen"

    SCHEMA_DRIFT_DETECTED = "SchemaDriftDetected"  # unexpected field appeared
    INVARIANT_VIOLATED = "InvariantViolated"  # business rule broke

    PLAYERS_RESOLVED_EXACT = "PlayersResolvedExact"
    PLAYERS_RESOLVED_FUZZY = "PlayersResolvedFuzzy"
    PLAYERS_UNRESOLVED = "PlayersUnresolved"

    NOTIFICATION_SENT = "NotificationSent"
    NOTIFICATION_SUPPRESSED = "NotificationSuppressed"  # idempotency lock held
    RUN_ABANDONED_STALE = "RunAbandonedStale"

    ODDS_CREDITS_REMAINING = "OddsCreditsRemaining"


# Fields we have already reported as drifted, so we log once per field per
# container rather than once per player per run. 558 elements times one unknown
# field is 558 identical log lines and a surprising CloudWatch bill.
_reported_drift: set[str] = set()


def report_schema_drift(model: str, field_name: str, sample: Any = None) -> None:
    """Record that an upstream payload contained a field we do not know about.

    This is *informational*, not an error. Pydantic models use extra="allow"
    precisely so a new FPL field cannot hard-fail the run. But we want to know,
    because a new field is often the visible edge of a semantic change to an
    existing one.
    """
    key = f"{model}.{field_name}"
    if key in _reported_drift:
        return
    _reported_drift.add(key)
    logger.info(
        "Unexpected field in upstream payload",
        extra={"model": model, "field": field_name, "sample": repr(sample)[:200]},
    )
    metrics.add_metric(name=Metric.SCHEMA_DRIFT_DETECTED, unit=MetricUnit.Count, value=1)


def report_invariant_violation(name: str, detail: str) -> None:
    """Record a business invariant that types cannot catch failing.

    Example: `sum(selected_by_percent)` should be about 1500 because 15 squad
    slots times 100%. If it is not, the ownership data is incoherent and every
    rank-attacking calculation downstream is built on sand.
    """
    logger.error("Business invariant violated", extra={"invariant": name, "detail": detail})
    metrics.add_metric(name=Metric.INVARIANT_VIOLATED, unit=MetricUnit.Count, value=1)


def emit(name: str, value: float, unit: MetricUnit, **dimensions: str) -> None:
    """Emit one metric, optionally dimensioned.

    Note `single_metric` is a module-level context manager in Powertools, not a
    method on `Metrics`. It exists because a dimensioned metric belongs in its
    own EMF blob: CloudWatch treats each distinct dimension set as a separate
    metric, and mixing dimensioned and undimensioned values into one blob
    produces metrics that silently do not aggregate the way you expect.
    """
    if not dimensions:
        metrics.add_metric(name=name, unit=unit, value=value)
        return

    with single_metric(name=name, unit=unit, value=value, namespace="fplBot") as metric:
        for key, dim_value in dimensions.items():
            metric.add_dimension(name=key, value=dim_value)


def count(name: str, value: float = 1, **dimensions: str) -> None:
    """Emit a count metric, optionally dimensioned (e.g. by source)."""
    emit(name, value, MetricUnit.Count, **dimensions)


@contextmanager
def timed(metric_name: str, **dimensions: str) -> Iterator[None]:
    """Time a block and emit the duration in milliseconds."""
    started = time.perf_counter()
    try:
        yield
    finally:
        emit(
            metric_name,
            (time.perf_counter() - started) * 1000,
            MetricUnit.Milliseconds,
            **dimensions,
        )


P = ParamSpec("P")
R = TypeVar("R")


def log_call(func: Callable[P, R]) -> Callable[P, R]:
    """Log entry and exit of a function at DEBUG, with timing.

    Used sparingly - on the handful of orchestration functions where knowing
    "we got this far" is what you actually want at three in the morning.
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        started = time.perf_counter()
        logger.debug("Entering %s", func.__qualname__)
        try:
            result = func(*args, **kwargs)
        except Exception:
            logger.exception("Failed in %s", func.__qualname__)
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.debug("Left %s", func.__qualname__, extra={"duration_ms": round(elapsed_ms, 1)})
        return result

    return wrapper
