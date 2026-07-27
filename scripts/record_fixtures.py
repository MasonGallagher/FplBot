#!/usr/bin/env python3
"""Record live API responses to disk, and hand-craft the awkward cases.

WHY THIS EXISTS, AND WHY IT COMES BEFORE THE MODEL
--------------------------------------------------
SPEC section 7 puts this third in the build order, ahead of any model logic, and
the reasoning is worth stating plainly:

    You are building in pre-season against data that looks materially different
    in August.

Right now `bootstrap-static` is a *mixture*. `minutes`, `total_points`, `bps` and
`starts` hold last season's values; `form`, `event_points` and every
`transfers_*` field are zero. Understat returns `{"teams":[],"players":[]}`. Every
DefCon field is zero for every player. Code written and tested against that will
behave differently in August, and you will not find out until it matters.

Recorded fixtures are the only way to test August behaviour in July - and the
synthetic ones below are the only way to test behaviour that has not happened
yet at all.

    ./scripts/record_fixtures.py                  record everything live
    ./scripts/record_fixtures.py --synthetic-only just build the hand-crafted set
    ./scripts/record_fixtures.py --source fpl     record one source

Output goes to `fixtures/recorded/{timestamp}/`, which is gitignored. The curated
synthetic set goes to `tests/fixtures/`, which IS committed - those are the ones
the test suite replays.

This script talks to live third-party APIs. It is a developer tool, run by hand.
It is never invoked by the Lambda, and `tests/` never invokes it either.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Make the package importable when run directly from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402

from fplbot.config import USER_AGENT, Endpoints  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RECORDED_DIR = REPO_ROOT / "fixtures" / "recorded"
CURATED_DIR = REPO_ROOT / "tests" / "fixtures"

HOUR = 3600


# ---------------------------------------------------------------------------
# Live recording
# ---------------------------------------------------------------------------
def record_live(sources: list[str], output_dir: Path) -> None:
    """Fetch each endpoint once and write the verbatim bytes.

    Deliberately NOT using `fplbot.http.HttpClient`: that client enforces a
    circuit breaker and a retry budget appropriate to a 120-second Lambda, and
    would abandon a source part-way through a recording session. Here we want
    every endpoint attempted, and a failure recorded rather than escalated.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    targets: dict[str, list[tuple[str, str, dict[str, str]]]] = {
        "fpl": [
            ("bootstrap-static", f"{Endpoints.FPL}bootstrap-static/", {}),
            ("fixtures", f"{Endpoints.FPL}fixtures/", {}),
            ("event-status", f"{Endpoints.FPL}event-status/", {}),
            # One element-summary, as a shape sample. Element 1 always exists.
            ("element-summary-1", f"{Endpoints.FPL}element-summary/1/", {}),
        ],
        "clubelo": [
            (
                "ratings",
                f"{Endpoints.CLUBELO}/{datetime.now(UTC):%Y-%m-%d}",
                {},
            ),
            ("fixtures", f"{Endpoints.CLUBELO}/Fixtures", {}),
        ],
        "understat": [
            # The header is not optional. Without it EVERY endpoint 404s, and
            # that 404 means "missing header", not "missing resource".
            (
                "league-2026",
                f"{Endpoints.UNDERSTAT}/getLeagueData/EPL/2026",
                {"X-Requested-With": "XMLHttpRequest"},
            ),
            (
                "league-2025",
                f"{Endpoints.UNDERSTAT}/getLeagueData/EPL/2025",
                {"X-Requested-With": "XMLHttpRequest"},
            ),
        ],
        "ffs": [("team-news", Endpoints.FFS_TEAM_NEWS, {})],
        "premierinjuries": [("injury-table", Endpoints.PREMIER_INJURIES, {})],
    }

    with httpx.Client(
        timeout=30.0,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
    ) as client:
        for source in sources:
            if source not in targets:
                print(f"  ! unknown source {source!r}, skipping")
                continue

            source_dir = output_dir / source
            source_dir.mkdir(parents=True, exist_ok=True)

            for name, url, headers in targets[source]:
                print(f"  {source}/{name} <- {url}")
                try:
                    response = client.get(url, headers=headers)
                except Exception as exc:  # noqa: BLE001 - record the failure, continue
                    print(f"    FAILED: {exc}")
                    (source_dir / f"{name}.error.txt").write_text(str(exc), encoding="utf-8")
                    continue

                content_type = response.headers.get("content-type", "")
                extension = (
                    "json" if "json" in content_type
                    else "html" if "html" in content_type
                    else "csv" if "csv" in content_type or "plain" in content_type
                    else "txt"
                )
                target = source_dir / f"{name}.{extension}"
                target.write_bytes(response.content)

                # Headers are worth keeping. `x-requests-remaining` on the Odds
                # API and the cache headers on FPL are both things you will want
                # to look at later and cannot reconstruct from the body.
                (source_dir / f"{name}.headers.json").write_text(
                    json.dumps(
                        {
                            "url": url,
                            "status": response.status_code,
                            "headers": dict(response.headers),
                            "recorded_at": datetime.now(UTC).isoformat(),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                print(f"    {response.status_code}  {len(response.content):,} bytes -> {target.name}")


# ---------------------------------------------------------------------------
# Synthetic fixtures - the branches that will not occur before they matter
# ---------------------------------------------------------------------------
def build_synthetic(output_dir: Path) -> None:
    """Hand-craft the cases live recording cannot capture.

    SPEC section 7 names these specifically, and the common thread is that each
    is *both* the most likely to be wrong *and* the least likely to be exercised
    before it matters. A blank gameweek arrives in December; if the code is wrong
    you find out in December, with a deadline three hours away.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    writers = {
        "double_gameweek.json": _double_gameweek,
        "blank_gameweek.json": _blank_gameweek,
        "postponed_fixture.json": _postponed_fixture,
        "off_season_bootstrap.json": _off_season_bootstrap,
        "season_end_bootstrap.json": _season_end_bootstrap,
        "understat_empty_preseason.json": _understat_empty,
        "chip_week_event.json": _chip_week_event,
        "rule_override_event.json": _rule_override_event,
    }

    for filename, builder in writers.items():
        path = output_dir / filename
        path.write_text(json.dumps(builder(), indent=2), encoding="utf-8")
        print(f"  wrote {path.relative_to(REPO_ROOT)}")

    # `updating.html` is not JSON - it is the maintenance page FPL serves with
    # HTTP 200. Status-code checking alone does not catch it, which is exactly
    # why we need a fixture of it.
    (output_dir / "updating.html").write_text(_updating_page(), encoding="utf-8")
    print(f"  wrote {(output_dir / 'updating.html').relative_to(REPO_ROOT)}")


def _base_deadline() -> int:
    """A Saturday 11:00 UTC deadline, as an epoch."""
    return 1_786_662_000


def _double_gameweek() -> dict:
    """Team 1 plays three times - a TRIPLE, not a double.

    Deliberately three rather than two. The test for a double is `>= 2`, and a
    fixture with exactly two would pass an incorrect `== 2` implementation.
    """
    return {
        "description": "GW20: team 1 plays three times; teams 2, 3, 4 once each.",
        "fixtures": [
            {"id": 200, "event": 20, "team_h": 1, "team_a": 2,
             "team_h_difficulty": 2, "team_a_difficulty": 4,
             "kickoff_time": "2026-12-26T15:00:00Z", "finished": False,
             "provisional_start_time": False, "stats": []},
            {"id": 201, "event": 20, "team_h": 3, "team_a": 1,
             "team_h_difficulty": 4, "team_a_difficulty": 2,
             "kickoff_time": "2026-12-28T20:00:00Z", "finished": False,
             "provisional_start_time": False, "stats": []},
            {"id": 202, "event": 20, "team_h": 1, "team_a": 4,
             "team_h_difficulty": 2, "team_a_difficulty": 4,
             "kickoff_time": "2026-12-30T19:30:00Z", "finished": False,
             "provisional_start_time": False, "stats": []},
        ],
    }


def _blank_gameweek() -> dict:
    """Only six fixtures - eight teams have no game at all.

    Note there are six fixtures here, not ten. Any code testing
    `len(fixtures) > 10` to detect an irregular gameweek gets this wrong in both
    directions: it is neither necessary nor sufficient.
    """
    return {
        "description": "GW29: FA Cup weekend. 12 teams play, 8 blank.",
        "fixtures": [
            {"id": 290 + i, "event": 29, "team_h": 1 + i * 2, "team_a": 2 + i * 2,
             "team_h_difficulty": 3, "team_a_difficulty": 3,
             "kickoff_time": "2027-03-13T15:00:00Z", "finished": False,
             "provisional_start_time": False, "stats": []}
            for i in range(6)
        ],
    }


def _postponed_fixture() -> dict:
    """`event: null` and `provisional_start_time: true`, the two postponement shapes."""
    return {
        "description": "One unscheduled fixture and one with a TBC kickoff time.",
        "fixtures": [
            # Unscheduled: belongs to no gameweek. Including it in a gameweek
            # would invent a match that is not being played.
            {"id": 400, "event": None, "team_h": 5, "team_a": 6,
             "team_h_difficulty": 3, "team_a_difficulty": 3,
             "kickoff_time": None, "finished": False,
             "provisional_start_time": False, "stats": []},
            # Scheduled but provisional: FPL's own UI renders "TBC" and the
            # fixture can still move gameweek, turning a pick into a blank.
            {"id": 401, "event": 30, "team_h": 7, "team_a": 8,
             "team_h_difficulty": 3, "team_a_difficulty": 3,
             "kickoff_time": "2027-03-20T15:00:00Z", "finished": False,
             "provisional_start_time": True, "stats": []},
        ],
    }


def _off_season_bootstrap() -> dict:
    """No events at all.

    The shape that breaks `[e for e in events if e["is_next"]][0]` with an
    IndexError - in July, in production, on the first deploy.
    """
    return {
        "description": "Off-season: no events published. Must exit cleanly, not raise.",
        "elements": [],
        "events": [],
        "teams": [],
        "element_types": [],
        "total_players": 0,
        "game_config": {"settings": {"timezone": "UTC"}, "rules": {}, "scoring": {}},
    }


def _season_end_bootstrap() -> dict:
    """Every deadline in the past. `next_deadline` must return None."""
    past = _base_deadline() - 400 * 24 * HOUR
    return {
        "description": "Season complete: all 38 deadlines are in the past.",
        "elements": [],
        "events": [
            {"id": i, "name": f"Gameweek {i}",
             "deadline_time_epoch": past + i * 7 * 24 * HOUR,
             "deadline_time": "2026-05-01T11:00:00Z",
             "is_current": False, "is_next": False, "is_previous": i == 38,
             "finished": True, "chip_plays": [], "overrides": {"rules": {}}}
            for i in range(1, 39)
        ],
        "teams": [],
        "element_types": [],
        "total_players": 11_000_000,
        "game_config": {"settings": {"timezone": "UTC"}, "rules": {}, "scoring": {}},
    }


def _understat_empty() -> dict:
    """`teams` as an empty ARRAY, not an empty object.

    This is live right now: `/getLeagueData/EPL/2026` returns exactly this.
    Any code doing `payload["teams"].items()` raises AttributeError in pre-season.
    """
    return {"teams": [], "players": [], "dates": []}


def _chip_week_event() -> dict:
    """A heavy wildcard week.

    Wildcards inflate raw transfer counts by roughly 29% while contributing about
    1.4% of genuine transfer pressure. Without the `chip_plays` discount, GW1-2,
    GW20-21 and every post-blank week read as alarming.
    """
    return {
        "description": "GW20 after a blank: 1.2m wildcards played. Contaminates transfer flow.",
        "id": 20,
        "name": "Gameweek 20",
        "deadline_time_epoch": _base_deadline() + 19 * 7 * 24 * HOUR,
        "is_current": True,
        "is_next": False,
        "finished": False,
        "transfers_made": 14_500_000,
        "chip_plays": [
            {"chip_name": "wildcard", "num_played": 1_200_000},
            {"chip_name": "bboost", "num_played": 180_000},
            {"chip_name": "3xc", "num_played": 95_000},
            {"chip_name": "freehit", "num_played": 340_000},
        ],
        "overrides": {"rules": {}},
    }


def _rule_override_event() -> dict:
    """FPL changed the rules for one gameweek.

    Rare but real. We do not model per-gameweek rule variation, so the invariant
    fires and a human reads the override rather than the bot guessing.
    """
    return {
        "description": "A gameweek with a non-empty overrides.rules block.",
        "id": 25,
        "name": "Gameweek 25",
        "deadline_time_epoch": _base_deadline() + 24 * 7 * 24 * HOUR,
        "is_current": False,
        "is_next": True,
        "finished": False,
        "chip_plays": [],
        "overrides": {"rules": {"squad_squadsize": 16}, "scoring": {}},
    }


def _updating_page() -> str:
    """The maintenance page FPL serves **with HTTP 200**.

    This is why `allow_redirects=False` plus a content-type assertion are both
    mandatory. Checking the status code alone does not catch it: you get a 200,
    and then a JSON parser chokes on HTML - or worse, silently produces nothing.
    """
    return (
        "<!DOCTYPE html><html><head><title>Fantasy Premier League</title></head>"
        "<body><h1>The game is being updated.</h1>"
        "<p>The game is currently being updated. Please check back shortly.</p>"
        "</body></html>"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--source",
        action="append",
        choices=["fpl", "clubelo", "understat", "ffs", "premierinjuries"],
        help="Record only these sources (repeatable). Default: all.",
    )
    parser.add_argument(
        "--synthetic-only",
        action="store_true",
        help="Skip live recording; only rebuild the hand-crafted fixtures.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Override the recording directory.",
    )
    args = parser.parse_args()

    print("Building synthetic fixtures (the branches that will not occur before")
    print("they matter - blanks, doubles, postponements, off-season, /updating/)")
    build_synthetic(CURATED_DIR)

    if args.synthetic_only:
        return 0

    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    output_dir = args.output or (RECORDED_DIR / timestamp)
    sources = args.source or ["fpl", "clubelo", "understat", "ffs", "premierinjuries"]

    print(f"\nRecording live responses to {output_dir}")
    print("(this talks to third-party APIs - one request each, honest User-Agent)\n")
    record_live(sources, output_dir)

    print(f"\nDone. Recorded set: {output_dir}")
    print("Commit anything worth replaying into tests/fixtures/ - and remember the")
    print("shape of this data changes materially once the season starts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
