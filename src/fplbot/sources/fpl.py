"""The FPL official API.

Base: https://fantasy.premierleague.com/api/

**Trailing slashes are required.** Without one you get a 301 to the slashed form,
and we run with redirects disabled, so the request fails. This is not pedantry -
it is the single most common way to break a working FPL integration.

No authentication for anything we use, and none is in scope. `users.premierleague.com`
no longer resolves at all (NXDOMAIN); auth moved to PingOne OIDC. The widely-copied
"POST to /accounts/login/ with a pl_profile cookie" recipe is dead, and v1
deliberately touches only public endpoints. SPEC section 0 and section 1.

Endpoints used:

    bootstrap-static/       everything: elements, events, teams, game_config
    fixtures/               all fixtures; ?event={gw} for one gameweek
    element-summary/{id}/   per-gameweek history, retrievable retroactively
    event-status/           bonus and data finalisation state
"""

from __future__ import annotations

from typing import Any

import orjson

from fplbot.config import Endpoints
from fplbot.models.fpl import Bootstrap, ElementSummary, Fixture
from fplbot.observability import logger, tracer
from fplbot.sources.base import SourceContext, SourceResult, with_fallback

SOURCE = "fpl"


@tracer.capture_method
def fetch_bootstrap(context: SourceContext) -> SourceResult[Bootstrap]:
    """Fetch and parse bootstrap-static.

    This is the only *required* source. Everything else degrades a feature; if
    this fails with no cached copy there is no gameweek, no deadline and no
    player list, so there is nothing to say.
    """

    def _fetch() -> Bootstrap:
        result = context.fetch_and_archive(SOURCE, f"{Endpoints.FPL}bootstrap-static/")
        payload = result.json()
        bootstrap = Bootstrap.model_validate(payload)
        logger.info(
            "Parsed bootstrap",
            extra={
                "elements": len(bootstrap.elements),
                "events": len(bootstrap.events),
                "teams": len(bootstrap.teams),
                "bytes": len(result.raw),
            },
        )
        return bootstrap

    return with_fallback(
        context,
        SOURCE,
        _fetch,
        serialise=lambda b: {"payload": b.model_dump(mode="json")},
        deserialise=lambda d: Bootstrap.model_validate(d["payload"]),
        required=True,
    )


@tracer.capture_method
def fetch_fixtures(
    context: SourceContext, gameweek: int | None = None
) -> SourceResult[list[Fixture]]:
    """Fetch fixtures, optionally for a single gameweek.

    We fetch the *whole* list rather than just the target gameweek. It is one
    request either way, and having every fixture lets us spot postponements
    (`event is null`) and look ahead at the fixture run, which feeds the "why"
    lines in the report.
    """
    url = f"{Endpoints.FPL}fixtures/"
    params = {"event": gameweek} if gameweek is not None else None

    def _fetch() -> list[Fixture]:
        result = context.fetch_and_archive(SOURCE, url, params=params)
        payload = result.json()
        fixtures = [Fixture.model_validate(row) for row in payload]
        logger.info("Parsed fixtures", extra={"count": len(fixtures), "gameweek": gameweek})
        return fixtures

    return with_fallback(
        context,
        f"{SOURCE}_fixtures",
        _fetch,
        serialise=lambda fs: {"payload": [f.model_dump(mode="json") for f in fs]},
        deserialise=lambda d: [Fixture.model_validate(r) for r in d["payload"]],
    )


@tracer.capture_method
def fetch_element_summary(context: SourceContext, element_id: int) -> ElementSummary | None:
    """Per-gameweek history for one player.

    `history[]` carries `transfers_in`, `transfers_out`, `transfers_balance`,
    `selected` and `value` per gameweek, retrievable retroactively. That
    substantially reduces the cold-start problem described in SPEC section 5.3:
    gameweek-granular transfer history does not require us to have been running.
    Only *intra-gameweek* velocity needs our own hourly snapshots.

    COST WARNING: this is one request per player. An unbounded backfill across
    558 players is the one way to turn a 50p/month bill into a real one, and with
    1.5 s host spacing it would also take fourteen minutes. Gate it to once per
    gameweek and to the watchlist only - see `handlers/backfill.py`.
    """
    url = f"{Endpoints.FPL}element-summary/{element_id}/"
    try:
        result = context.fetch_and_archive(SOURCE, url)
        return ElementSummary.model_validate(result.json())
    except Exception as exc:
        logger.warning(
            "element-summary fetch failed",
            extra={"element_id": element_id, "error": str(exc)},
        )
        return None


@tracer.capture_method
def fetch_event_status(context: SourceContext) -> dict[str, Any] | None:
    """Bonus and data-finalisation state.

    Used to know whether the previous gameweek's points are final. If they are
    not, `total_points` and `bps` are still moving and any feature derived from
    them is provisional.
    """
    try:
        result = context.fetch_and_archive(SOURCE, f"{Endpoints.FPL}event-status/")
        return result.json()
    except Exception as exc:
        logger.warning("event-status fetch failed", extra={"error": str(exc)})
        return None


# ---------------------------------------------------------------------------
# Snapshot extraction
# ---------------------------------------------------------------------------
def build_snapshot(bootstrap: Bootstrap) -> dict[str, Any]:
    """The slim per-player slice we store hourly.

    Not the whole 3 MB payload - that goes to S3 verbatim. This is the handful of
    fields whose *change over time* is the signal:

    * `transfers_in_event` / `transfers_out_event` for intra-gameweek velocity,
    * `selected` for the ownership denominator that normalises it,
    * `now_cost` and `cost_change_event` for the price model,
    * `price_change_percent`, which is the highest-value unknown in the spec.
      It is currently '0' for everyone. If it turns out to encode progress
      towards the next price change it replaces most price modelling, and the
      only way to find out is to log it hourly from GW1 and correlate against
      `cost_change_event` transitions. That is a day-one instrumentation task
      and this is where it happens. SPEC sections 4.0 and 8 item 1.

    Gzipped this comes to roughly 60 KB, comfortably inside DynamoDB's 400 KB
    item limit.
    """
    return {
        "total_players": bootstrap.total_players,
        "players": [
            {
                "id": e.id,
                "cost": e.now_cost,
                "cost_change_event": e.cost_change_event,
                "price_change_percent": e.price_change_percent,
                "selected_by_percent": e.selected_by_percent,
                "transfers_in_event": e.transfers_in_event,
                "transfers_out_event": e.transfers_out_event,
                "transfers_in": e.transfers_in,
                "transfers_out": e.transfers_out,
                "status": e.status,
                "chance": e.availability_pct,
                "news_added": e.news_added,
                "form": e.form,
                "ep_next": e.ep_next,
            }
            for e in bootstrap.elements
        ],
    }


def snapshot_bytes(bootstrap: Bootstrap) -> bytes:
    return orjson.dumps(build_snapshot(bootstrap))
