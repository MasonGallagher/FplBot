"""The single HTTP client every source must go through.

No source module may call httpx directly. Routing everything through one client
means the retry policy, the rate limiting, the redirect ban, the content-type
assertion and the circuit breaker are applied uniformly and cannot be forgotten
in a module written six months from now.
"""

from fplbot.http.breaker import BreakerOpen, CircuitBreaker
from fplbot.http.client import FetchResult, HttpClient, HttpFetchError

__all__ = ["BreakerOpen", "CircuitBreaker", "FetchResult", "HttpClient", "HttpFetchError"]
