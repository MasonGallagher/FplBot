"""End-to-end run through `pipeline.py`.

Everything else in this suite tests a piece. This tests the *wiring*: that the
orchestration actually calls the sources, threads the results into the scorer,
builds a board and renders an email - and that it takes the right early exits.

The seams are the two adapters (`DynamoStore`, `RawArchive`) and `send_report`,
all replaced with in-memory fakes, plus `respx` for HTTP. No AWS, no network.

This is the closest thing to the CodeBuild smoke test that can run without
credentials. The smoke test covers what this structurally cannot - IAM
permissions, environment variables, arm64 wheels - which is why both exist.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from fplbot import pipeline
from fplbot.config import HttpPolicy
from fplbot.http import HttpClient as RealHttpClient

from .conftest import DEADLINE_EPOCH, HOUR

FPL_BASE = "https://fantasy.premierleague.com/api"


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------
class FakeDynamoStore:
    """An in-memory stand-in with the same contract as the real store.

    Deliberately hand-written rather than a Mock: the interesting behaviour here
    is the *idempotency lock*, and a Mock would happily let both acquisitions
    succeed and prove nothing.
    """

    def __init__(self) -> None:
        self.snapshots: list[dict] = []
        self.locks: set[tuple[int, str]] = set()
        self.released: list[tuple[int, str]] = []
        self.last_known_good: dict[str, dict] = {}
        self.aliases: dict[str, dict[str, int]] = {}
        self.player_series: dict[int, list[dict]] = {}

    def put_snapshot(self, payload: dict, *, timestamp: str | None = None) -> str:
        self.snapshots.append(payload)
        return timestamp or f"snapshot-{len(self.snapshots)}"

    def recent_snapshots(self, limit: int = 48) -> list[dict]:
        return list(reversed(self.snapshots[-limit:]))

    def acquire_notification_lock(self, gameweek: int, tier: str, *, ttl_days: int = 30) -> None:
        key = (gameweek, tier)
        if key in self.locks:
            from fplbot.storage import LockAlreadyHeld

            raise LockAlreadyHeld(f"GW{gameweek} {tier} already sent")
        self.locks.add(key)

    def release_notification_lock(self, gameweek: int, tier: str) -> None:
        self.locks.discard((gameweek, tier))
        self.released.append((gameweek, tier))

    def put_last_known_good(self, source: str, payload: dict) -> None:
        self.last_known_good[source] = payload

    def get_last_known_good(self, source: str) -> tuple[dict | None, int | None]:
        payload = self.last_known_good.get(source)
        return (payload, 300) if payload else (None, None)

    def get_aliases(self, source: str) -> dict[str, int]:
        return self.aliases.get(source, {})

    def put_alias(self, source: str, source_id: str, element_id: int, **kwargs: Any) -> None:
        self.aliases.setdefault(source, {})[source_id] = element_id

    def put_player_series(self, element_id: int, record: dict) -> None:
        self.player_series.setdefault(element_id, []).append(record)


class FakeArchive:
    """Records what would have been written to S3."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.reports: dict[str, str] = {}

    def put(self, source: str, url: str, raw: bytes, **kwargs: Any) -> str:
        key = f"{source}/{len(self.objects)}"
        self.objects[key] = raw
        return key

    def put_report(self, gameweek: int, tier: str, html: str) -> str:
        key = f"gw{gameweek}-{tier}"
        self.reports[key] = html
        return key


class CapturedEmail:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def __call__(self, subject: str, html_body: str, text_body: str, **kwargs: Any):
        from fplbot.report.email import SendResult

        self.sent.append({"subject": subject, "html": html_body, "text": text_body})
        return SendResult(sent=True, message_id=f"msg-{len(self.sent)}")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
@pytest.fixture
def wired(monkeypatch, bootstrap_payload: dict, fixtures_payload: list[dict]):
    """Replace the I/O adapters and stub every HTTP endpoint."""
    store = FakeDynamoStore()
    archive = FakeArchive()
    email = CapturedEmail()

    monkeypatch.setattr(pipeline, "DynamoStore", lambda *a, **k: store)
    monkeypatch.setattr(pipeline, "RawArchive", lambda *a, **k: archive)
    monkeypatch.setattr(pipeline.email_module, "send_report", email)

    # A real HttpClient, but with no host spacing and no retries, so a run that
    # exercises the genuine fetch path still completes in milliseconds. Note this
    # is the real client - respx intercepts below it, at the transport layer, so
    # the redirect ban and content-type assertion are all still under test.
    fast = HttpPolicy(
        min_host_spacing_seconds=0.0,
        max_attempts=1,
        backoff_base_seconds=0.001,
        backoff_cap_seconds=0.002,
    )
    monkeypatch.setattr(pipeline, "HttpClient", lambda: RealHttpClient(policy=fast))

    return {
        "store": store,
        "archive": archive,
        "email": email,
        "bootstrap": bootstrap_payload,
        "fixtures": fixtures_payload,
    }


