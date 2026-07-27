"""vaastav/Fantasy-Premier-League - the historical archive.

https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data/{season}/...

Note the default branch is **`master`**, not `main`. Confirmed active.

**NEVER ON THE REQUEST PATH.** This module is for backtesting and model training
only, and it is imported by `scripts/` and `tests/`, never by the Lambda
handlers. Fetching a season of CSVs inside a 120-second scheduled invocation
would be slow, pointless and rude to GitHub.

Why it earns a place at all:

* `gws/gwN.csv` has an `element` column - the FPL id - so the join is exact and
  no name matching is needed.
* It contains the **DefCon columns**, which is how we get prior-season defensive
  contribution priors. The FPL API no longer exposes them: all five DefCon fields
  are currently zero for every player, including players whose other stats
  carried over. That matters most in GW1-5, when in-season sample size is
  effectively nil.

TWO SPECIFIC TRAPS
------------------
1. `fixtures.csv` embeds the FPL `stats` blob as a **Python repr string** with
   single quotes, not JSON. `json.loads` fails on it; `ast.literal_eval` is the
   correct tool. (And `literal_eval` is safe - it evaluates literals only, never
   arbitrary code, unlike `eval`.)

2. There is **no `2026-27` directory yet**. Tolerate its absence rather than
   treating a 404 as a fault.

GitHub raw supports `ETag`, so we send `If-None-Match` and take the 304. Being a
good citizen of someone else's free CDN costs one header.
"""

from __future__ import annotations

import ast
import csv
import io
from dataclasses import dataclass, field
from typing import Any

import httpx

from fplbot.config import USER_AGENT, Endpoints
from fplbot.observability import logger


@dataclass
class GameweekRow:
    """One player's gameweek from `gws/gwN.csv`.

    `element` is the FPL element id - an exact join, no fuzzy matching.
    """

    element: int
    name: str
    position: str
    team: str
    round: int
    minutes: int
    total_points: int
    goals_scored: int
    assists: int
    clean_sheets: int
    bps: int
    bonus: int
    value: int
    selected: int
    transfers_in: int
    transfers_out: int
    transfers_balance: int
    was_home: bool
    opponent_team: int
    expected_goals: float
    expected_assists: float
    # The reason this source exists: DefCon priors the FPL API will not give us.
    clearances_blocks_interceptions: int = 0
    recoveries: int = 0
    tackles: int = 0
    defensive_contribution: int = 0
    raw: dict[str, str] = field(default_factory=dict)


