"""DynamoDB adapter.

Uses the boto3 *resource* interface rather than the client, because the resource
layer marshals Python types to and from DynamoDB's typed attribute format for us.
The one thing it will not accept is `float` - DynamoDB has no floating-point type,
only `N` (an arbitrary-precision decimal). We convert on the way in and back on
the way out; `_to_dynamo` / `_from_dynamo` below. Getting this wrong is the single
most common DynamoDB papercut in Python.
"""

from __future__ import annotations

import gzip
import time
from decimal import Decimal
from typing import Any

import boto3
import orjson
from botocore.exceptions import ClientError

from fplbot.config import ALIAS_TTL_DAYS, SNAPSHOT_TTL_DAYS, get_settings
from fplbot.observability import logger
from fplbot.storage.keys import Keys, iso_now

DAY_SECONDS = 86_400


class LockAlreadyHeld(RuntimeError):
    """The idempotency lock for this (gameweek, tier) already exists."""


def _to_dynamo(value: Any) -> Any:
    """Recursively convert Python values into something DynamoDB accepts.

    floats -> Decimal, and empty strings are preserved (DynamoDB has allowed
    empty string attribute values since 2020; the old "must be null" advice is
    out of date and would lose information here - FPL uses '' meaningfully,
    e.g. `chance_of_playing_next_round` is '' for a fit player).
    """
    if isinstance(value, float):
        # str() first: Decimal(0.1) is 0.1000000000000000055511151231257827,
        # Decimal("0.1") is 0.1. Always go via the string form.
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
    return value


def _from_dynamo(value: Any) -> Any:
    """Inverse of `_to_dynamo`: Decimal back to int or float."""
    if isinstance(value, Decimal):
        # Preserve integer-ness. `selected` counts are integers and turning them
        # into floats makes every downstream log line uglier and every equality
        # comparison riskier.
        as_int = int(value)
        return as_int if value == as_int else float(value)
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    return value


