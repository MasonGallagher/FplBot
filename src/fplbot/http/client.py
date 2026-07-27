"""The one hardened HTTP client.

Every rule in SPEC section 4 that applies to outbound requests is implemented
here, once, so that no individual source module can forget one:

* `Accept-Encoding: gzip`   - the bootstrap payload is ~3 MB uncompressed.
* An honest User-Agent with a contact URL.
* **Redirects disabled.** This is not a stylistic choice. During maintenance the
  FPL API redirects to an `/updating/` page which returns **HTTP 200 with an HTML
  body**. A client that follows redirects and only checks the status code will
  happily hand you a parse error at best, and at worst a well-formed-looking
  empty result. Any 3xx is treated as a failure.
* A content-type assertion before parsing, which is the second half of the same
  defence.
* Per-host serialisation with >= 1.5 s spacing.
* Exponential backoff with **full jitter** (base 1 s, cap 60 s, 3 attempts).
* A per-source circuit breaker.
* Retry **only** on 429/500/502/503/504 and connection errors. Never on 403 or
  404. A 403 is a Cloudflare challenge and retrying looks like an attack;
  Understat's 404 means a missing header, and no amount of retrying adds one.

Full jitter deserves a word, since "exponential backoff" is often implemented
without it. The naive version sleeps exactly 1 s, 2 s, 4 s. If several clients
fail at the same moment - which is precisely what happens when a server has a
blip - they all retry at the same moment too, and hammer the recovering server
in synchronised waves. Full jitter draws the sleep uniformly from [0, backoff],
which spreads the retries out. It is one line and it is strictly better.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import orjson
from aws_lambda_powertools.metrics import MetricUnit

from fplbot.config import HTTP_POLICY, USER_AGENT, HttpPolicy
from fplbot.http.breaker import CircuitBreaker
from fplbot.http.ratelimit import HostRateLimiter
from fplbot.observability import Metric, count, emit, logger, tracer


class HttpFetchError(RuntimeError):
    """A request failed in a way the caller must handle (degrade or abandon)."""

    def __init__(self, source: str, url: str, reason: str, status: int | None = None) -> None:
        super().__init__(f"{source}: {reason} ({url})")
        self.source = source
        self.url = url
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class FetchResult:
    """A successful response.

    `raw` is the **verbatim decoded body bytes**, and it is what gets archived to
    S3. Archiving a re-serialised parse would be a subtle disaster: it bakes
    today's understanding of the schema into the archive, so when the schema
    drifts you can no longer replay the original bytes to find out what actually
    changed. SPEC section 2.
    """

    source: str
    url: str
    status: int
    raw: bytes
    headers: dict[str, str]
    fetched_at_epoch: int
    duration_ms: float

    def json(self) -> Any:
        """Parse the body as JSON with orjson."""
        try:
            return orjson.loads(self.raw)
        except orjson.JSONDecodeError as exc:
            raise HttpFetchError(self.source, self.url, f"body was not valid JSON: {exc}") from exc

    def text(self, encoding: str = "utf-8") -> str:
        """Decode the body as text, tolerating the odd stray byte.

        `errors="replace"` rather than strict: PremierInjuries and FFS both emit
        the occasional mis-encoded character in player names, and losing an
        entire injury table to one bad byte would be a poor trade.
        """
        return self.raw.decode(encoding, errors="replace")


@dataclass
class HttpClient:
    """Shared client. Construct once per invocation and pass it to the sources."""

    policy: HttpPolicy = HTTP_POLICY
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    limiter: HostRateLimiter = field(default_factory=HostRateLimiter)
    _client: httpx.Client | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._client is None:
            self._client = httpx.Client(
                # THE important one. See the module docstring.
                follow_redirects=False,
                timeout=httpx.Timeout(
                    connect=self.policy.connect_timeout_seconds,
                    read=self.policy.read_timeout_seconds,
                    write=self.policy.read_timeout_seconds,
                    pool=self.policy.read_timeout_seconds,
                ),
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept-Encoding": "gzip, deflate",
                },
                # One connection per host is plenty and reinforces the politeness
                # policy at the transport level as well as the application level.
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- the single entry point -------------------------------------------

    @tracer.capture_method
    def fetch(
        self,
        source: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        expect_content_type: str | tuple[str, ...] = "application/json",
        allow_statuses: tuple[int, ...] = (200,),
    ) -> FetchResult:
        """Fetch a URL under the full policy.

        Args:
            source: Logical source name, e.g. "fpl". Drives the circuit breaker
                and the metric dimensions. Use the same string consistently.
            expect_content_type: Substring(s) the response Content-Type must
                contain. Pass a tuple when a source is inconsistent (ClubElo
                serves CSV as text/plain on some paths and text/csv on others).
                Pass "" to skip the check entirely - only sensible for binary.
            allow_statuses: Statuses treated as success. Almost always (200,).

        Raises:
            BreakerOpen: the source is cut off; caller should degrade.
            HttpFetchError: the request failed after the retry policy was spent.
        """
        assert self._client is not None  # set in __post_init__

        self.breaker.check(source)

        expected = (
            (expect_content_type,) if isinstance(expect_content_type, str) else expect_content_type
        )
        last_error: str = "no attempt made"
        last_status: int | None = None

        for attempt in range(1, self.policy.max_attempts + 1):
            waited = self.limiter.acquire(url)
            if waited > 0:
                logger.debug(
                    "Waited to respect host spacing",
                    extra={"source": source, "waited_s": round(waited, 2)},
                )

            started = time.perf_counter()
            try:
                response = self._client.get(url, headers=headers, params=params)
            except httpx.TransportError as exc:
                # Connection-level failure: DNS, TCP, TLS, read timeout. These
                # are the canonical retryable errors.
                last_error = f"transport error: {type(exc).__name__}: {exc}"
                last_status = None
                if self._sleep_before_retry(attempt, source, last_error):
                    continue
                break

            duration_ms = (time.perf_counter() - started) * 1000

            # --- Redirects are always a failure ---------------------------
            if 300 <= response.status_code < 400:
                location = response.headers.get("location", "<none>")
                self.breaker.record_failure(source)
                self._emit_failure(source, duration_ms)
                raise HttpFetchError(
                    source,
                    url,
                    f"unexpected redirect {response.status_code} to {location}. "
                    "This is how FPL signals maintenance (/updating/ returns 200 with HTML), "
                    "so we treat any 3xx as a hard failure rather than following it.",
                    status=response.status_code,
                )

            # --- Success --------------------------------------------------
            if response.status_code in allow_statuses:
                self._assert_content_type(source, url, response, expected)
                self.breaker.record_success(source)
                count(Metric.SOURCE_FETCH_OK, source=source)
                emit(
                    Metric.SOURCE_FETCH_DURATION,
                    duration_ms,
                    MetricUnit.Milliseconds,
                    source=source,
                )
                logger.debug(
                    "Fetched",
                    extra={
                        "source": source,
                        "url": url,
                        "status": response.status_code,
                        "bytes": len(response.content),
                        "duration_ms": round(duration_ms, 1),
                        "attempt": attempt,
                    },
                )
                return FetchResult(
                    source=source,
                    url=url,
                    status=response.status_code,
                    raw=response.content,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    fetched_at_epoch=int(time.time()),
                    duration_ms=duration_ms,
                )

            # --- Failure: retryable or not? -------------------------------
            last_status = response.status_code
            last_error = f"HTTP {response.status_code}"

            if response.status_code not in self.policy.retry_statuses:
                # Terminal. Give the caller a diagnosis rather than a bare code,
                # because these two in particular have specific known causes.
                hint = _terminal_status_hint(response.status_code, source)
                self.breaker.record_failure(source)
                self._emit_failure(source, duration_ms)
                raise HttpFetchError(source, url, f"{last_error}. {hint}", status=last_status)

            if not self._sleep_before_retry(attempt, source, last_error):
                break

        # Retries exhausted.
        self.breaker.record_failure(source)
        self._emit_failure(source, 0.0)
        raise HttpFetchError(
            source,
            url,
            f"giving up after {self.policy.max_attempts} attempts; last error: {last_error}",
            status=last_status,
        )

    # -- internals ---------------------------------------------------------

    def _sleep_before_retry(self, attempt: int, source: str, reason: str) -> bool:
        """Sleep with full jitter. Returns False when attempts are exhausted."""
        if attempt >= self.policy.max_attempts:
            return False
        ceiling = min(
            self.policy.backoff_cap_seconds,
            self.policy.backoff_base_seconds * (2 ** (attempt - 1)),
        )
        # Full jitter: uniform over [0, ceiling], not the ceiling itself.
        delay = random.uniform(0, ceiling)
        logger.warning(
            "Retrying after transient failure",
            extra={
                "source": source,
                "attempt": attempt,
                "reason": reason,
                "sleep_s": round(delay, 2),
            },
        )
        time.sleep(delay)
        return True

    def _assert_content_type(
        self,
        source: str,
        url: str,
        response: httpx.Response,
        expected: tuple[str, ...],
    ) -> None:
        """Second half of the `/updating/` defence.

        A 200 with `text/html` where we asked for JSON means we are looking at a
        maintenance page or an interstitial, not data. Catching it here means the
        parser never sees it and we get a clear error rather than a confusing one.
        """
        if expected == ("",):
            return
        content_type = response.headers.get("content-type", "").lower()
        if any(exp.lower() in content_type for exp in expected):
            return
        self.breaker.record_failure(source)
        preview = response.content[:120].decode("utf-8", errors="replace")
        raise HttpFetchError(
            source,
            url,
            f"expected content-type in {expected} but got {content_type!r}. "
            f"Body starts: {preview!r}. A 200 with HTML where JSON was expected is "
            "usually a maintenance or challenge page, not data.",
            status=response.status_code,
        )

    @staticmethod
    def _emit_failure(source: str, duration_ms: float) -> None:
        count(Metric.SOURCE_FETCH_FAILED, source=source)


def _terminal_status_hint(status: int, source: str) -> str:
    """Turn a bare status code into something actionable in the logs."""
    if status == 403:
        return (
            "403 is a Cloudflare / WAF block, not a transient fault. We do not retry it: "
            "retrying escalates the block and looks like an attack. If this is persistent, "
            "the source is unusable from a datacentre IP and should be removed from the run."
        )
    if status == 404:
        if source == "understat":
            return (
                "Understat returns 404 for EVERY endpoint when the header "
                "'X-Requested-With: XMLHttpRequest' is missing. Check the header before "
                "concluding the resource is gone."
            )
        return "404 is not retryable. Check the path - FPL endpoints require a trailing slash."
    if status == 401:
        return "401 - credentials missing or wrong. Check the SSM parameter."
    if status == 422:
        return "422 - the request shape was rejected. Check query parameters."
    return "Not in the retryable set, so treated as terminal."
