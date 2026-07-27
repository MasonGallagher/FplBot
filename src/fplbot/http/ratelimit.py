"""Per-host request spacing.

We are an unpaid guest on several small servers. PremierInjuries and Fantasy
Football Scout are not Google; a burst of parallel requests from a datacentre IP
is exactly the pattern that gets a User-Agent blocked. Serialising per host with
at least 1.5 seconds between requests keeps us comfortably below anything that
would look like abuse, and costs us nothing - our entire run needs perhaps
fifteen requests.

Note the choice of `time.monotonic()` over `time.time()`. Wall-clock time can
jump backwards (NTP correction, and Lambda's clock is virtualised); a monotonic
clock cannot, so a jump cannot accidentally let a burst through.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

from fplbot.config import HTTP_POLICY


class HostRateLimiter:
    """Enforces a minimum interval between requests to the same host.

    Thread-safe, because although the Lambda handler is single-threaded today,
    the backfill function is an obvious candidate for a thread pool and this is
    the exact place where that would go wrong silently.
    """

    def __init__(self, min_spacing_seconds: float = HTTP_POLICY.min_host_spacing_seconds) -> None:
        self._min_spacing = min_spacing_seconds
        self._last_request_at: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    def _lock_for(self, host: str) -> threading.Lock:
        with self._registry_lock:
            return self._locks.setdefault(host, threading.Lock())

    def acquire(self, url: str) -> float:
        """Block until it is polite to call this host. Returns seconds waited."""
        host = urlparse(url).netloc.lower()
        waited = 0.0
        with self._lock_for(host):
            last = self._last_request_at.get(host)
            if last is not None:
                elapsed = time.monotonic() - last
                if elapsed < self._min_spacing:
                    waited = self._min_spacing - elapsed
                    time.sleep(waited)
            self._last_request_at[host] = time.monotonic()
        return waited
