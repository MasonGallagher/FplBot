"""The hardened HTTP client.

Every rule in SPEC section 4 that applies to outbound requests has a test here.
`respx` intercepts at the httpx transport layer, so the client's real code path
runs end to end without a socket ever being opened.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from fplbot.config import HttpPolicy
from fplbot.http.breaker import BreakerOpen, CircuitBreaker
from fplbot.http.client import HttpClient, HttpFetchError
from fplbot.http.ratelimit import HostRateLimiter

# No spacing and no sleeping, so the suite runs in milliseconds. The spacing
# behaviour itself is tested separately against the limiter.
FAST_POLICY = HttpPolicy(
    min_host_spacing_seconds=0.0,
    backoff_base_seconds=0.001,
    backoff_cap_seconds=0.002,
    max_attempts=3,
)


@pytest.fixture
def client() -> HttpClient:
    with HttpClient(policy=FAST_POLICY) as instance:
        yield instance


class TestSuccess:
    @respx.mock
    def test_returns_verbatim_bytes(self, client: HttpClient) -> None:
        """The archive stores raw bytes, never a re-serialised parse.

        A re-serialised dump contains today's *interpretation* of the payload -
        dropped fields are gone, coerced types are coerced - and destroys the
        evidence you need when the schema drifts.
        """
        body = b'{"hello": "world", "unknown_field": 1}'
        respx.get("https://example.com/data").mock(
            return_value=httpx.Response(
                200, content=body, headers={"content-type": "application/json"}
            )
        )

        result = client.fetch("test", "https://example.com/data")

        assert result.raw == body
        assert result.json() == {"hello": "world", "unknown_field": 1}

    @respx.mock
    def test_sends_an_honest_user_agent(self, client: HttpClient) -> None:
        route = respx.get("https://example.com/x").mock(
            return_value=httpx.Response(200, json={}, headers={"content-type": "application/json"})
        )

        client.fetch("test", "https://example.com/x")

        user_agent = route.calls[0].request.headers["user-agent"]
        assert "fplBot" in user_agent
        assert "+http" in user_agent, "a contact URL lets an operator reach us"

    @respx.mock
    def test_custom_headers_are_passed_through(self, client: HttpClient) -> None:
        """Understat 404s on every endpoint without X-Requested-With."""
        route = respx.get("https://understat.com/getLeagueData/EPL/2026").mock(
            return_value=httpx.Response(200, json={}, headers={"content-type": "application/json"})
        )

        client.fetch(
            "understat",
            "https://understat.com/getLeagueData/EPL/2026",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        assert route.calls[0].request.headers["x-requested-with"] == "XMLHttpRequest"


class TestRedirects:
    @respx.mock
    def test_a_redirect_is_a_hard_failure(self, client: HttpClient) -> None:
        """THE `/updating/` defence.

        During maintenance FPL redirects to a page returning **HTTP 200 with an
        HTML body**. A client that follows redirects and checks only the status
        code gets a parse error at best and a plausible-looking empty result at
        worst.
        """
        respx.get("https://fantasy.premierleague.com/api/bootstrap-static/").mock(
            return_value=httpx.Response(302, headers={"location": "/updating/"})
        )

        with pytest.raises(HttpFetchError) as exc_info:
            client.fetch("fpl", "https://fantasy.premierleague.com/api/bootstrap-static/")

        assert "redirect" in str(exc_info.value).lower()
        assert "/updating/" in str(exc_info.value)

    @respx.mock
    def test_redirects_are_not_followed(self, client: HttpClient) -> None:
        redirect = respx.get("https://example.com/a").mock(
            return_value=httpx.Response(301, headers={"location": "https://example.com/b"})
        )
        target = respx.get("https://example.com/b").mock(return_value=httpx.Response(200, json={}))

        with pytest.raises(HttpFetchError):
            client.fetch("test", "https://example.com/a")

        assert redirect.called
        assert not target.called, "the redirect must not be followed"


class TestContentType:
    @respx.mock
    def test_html_where_json_expected_is_rejected(self, client: HttpClient) -> None:
        """The second half of the `/updating/` defence.

        A 200 with text/html where we asked for JSON is a maintenance page or an
        interstitial, not data.
        """
        respx.get("https://example.com/api").mock(
            return_value=httpx.Response(
                200,
                content=b"<html><body>We'll be back shortly</body></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )

        with pytest.raises(HttpFetchError, match="content-type"):
            client.fetch("test", "https://example.com/api")

    @respx.mock
    def test_multiple_acceptable_types(self, client: HttpClient) -> None:
        """ClubElo serves CSV as text/plain on some paths and text/csv on others."""
        respx.get("http://api.clubelo.com/Fixtures").mock(
            return_value=httpx.Response(
                200, content=b"a,b\n1,2\n", headers={"content-type": "text/plain"}
            )
        )

        result = client.fetch(
            "clubelo",
            "http://api.clubelo.com/Fixtures",
            expect_content_type=("text/csv", "text/plain"),
        )

        assert result.text().startswith("a,b")


class TestRetryPolicy:
    @respx.mock
    def test_retries_a_503(self, client: HttpClient) -> None:
        route = respx.get("https://example.com/flaky").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(
                    200, json={"ok": True}, headers={"content-type": "application/json"}
                ),
            ]
        )

        result = client.fetch("test", "https://example.com/flaky")

        assert result.json() == {"ok": True}
        assert route.call_count == 2

    @respx.mock
    def test_never_retries_a_403(self, client: HttpClient) -> None:
        """A 403 is a Cloudflare challenge.

        Retrying escalates the block and looks like an attack. This is why FBref
        is excluded from the project entirely.
        """
        route = respx.get("https://example.com/blocked").mock(return_value=httpx.Response(403))

        with pytest.raises(HttpFetchError) as exc_info:
            client.fetch("test", "https://example.com/blocked")

        assert route.call_count == 1, "403 must be terminal, not retried"
        assert "Cloudflare" in str(exc_info.value)

    @respx.mock
    def test_never_retries_a_404(self, client: HttpClient) -> None:
        route = respx.get("https://understat.com/getLeagueData/EPL/2026").mock(
            return_value=httpx.Response(404)
        )

        with pytest.raises(HttpFetchError) as exc_info:
            client.fetch("understat", "https://understat.com/getLeagueData/EPL/2026")

        assert route.call_count == 1
        assert "X-Requested-With" in str(exc_info.value), (
            "the error should name the actual cause for this source"
        )

    @respx.mock
    def test_retries_a_connection_error(self, client: HttpClient) -> None:
        route = respx.get("https://example.com/timeout").mock(
            side_effect=[
                httpx.ConnectTimeout("timed out"),
                httpx.Response(200, json={}, headers={"content-type": "application/json"}),
            ]
        )

        client.fetch("test", "https://example.com/timeout")

        assert route.call_count == 2

    @respx.mock
    def test_gives_up_after_max_attempts(self, client: HttpClient) -> None:
        route = respx.get("https://example.com/down").mock(return_value=httpx.Response(503))

        with pytest.raises(HttpFetchError, match="giving up"):
            client.fetch("test", "https://example.com/down")

        assert route.call_count == FAST_POLICY.max_attempts


class TestCircuitBreaker:
    def test_trips_after_the_threshold(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3, reset_seconds=60)

        for _ in range(3):
            breaker.record_failure("understat")

        with pytest.raises(BreakerOpen):
            breaker.check("understat")

    def test_a_success_resets_the_count(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3, reset_seconds=60)

        breaker.record_failure("understat")
        breaker.record_failure("understat")
        breaker.record_success("understat")
        breaker.record_failure("understat")

        breaker.check("understat")  # must not raise

    def test_isolated_per_source(self) -> None:
        """Understat being down must not stop us using ClubElo."""
        breaker = CircuitBreaker(failure_threshold=2, reset_seconds=60)

        breaker.record_failure("understat")
        breaker.record_failure("understat")

        with pytest.raises(BreakerOpen):
            breaker.check("understat")
        breaker.check("clubelo")  # unaffected

    def test_open_sources_are_reportable(self) -> None:
        """Feeds the data-quality line in the email."""
        breaker = CircuitBreaker(failure_threshold=1, reset_seconds=60)
        breaker.record_failure("understat")

        assert breaker.open_sources() == ["understat"]

    @respx.mock
    def test_the_client_short_circuits_an_open_source(self) -> None:
        with HttpClient(policy=HttpPolicy(min_host_spacing_seconds=0.0, max_attempts=1)) as client:
            route = respx.get("https://example.com/x").mock(return_value=httpx.Response(500))

            for _ in range(3):
                with pytest.raises(HttpFetchError):
                    client.fetch("test", "https://example.com/x")

            calls_before = route.call_count
            with pytest.raises(BreakerOpen):
                client.fetch("test", "https://example.com/x")

            assert route.call_count == calls_before, "no request should have been made"


class TestRateLimiter:
    def test_spacing_is_enforced_per_host(self) -> None:
        limiter = HostRateLimiter(min_spacing_seconds=0.05)

        first = limiter.acquire("https://example.com/a")
        second = limiter.acquire("https://example.com/b")

        assert first == 0.0, "the first request to a host never waits"
        assert second > 0.0, "the second to the same host must wait"

    def test_different_hosts_do_not_block_each_other(self) -> None:
        limiter = HostRateLimiter(min_spacing_seconds=0.05)

        limiter.acquire("https://example.com/a")
        waited = limiter.acquire("https://other.example.org/a")

        assert waited == 0.0