class DynamoStore:
    """All DynamoDB access. One instance per invocation."""

    def __init__(self, table_name: str | None = None, *, resource: Any = None) -> None:
        settings = get_settings()
        self._table_name = table_name or settings.table_name
        self._resource = resource or boto3.resource("dynamodb", region_name=settings.aws_region)
        self._table = self._resource.Table(self._table_name)
        self._season = settings.season

    # -- idempotency -------------------------------------------------------

    def acquire_notification_lock(self, gameweek: int, tier: str, *, ttl_days: int = 30) -> None:
        """Claim the right to send the email for this (gameweek, tier).

        The whole mechanism is one conditional write. `attribute_not_exists(pk)`
        is evaluated by DynamoDB atomically as part of the write, so two
        concurrent invocations cannot both succeed - one gets the item, the other
        gets ConditionalCheckFailedException. That is a distributed lock in a
        single API call, with no lease to renew and nothing to clean up.

        Note that we take the lock *before* sending, not after. If we crash
        between locking and sending, no email goes out for that tier - we have
        chosen at-most-once for a given tier. That is the right trade here: a
        missing 48h email is recoverable (the 24h and 3h tiers still fire), while
        a duplicate email erodes trust in every future one.

        Raises:
            LockAlreadyHeld: someone already sent this notification.
        """
        now = int(time.time())
        try:
            self._table.put_item(
                Item={
                    "pk": Keys.notify_pk(self._season, gameweek, tier),
                    "sk": Keys.NOTIFY_SK,
                    "acquired_at": iso_now(),
                    "gameweek": gameweek,
                    "tier": tier,
                    # Locks are transient state; a month is far longer than they
                    # can matter and keeps the table tidy.
                    "ttl": now + ttl_days * DAY_SECONDS,
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
            logger.info("Acquired notification lock", extra={"gameweek": gameweek, "tier": tier})
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise LockAlreadyHeld(
                    f"Notification for GW{gameweek} tier {tier} has already been sent"
                ) from exc
            raise

    def release_notification_lock(self, gameweek: int, tier: str) -> None:
        """Give the lock back after a send failure, so a retry can try again.

        Only called when sending itself failed. If we took the lock and then SES
        rejected the message, holding the lock would suppress every retry for a
        tier that never actually delivered.
        """
        self._table.delete_item(
            Key={"pk": Keys.notify_pk(self._season, gameweek, tier), "sk": Keys.NOTIFY_SK}
        )
        logger.warning(
            "Released notification lock after send failure",
            extra={"gameweek": gameweek, "tier": tier},
        )

    # -- bootstrap snapshots ----------------------------------------------

    def put_snapshot(self, payload: dict[str, Any], *, timestamp: str | None = None) -> str:
        """Store a gzipped summary snapshot.

        We do NOT store the whole 3 MB bootstrap here - that is S3's job. What
        goes in DynamoDB is the slim per-player slice we need for time-series
        work (ownership, price, transfer counts), gzipped, which lands at roughly
        60 KB and sits comfortably under the 400 KB item limit.

        TTL is 400 days, not 30. This series is the only training set for the
        price model and the only source of intra-gameweek transfer velocity.
        Losing it to a short TTL would be silent and irreversible, and it costs
        pennies to keep. SPEC section 2.
        """
        sk = timestamp or iso_now()
        blob = gzip.compress(orjson.dumps(payload), compresslevel=6)
        self._table.put_item(
            Item={
                "pk": Keys.snapshot_pk(self._season),
                "sk": sk,
                "gz": blob,
                "bytes": len(blob),
                "ttl": int(time.time()) + SNAPSHOT_TTL_DAYS * DAY_SECONDS,
            }
        )
        logger.info("Stored snapshot", extra={"sk": sk, "gz_bytes": len(blob)})
        return sk

    def recent_snapshots(self, limit: int = 48) -> list[dict[str, Any]]:
        """Most recent snapshots, newest first.

        `ScanIndexForward=False` walks the sort key descending. Because the sort
        key is ISO-8601, descending lexicographic order is descending
        chronological order - no filtering, no sorting in Python.
        """
        from boto3.dynamodb.conditions import Key as DdbKey

        response = self._table.query(
            KeyConditionExpression=DdbKey("pk").eq(Keys.snapshot_pk(self._season)),
            ScanIndexForward=False,
            Limit=limit,
        )
        out: list[dict[str, Any]] = []
        for item in response.get("Items", []):
            blob = item.get("gz")
            if blob is None:
                continue
            raw = blob.value if hasattr(blob, "value") else bytes(blob)
            payload = orjson.loads(gzip.decompress(raw))
            payload["_sk"] = item["sk"]
            out.append(payload)
        return out

    # -- last known good ---------------------------------------------------

    def put_last_known_good(self, source: str, payload: dict[str, Any]) -> None:
        """Remember the most recent successful parse from a source.

        This is the durable half of graceful degradation. When Understat is
        unreachable, we do not simply drop xG from the model - we use yesterday's
        xG and mark the source as degraded in the email. Stale data, clearly
        labelled, beats a silently different model. SPEC section 6.2.
        """
        self._table.put_item(
            Item={
                "pk": Keys.lkg_pk(self._season),
                "sk": Keys.lkg_sk(source),
                "stored_at": iso_now(),
                "stored_at_epoch": int(time.time()),
                "gz": gzip.compress(orjson.dumps(payload), compresslevel=6),
                "ttl": int(time.time()) + SNAPSHOT_TTL_DAYS * DAY_SECONDS,
            }
        )

    def get_last_known_good(self, source: str) -> tuple[dict[str, Any] | None, int | None]:
        """Return (payload, age_seconds) or (None, None) if we have never stored one."""
        response = self._table.get_item(
            Key={"pk": Keys.lkg_pk(self._season), "sk": Keys.lkg_sk(source)}
        )
        item = response.get("Item")
        if not item:
            return None, None
        blob = item["gz"]
        raw = blob.value if hasattr(blob, "value") else bytes(blob)
        payload = orjson.loads(gzip.decompress(raw))
        age = int(time.time()) - int(item.get("stored_at_epoch", 0))
        return payload, age

    # -- alias cache -------------------------------------------------------

    def get_alias(self, source: str, source_id: str) -> int | None:
        """Look up a previously resolved FPL element id for a source's player id."""
        response = self._table.get_item(
            Key={"pk": Keys.alias_pk(source), "sk": Keys.alias_sk(source_id)}
        )
        item = response.get("Item")
        if not item:
            return None
        return int(item["element_id"])

    def get_aliases(self, source: str) -> dict[str, int]:
        """Fetch the whole alias map for a source in one query.

        Far cheaper than N `get_item` calls: one query returns every alias for
        the source because they share a partition key. This is the payoff of the
        single-table design.
        """
        from boto3.dynamodb.conditions import Key as DdbKey

        aliases: dict[str, int] = {}
        kwargs: dict[str, Any] = {"KeyConditionExpression": DdbKey("pk").eq(Keys.alias_pk(source))}
        while True:
            response = self._table.query(**kwargs)
            for item in response.get("Items", []):
                aliases[str(item["sk"])] = int(item["element_id"])
            token = response.get("LastEvaluatedKey")
            if not token:
                break
            kwargs["ExclusiveStartKey"] = token
        return aliases

    def put_alias(
        self, source: str, source_id: str, element_id: int, *, method: str, score: float
    ) -> None:
        """Cache a resolution, recording *how* it was resolved.

        Storing the method and score is not bookkeeping for its own sake: when a
        recommendation turns out to be about the wrong player, the first question
        is "was this an exact join or an 89-point fuzzy match?", and you want to
        be able to answer it without re-running anything.
        """
        self._table.put_item(
            Item={
                "pk": Keys.alias_pk(source),
                "sk": Keys.alias_sk(source_id),
                "element_id": element_id,
                "method": method,
                "score": _to_dynamo(score),
                "resolved_at": iso_now(),
                "ttl": int(time.time()) + ALIAS_TTL_DAYS * DAY_SECONDS,
            }
        )

    # -- per-player watchlist series --------------------------------------

    def put_player_series(self, element_id: int, record: dict[str, Any]) -> None:
        """Append a point to a watched player's hourly series."""
        self._table.put_item(
            Item={
                "pk": Keys.player_pk(self._season, element_id),
                "sk": iso_now(),
                **_to_dynamo(record),
                "ttl": int(time.time()) + SNAPSHOT_TTL_DAYS * DAY_SECONDS,
            }
        )

    def get_player_series(self, element_id: int, limit: int = 72) -> list[dict[str, Any]]:
        """Most recent series points for a player, oldest first."""
        from boto3.dynamodb.conditions import Key as DdbKey

        response = self._table.query(
            KeyConditionExpression=DdbKey("pk").eq(Keys.player_pk(self._season, element_id)),
            ScanIndexForward=False,
            Limit=limit,
        )
        items = [_from_dynamo(item) for item in response.get("Items", [])]
        return list(reversed(items))
