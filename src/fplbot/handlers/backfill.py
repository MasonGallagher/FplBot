"""Per-gameweek backfill: `element-summary` history for the watchlist.

A SEPARATE FUNCTION, AND THAT MATTERS
-------------------------------------
`element-summary/{id}/` is one HTTP request per player. With 1.5-second host
spacing, 558 players would take fourteen minutes - well past the poll function's
120-second timeout - and would be the one way to turn a 50p/month bill into a
real one. SPEC section 2 is explicit: *the one way to blow the cost up is an
unbounded element-summary backfill; gate it to once per gameweek.*

So this runs as its own function with a 600-second timeout, on its own schedule,
against a bounded watchlist rather than the whole player list.

WHAT IT BUYS US
---------------
`history[]` carries `transfers_in`, `transfers_out`, `transfers_balance`,
`selected` and `value` **per gameweek, retrievable retroactively**. That
substantially reduces the cold-start problem: gameweek-granular transfer history
does not require the bot to have been running. Only *intra-gameweek* velocity
needs our own hourly snapshots.

It also settles SPEC section 8 item 3 - whether `history[].transfers_in` is
per-gameweek or cumulative-to-that-gameweek - by diffing two consecutive rows and
logging the answer. That is the treatment the spec asks for: surface the open
question, do not guess at it.
"""

from __future__ import annotations

import itertools
import time
from typing import Any

from aws_lambda_powertools.utilities.typing import LambdaContext

from fplbot.config import get_settings
from fplbot.http import HttpClient
from fplbot.models.domain import DataQuality
from fplbot.observability import logger, metrics, tracer
from fplbot.sources import fpl
from fplbot.sources.base import SourceContext
from fplbot.storage import DynamoStore, RawArchive

# How many players to pull per run. Chosen so the whole batch fits inside the
# 600-second timeout with 1.5-second spacing and a safety margin:
# 120 x 1.5s = 180s of spacing, plus request time.
DEFAULT_WATCHLIST_SIZE = 120


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics
@tracer.capture_lambda_handler
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Backfill per-gameweek history for the most relevant players.

    Event overrides:
        {"limit": 40}            fetch fewer players
        {"element_ids": [1, 2]}  fetch specific players
    """
    settings = get_settings()
    limit = (
        int(event.get("limit", DEFAULT_WATCHLIST_SIZE))
        if isinstance(event, dict)
        else DEFAULT_WATCHLIST_SIZE
    )
    explicit_ids = event.get("element_ids") if isinstance(event, dict) else None

    quality = DataQuality()
    started = time.time()

    with HttpClient() as http:
        source_context = SourceContext(
            http=http, archive=RawArchive(), store=DynamoStore(), quality=quality
        )

        bootstrap_result = fpl.fetch_bootstrap(source_context)
        bootstrap = bootstrap_result.data
        if bootstrap is None:
            return {"statusCode": 500, "body": {"error": "bootstrap unavailable"}}

        element_ids = explicit_ids or _select_watchlist(bootstrap, limit)
        logger.info(
            "Starting backfill",
            extra={"players": len(element_ids), "season": settings.season},
        )

        fetched = 0
        semantics_logged = False

        for element_id in element_ids:
            # Leave headroom so a slow run finishes cleanly rather than being
            # killed mid-write. A partial backfill is fine; a timeout in the
            # middle of a DynamoDB batch is messier than it needs to be.
            if context.get_remaining_time_in_millis() < 30_000:
                logger.warning(
                    "Stopping backfill early to stay inside the timeout",
                    extra={"fetched": fetched, "remaining": len(element_ids) - fetched},
                )
                break

            summary = fpl.fetch_element_summary(source_context, element_id)
            if summary is None or not summary.history:
                continue

            for row in summary.history:
                source_context.store.put_player_series(
                    element_id,
                    {
                        "round": row.round,
                        "minutes": row.minutes,
                        "total_points": row.total_points,
                        "value": row.value,
                        "selected": row.selected,
                        "transfers_in": row.transfers_in,
                        "transfers_out": row.transfers_out,
                        "transfers_balance": row.transfers_balance,
                        "defensive_contribution": row.defensive_contribution,
                        "source": "element_summary",
                    },
                )

            if not semantics_logged and len(summary.history) >= 2:
                _log_transfer_semantics(element_id, summary)
                semantics_logged = True

            fetched += 1

    elapsed = time.time() - started
    logger.info(
        "Backfill complete",
        extra={"fetched": fetched, "elapsed_s": round(elapsed, 1)},
    )
    return {"statusCode": 200, "body": {"fetched": fetched, "elapsed_s": round(elapsed, 1)}}


def _select_watchlist(bootstrap, limit: int) -> list[int]:
    """Choose which players are worth spending requests on.

    Ownership plus price. High-ownership players matter because a change affects
    most managers; expensive players matter because they are the ones anyone is
    actually deciding about. A 3.9m fourth-choice goalkeeper's transfer history is
    not worth a request, and there are a couple of hundred of him.
    """
    ranked = sorted(
        bootstrap.elements,
        key=lambda e: (e.ownership, e.now_cost),
        reverse=True,
    )
    return [element.id for element in ranked[:limit]]


def _log_transfer_semantics(element_id: int, summary) -> None:
    """Resolve SPEC section 8 item 3 from live data.

    The question: is `history[].transfers_in` a per-gameweek count, or is it
    cumulative to that gameweek?

    The test is a diff. If the series is monotonically non-decreasing across every
    consecutive pair, it is cumulative. If it moves in both directions, it is
    per-gameweek. We log the verdict rather than assuming one, because getting it
    wrong would put a season-cumulative number where a weekly one belongs and
    make every transfer-flow z-score meaningless.
    """
    rows = sorted(summary.history, key=lambda r: r.round)
    values = [row.transfers_in for row in rows]
    diffs = [b - a for a, b in itertools.pairwise(values)]

    monotonic = all(d >= 0 for d in diffs)
    verdict = (
        "looks CUMULATIVE (monotonically non-decreasing)"
        if monotonic and any(d > 0 for d in diffs)
        else "looks PER-GAMEWEEK (moves in both directions)"
    )

    logger.info(
        "SPEC open question 3: element-summary history[].transfers_in semantics",
        extra={
            "element_id": element_id,
            "verdict": verdict,
            "series": values[:10],
            "diffs": diffs[:10],
            "action": (
                "If cumulative, availability.py must diff consecutive rows before "
                "computing per-gameweek flow. If per-gameweek, use the values directly."
            ),
        },
    )