def stub_endpoints(bootstrap_payload: dict, fixtures_payload: list[dict]) -> None:
    """Mock every outbound call the pipeline can make.

    Third-party sources return failures on purpose in most tests: the pipeline's
    headline promise is that it degrades a feature rather than the run, and the
    only way to test that is to make them fail.
    """
    respx.get(f"{FPL_BASE}/bootstrap-static/").mock(
        return_value=httpx.Response(
            200,
            content=json.dumps(bootstrap_payload).encode(),
            headers={"content-type": "application/json"},
        )
    )
    respx.get(f"{FPL_BASE}/fixtures/").mock(
        return_value=httpx.Response(
            200,
            content=json.dumps(fixtures_payload).encode(),
            headers={"content-type": "application/json"},
        )
    )
    # Third parties: unavailable. The run must still produce a board.
    respx.route(host="api.clubelo.com").mock(return_value=httpx.Response(503))
    respx.route(host="www.fantasyfootballscout.co.uk").mock(return_value=httpx.Response(503))
    respx.route(host="www.premierinjuries.com").mock(return_value=httpx.Response(503))
    respx.route(host="understat.com").mock(return_value=httpx.Response(503))


# ---------------------------------------------------------------------------
# Early exits
# ---------------------------------------------------------------------------
class TestEarlyExits:
    @respx.mock
    def test_outside_a_window_it_snapshots_and_stops(self, wired) -> None:
        """The common case: 750 invocations a month, ~730 of them end here."""
        stub_endpoints(wired["bootstrap"], wired["fixtures"])

        outcome = pipeline.run(now_epoch=DEADLINE_EPOCH - 100 * HOUR)

        assert outcome.status == "snapshot_only"
        assert len(wired["store"].snapshots) == 1, "the snapshot happens regardless"
        assert wired["email"].sent == []

    @respx.mock
    def test_off_season_exits_cleanly(self, wired) -> None:
        """No events at all. A normal outcome, not an error."""
        payload = dict(wired["bootstrap"], events=[])
        stub_endpoints(payload, wired["fixtures"])

        outcome = pipeline.run(now_epoch=DEADLINE_EPOCH)

        assert outcome.status == "no_deadline"
        assert len(wired["store"].snapshots) == 1, "we still snapshot in the off-season"

    @respx.mock
    def test_a_snapshot_is_taken_on_every_run(self, wired) -> None:
        """The series is the only training set and the only source of
        intra-gameweek velocity. An hour not snapshotted is lost forever."""
        stub_endpoints(wired["bootstrap"], wired["fixtures"])

        for offset in (100, 90, 80):
            pipeline.run(now_epoch=DEADLINE_EPOCH - offset * HOUR)

        assert len(wired["store"].snapshots) == 3