class VaastavArchive:
    """Read-only client for the historical archive. Build time only."""

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        # Its own client rather than the shared hardened one: this never runs in
        # Lambda, the politeness constraints are different (GitHub's CDN is not a
        # small server), and a build script blocked on 1.5-second host spacing
        # while pulling 38 gameweek files would take a minute for no reason.
        self._client = client or httpx.Client(
            timeout=30.0,
            follow_redirects=True,  # raw.githubusercontent redirects legitimately
            headers={"User-Agent": USER_AGENT},
        )
        self._etags: dict[str, str] = {}

    def _get(self, path: str) -> str | None:
        """GET a file, returning None on 404 (a season not yet present)."""
        url = f"{Endpoints.VAASTAV_RAW}/{path}"
        headers: dict[str, str] = {}
        if url in self._etags:
            headers["If-None-Match"] = self._etags[url]

        response = self._client.get(url, headers=headers)

        if response.status_code == 304:
            logger.debug("Archive file unchanged", extra={"path": path})
            return None
        if response.status_code == 404:
            logger.info(
                "Archive file not found - expected for a season not yet published",
                extra={"path": path},
            )
            return None
        response.raise_for_status()

        etag = response.headers.get("etag")
        if etag:
            self._etags[url] = etag
        return response.text

    # -- gameweek data -----------------------------------------------------

    def gameweek(self, season: str, gameweek: int) -> list[GameweekRow]:
        """Load one gameweek's player rows for a season, e.g. ("2025-26", 12)."""
        text = self._get(f"{season}/gws/gw{gameweek}.csv")
        if text is None:
            return []
        return [_parse_gameweek_row(row) for row in csv.DictReader(io.StringIO(text))]

    def season_gameweeks(self, season: str, through: int = 38) -> dict[int, list[GameweekRow]]:
        """Load every available gameweek for a season.

        Stops cleanly at whatever is published, so a part-played season returns
        what exists rather than raising.
        """
        out: dict[int, list[GameweekRow]] = {}
        for gameweek in range(1, through + 1):
            rows = self.gameweek(season, gameweek)
            if not rows:
                break
            out[gameweek] = rows
        logger.info("Loaded archive season", extra={"season": season, "gameweeks": len(out)})
        return out

    # -- fixtures ----------------------------------------------------------

    def fixtures(self, season: str) -> list[dict[str, Any]]:
        """Load `fixtures.csv`, decoding the embedded Python-repr `stats` blob.

        This is the `ast.literal_eval` trap. The `stats` column is not JSON - it
        is the string repr of a Python object, single quotes and all. Passing it
        to `json.loads` raises immediately, which at least fails loudly; the
        subtler risk is someone reaching for `eval` to make it work.
        `literal_eval` parses literals only and cannot execute code.
        """
        text = self._get(f"{season}/fixtures.csv")
        if text is None:
            return []

        rows: list[dict[str, Any]] = []
        for row in csv.DictReader(io.StringIO(text)):
            stats_blob = row.get("stats")
            if stats_blob:
                try:
                    row["stats"] = ast.literal_eval(stats_blob)
                except (ValueError, SyntaxError):
                    logger.debug(
                        "Could not decode fixtures.csv stats blob",
                        extra={"fixture": row.get("id")},
                    )
                    row["stats"] = []
            rows.append(row)
        return rows

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> VaastavArchive:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _parse_gameweek_row(row: dict[str, str]) -> GameweekRow:
    def as_int(key: str, default: int = 0) -> int:
        try:
            return int(float(row.get(key) or default))
        except (TypeError, ValueError):
            return default

    def as_float(key: str, default: float = 0.0) -> float:
        try:
            return float(row.get(key) or default)
        except (TypeError, ValueError):
            return default

    return GameweekRow(
        element=as_int("element"),
        name=row.get("name", ""),
        position=row.get("position", ""),
        team=row.get("team", ""),
        round=as_int("round"),
        minutes=as_int("minutes"),
        total_points=as_int("total_points"),
        goals_scored=as_int("goals_scored"),
        assists=as_int("assists"),
        clean_sheets=as_int("clean_sheets"),
        bps=as_int("bps"),
        bonus=as_int("bonus"),
        value=as_int("value"),
        selected=as_int("selected"),
        transfers_in=as_int("transfers_in"),
        transfers_out=as_int("transfers_out"),
        transfers_balance=as_int("transfers_balance"),
        was_home=(row.get("was_home") or "").strip().lower() in {"true", "1"},
        opponent_team=as_int("opponent_team"),
        expected_goals=as_float("expected_goals"),
        expected_assists=as_float("expected_assists"),
        clearances_blocks_interceptions=as_int("clearances_blocks_interceptions"),
        recoveries=as_int("recoveries"),
        tackles=as_int("tackles"),
        defensive_contribution=as_int("defensive_contribution"),
        raw=dict(row),
    )


def defensive_contribution_priors(
    gameweeks: dict[int, list[GameweekRow]],
) -> dict[int, float]:
    """Per-90 defensive contribution by element id, from a completed season.

    This is the whole point of the archive at model-fit time. All five DefCon
    fields in the live FPL API are currently zero for every player, so without
    this there is no prior at all for GW1-5, when in-season sample size is nil
    and DefCon is a genuinely under-exploited scoring route.

    Players with under 450 minutes across the season are omitted rather than
    given a noisy rate: the caller applies the positional prior for them, which
    is the honest answer for a small sample.
    """
    minutes: dict[int, int] = {}
    contributions: dict[int, int] = {}
    for rows in gameweeks.values():
        for row in rows:
            minutes[row.element] = minutes.get(row.element, 0) + row.minutes
            contributions[row.element] = (
                contributions.get(row.element, 0) + row.defensive_contribution
            )
    return {
        element: contributions[element] * 90.0 / played
        for element, played in minutes.items()
        if played >= 450
    }
