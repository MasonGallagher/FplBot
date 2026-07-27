"""Raw response archive.

One rule, and it matters more than it looks: **we store the verbatim bytes we
received, gzipped, and never a re-serialised parse.**

The reason is schema drift. When FPL changes a field's meaning next February,
the question you need to answer is "what exactly did the payload look like
before and after?". If the archive contains `orjson.dumps(parsed_model)`, it
contains today's *interpretation* of the payload - fields our model dropped are
gone, types our model coerced are coerced, and the evidence you need has been
destroyed by the very code you are trying to debug.

Verbatim bytes also make backtests honest. A replay from raw bytes exercises the
parser; a replay from a parsed dump does not.

Layout:

    raw/{source}/{yyyy}/{mm}/{dd}/{iso8601}--{slug}.{ext}.gz

Date-partitioned because that is how you will query it (Athena, or just `aws s3
ls`), and because a lifecycle rule that moves objects older than 90 days to
Glacier Instant Retrieval is trivial to express on a date prefix.
"""

from __future__ import annotations

import gzip
import re
from datetime import UTC, datetime
from typing import Any

import boto3

from fplbot.config import get_settings
from fplbot.observability import logger

_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    """Turn a URL path into something safe and readable as an object key."""
    return _SLUG_UNSAFE.sub("-", value.lower()).strip("-")[:80] or "root"


class RawArchive:
    """Writes raw upstream payloads to S3. Never reads them at runtime."""

    def __init__(self, bucket: str | None = None, *, client: Any = None) -> None:
        settings = get_settings()
        self._bucket = bucket or settings.bucket_name
        self._client = client or boto3.client("s3", region_name=settings.aws_region)
        self._season = settings.season

    def key_for(self, source: str, url: str, *, when: datetime | None = None, ext: str) -> str:
        moment = when or datetime.now(UTC)
        # The path minus the scheme and host, which is the bit that identifies
        # *what* was fetched rather than from whom.
        path = url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url
        return (
            f"raw/{source}/{moment:%Y/%m/%d}/{moment:%Y-%m-%dT%H%M%SZ}--{_slugify(path)}.{ext}.gz"
        )

    def put(
        self,
        source: str,
        url: str,
        raw: bytes,
        *,
        content_type: str = "application/json",
        ext: str = "json",
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Archive one response. Returns the object key.

        Failures here are logged and swallowed. Losing an archive write is a
        nuisance for future backtests; failing the run because of it would cost
        the user a transfer deadline. The archive is a research asset, not a
        correctness dependency - so it must never be on the critical path.
        """
        key = self.key_for(source, url, ext=ext)
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=gzip.compress(raw, compresslevel=6),
                ContentType=content_type,
                ContentEncoding="gzip",
                Metadata={
                    "source": source,
                    "url": url[:1024],
                    "season": self._season,
                    **(metadata or {}),
                },
            )
            logger.debug(
                "Archived raw payload",
                extra={"source": source, "key": key, "bytes": len(raw)},
            )
        except Exception as exc:
            logger.warning(
                "Failed to archive raw payload (continuing)",
                extra={"source": source, "key": key, "error": str(exc)},
            )
        return key

    def put_report(self, gameweek: int, tier: str, html: str) -> str:
        """Keep a copy of every email we send.

        Cheap, and it turns "what did the bot actually say last Thursday?" from
        an archaeology exercise into a single `aws s3 cp`.
        """
        moment = datetime.now(UTC)
        key = f"reports/{self._season}/gw{gameweek:02d}/{tier}-{moment:%Y-%m-%dT%H%M%SZ}.html.gz"
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=gzip.compress(html.encode("utf-8"), compresslevel=6),
                ContentType="text/html; charset=utf-8",
                ContentEncoding="gzip",
            )
        except Exception as exc:
            logger.warning("Failed to archive report", extra={"error": str(exc)})
        return key