# ---------------------------------------------------------------------------
# A full run
# ---------------------------------------------------------------------------
class TestFullRun:
    @respx.mock
    def test_produces_and_sends_a_board(self, wired) -> None:
        stub_endpoints(wired["bootstrap"], wired["fixtures"])

        outcome = pipeline.run(now_epoch=DEADLINE_EPOCH - 47 * HOUR)

        assert outcome.status == "sent"
        assert outcome.gameweek == 1
        assert outcome.tier == "48h"
        assert outcome.recommendations > 0

        assert len(wired["email"].sent) == 1
        message = wired["email"].sent[0]
        assert "GW1" in message["subject"]
        assert "provisional" in message["subject"]

    @respx.mock
    def test_the_email_carries_every_section(self, wired) -> None:
        stub_endpoints(wired["bootstrap"], wired["fixtures"])

        pipeline.run(now_epoch=DEADLINE_EPOCH - 47 * HOUR)
        html = wired["email"].sent[0]["html"]

        for heading in (
            "Buy board",
            "Sell / avoid",
            "Injury-signal watchlist",
            "Returning from injury",
            "Caveats",
        ):
            assert heading in html

    @respx.mock
    def test_degraded_sources_are_named_in_the_email(self, wired) -> None:
        """The headline failure promise: degrade a feature, not the run.

        Every third-party source is returning 503 in this test, and the run still
        produces a board - with the degradation stated rather than hidden.
        """
        stub_endpoints(wired["bootstrap"], wired["fixtures"])

        outcome = pipeline.run(now_epoch=DEADLINE_EPOCH - 47 * HOUR)
        html = wired["email"].sent[0]["html"]

        assert outcome.status == "sent"
        assert "Degraded" in html or "unavailable" in html

    @respx.mock
    def test_the_report_is_archived(self, wired) -> None:
        stub_endpoints(wired["bootstrap"], wired["fixtures"])

        pipeline.run(now_epoch=DEADLINE_EPOCH - 47 * HOUR)

        assert wired["archive"].reports
        assert wired["archive"].objects, "raw payloads should be archived too"

    @respx.mock
    def test_the_three_hour_report_is_marked_confirmed(self, wired) -> None:
        """The one the user should act on."""
        stub_endpoints(wired["bootstrap"], wired["fixtures"])

        outcome = pipeline.run(now_epoch=DEADLINE_EPOCH - 2 * HOUR)

        assert outcome.tier == "3h"
        assert "FINAL" in wired["email"].sent[0]["subject"]
        assert "CONFIRMED" in wired["email"].sent[0]["html"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
class TestIdempotency:
    @respx.mock
    def test_a_second_run_in_the_same_tier_is_suppressed(self, wired) -> None:
        """Scheduler is at-least-once. A duplicate email erodes trust in every
        future one, so the conditional write is the guard."""
        stub_endpoints(wired["bootstrap"], wired["fixtures"])

        first = pipeline.run(now_epoch=DEADLINE_EPOCH - 47 * HOUR)
        second = pipeline.run(now_epoch=DEADLINE_EPOCH - 46 * HOUR)

        assert first.status == "sent"
        assert second.status == "suppressed"
        assert len(wired["email"].sent) == 1

    @respx.mock
    def test_a_different_tier_still_sends(self, wired) -> None:
        """The lock is per (gameweek, tier), not per gameweek."""
        stub_endpoints(wired["bootstrap"], wired["fixtures"])

        pipeline.run(now_epoch=DEADLINE_EPOCH - 47 * HOUR)  # 48h
        pipeline.run(now_epoch=DEADLINE_EPOCH - 20 * HOUR)  # 24h

        assert len(wired["email"].sent) == 2
        assert wired["store"].locks == {(1, "48h"), (1, "24h")}

    @respx.mock
    def test_force_tier_still_respects_the_lock(self, wired) -> None:
        """A debug affordance that can spam the user is not a debug affordance."""
        stub_endpoints(wired["bootstrap"], wired["fixtures"])

        first = pipeline.run(now_epoch=DEADLINE_EPOCH - 200 * HOUR, force_tier="48h")
        second = pipeline.run(now_epoch=DEADLINE_EPOCH - 200 * HOUR, force_tier="48h")

        assert first.status == "sent"
        assert second.status == "suppressed"


# ---------------------------------------------------------------------------
# Hard failures
# ---------------------------------------------------------------------------
class TestHardFailures:
    @respx.mock
    def test_bootstrap_unavailable_with_no_cache_is_fatal(self, wired) -> None:
        """The only required source. No gameweek, no deadline, nothing to say."""
        respx.get(f"{FPL_BASE}/bootstrap-static/").mock(return_value=httpx.Response(503))

        with pytest.raises(RuntimeError, match="Required source"):
            pipeline.run(now_epoch=DEADLINE_EPOCH - 47 * HOUR)

    @respx.mock
    def test_the_updating_page_is_rejected(self, wired) -> None:
        """FPL's maintenance page returns HTTP 200 with an HTML body.

        Status-code checking alone does not catch it, so the content-type
        assertion has to - and it must propagate as a required-source failure
        rather than as an empty board.
        """
        respx.get(f"{FPL_BASE}/bootstrap-static/").mock(
            return_value=httpx.Response(
                200,
                content=b"<html>The game is being updated.</html>",
                headers={"content-type": "text/html"},
            )
        )

        with pytest.raises(RuntimeError, match="Required source"):
            pipeline.run(now_epoch=DEADLINE_EPOCH - 47 * HOUR)

    @respx.mock
    def test_bootstrap_falls_back_to_last_known_good(self, wired) -> None:
        """A cached bootstrap keeps the run alive."""
        stub_endpoints(wired["bootstrap"], wired["fixtures"])
        pipeline.run(now_epoch=DEADLINE_EPOCH - 100 * HOUR)  # populates LKG

        respx.get(f"{FPL_BASE}/bootstrap-static/").mock(return_value=httpx.Response(503))

        outcome = pipeline.run(now_epoch=DEADLINE_EPOCH - 47 * HOUR)

        assert outcome.status == "sent"
        html = wired["email"].sent[0]["html"]
        assert "fpl" in html.lower()
