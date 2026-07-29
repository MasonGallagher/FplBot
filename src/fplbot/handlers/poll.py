"""The scheduled poll handler.

Invoked hourly by EventBridge Scheduler at `cron(7 * * * ? *)` in Europe/London.
Every run snapshots; only runs that cross a notification tier send an email.

WHY :07 AND NOT :00
-------------------
Two reasons, and both are real. First, the top of the hour is when every
scheduled job in the world fires, so the whole internet's CDN edge is busiest
then. Second - and more usefully - FPL sits behind Fastly with a 300-second edge
TTL, so a request at exactly :00 has a good chance of returning a cached response
assembled before the minute rolled over. Seven minutes past is late enough that
the cache has certainly turned over and early enough that we are still sampling
the hour cleanly.

Polling faster than five minutes is pure waste: you get byte-identical cached
responses and manufacture a transfer velocity of zero. SPEC section 3.

THE POWERTOOLS DECORATORS
-------------------------
`inject_lambda_context` adds the request id and cold-start flag to every log
line. `log_metrics` flushes buffered EMF metrics at the end of the invocation -
without it, metrics accumulated during the run are silently dropped, which is a
uniquely annoying way to lose observability. `capture_lambda_handler` opens the
X-Ray root segment.
"""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools.utilities.typing import LambdaContext

from fplbot import pipeline
from fplbot.observability import PUBLISH_ALL_METRICS, logger, metrics, tracer


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=PUBLISH_ALL_METRICS)
@tracer.capture_lambda_handler
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Run one poll.

    The event is normally empty - EventBridge Scheduler sends whatever payload we
    configured, and we configure nothing. Two optional overrides are honoured for
    manual invocation:

        {"force_tier": "3h"}      send this tier regardless of time remaining
        {"now_epoch": 1767225600} pretend it is a different moment

    `force_tier` still respects the DynamoDB idempotency lock, so it cannot be
    used to send the same tier twice. That is deliberate: a debug affordance that
    can spam the user is not a debug affordance.
    """
    force_tier = event.get("force_tier") if isinstance(event, dict) else None
    now_epoch = event.get("now_epoch") if isinstance(event, dict) else None

    if force_tier:
        logger.warning(
            "Manual invocation with a forced tier",
            extra={"force_tier": force_tier},
        )

    outcome = pipeline.run(now_epoch=now_epoch, force_tier=force_tier)

    logger.info("Run complete", extra=outcome.as_dict())

    # We return 200 even for `status: failed`. A non-2xx (or a raised exception)
    # would make Scheduler retry, and a retry cannot fix "Understat is blocking
    # our IP" or "the data is 30 hours old" - it would simply burn the invocation
    # and, worse, could send a second email. Genuine infrastructure failures
    # still raise and still get retried; this path is for *handled* outcomes.
    return {"statusCode": 200, "body": outcome.as_dict()}
