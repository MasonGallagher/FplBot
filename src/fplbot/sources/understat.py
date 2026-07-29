"""Understat - expected goals.

https://understat.com

THE HEADER
----------
**`X-Requested-With: XMLHttpRequest` is mandatory on every call.** Without it
*every* endpoint returns 404. That 404 means "you forgot a header", not "this
resource does not exist", and it must never be retried - retrying cannot add a
header. This is the single fact that makes most published Understat scrapers
fail today.

The other widely-copied recipe - regexing `JSON.parse('\\x7B...')` out of a
`<script>` tag - is dead. Understat has a real JSON API now:

    GET /getLeagueData/{league}/{season}   -> {teams, players, dates}
    GET /getPlayerData/{player_id}         -> per-match log, per-shot data

League slug is `EPL`; season is the **starting year**, so `2025` means 2025/26.

TYPING IS INCONSISTENT *WITHIN THE SAME RESPONSE*
-------------------------------------------------
`players[]` values are all **strings**, including the floats - `"xG": "0.42"`.
But `teams[].history[]` values are genuine numbers. Same payload, different
conventions, so coercion has to be applied per block rather than globally.

THE LIVE PARSING LANDMINE
-------------------------
For a season with no data yet, `teams` is an empty **array** `[]`, not an empty
object `{}`. Right now `/getLeagueData/EPL/2026` returns
`{"teams":[],"players":[],"dates":[]}`. Any code doing `payload["teams"].items()`
throws an AttributeError in pre-season. `_coerce_teams` below handles both shapes.

TERMS OF SERVICE - A DELIBERATE JUDGEMENT CALL
----------------------------------------------
Understat's robots.txt is `Disallow: /`. This is a robots prohibition, not a
technical block, and the repo owner has made this call knowingly rather than by
oversight. The mitigations are real and are implemented here:

  * **One request per gameweek per endpoint.** Not per run - the caller gates on
    a DynamoDB marker.
  * An honest User-Agent with a contact URL.
  * Aggressive S3 caching so we never re-fetch what we already have.
  * Never retry-storm.
  * Never redistribute the data.

And critically: **the fallback must work.** There is precedent for Understat
blocking datacentre IPs (a soccerdata issue from December 2025). FPL's own
`expected_goals` / `expected_assists` / `expected_goals_conceded` are Opta-sourced
and good enough that an Understat outage degrades the model rather than stopping
it. See docs/LEGAL.md.

[UNVERIFIED - SPEC section 8 item 2: post-match update latency, community
estimate 2-6 hours. Measure it in GW1; the two-phase schedule depends on it.]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fplbot.config import Endpoints
from fplbot.domain.invariants import check_understat_players
from fplbot.observability import logger, tracer
from fplbot.sources.base import SourceContext, SourceResult, with_fallback

SOURCE = "understat"

# Without this header every endpoint 404s. It is not optional and it is not a
# nicety - it is how the API distinguishes its own AJAX calls.
REQUIRED_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://understat.com/league/EPL",
}


def _to_float(value: Any) -> float:
    """Coerce Understat's string-encoded numbers."""
    if value in (None, "", "-"):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    return int(_to_float(value))


@dataclass
class UnderstatPlayer:
    """One player's season aggregates.

    `npxG` (non-penalty xG) matters more than `xG` for our purposes: penalties
    are modelled separately from open play, via `penalties_order`, and counting
    them twice would systematically overrate designated takers.
    """

    understat_id: str
    name: str
    team_title: str
    position: str
    games: int
    minutes: int
    goals: int
    xg: float
    npxg: float
    assists: int
    xa: float
    shots: int
    key_passes: int
    xg_chain: float
    xg_buildup: float

    @property
    def npxg_per_90(self) -> float | None:
        """None below any minutes at all - the caller applies shrinkage.

        We deliberately do NOT clamp small samples here. Returning a raw per-90
        from 90 minutes and letting `scoring.py` shrink it towards a positional
        prior keeps the two concerns separate and testable. Doing the shrinkage
        in the source adapter would hide it.
        """
        if self.minutes <= 0:
            return None
        return self.npxg * 90.0 / self.minutes

    @property
    def xa_per_90(self) -> float | None:
        if self.minutes <= 0:
            return None
        return self.xa * 90.0 / self.minutes


@dataclass
class UnderstatData:
    players: list[UnderstatPlayer] = field(default_factory=list)
    # `dates[].forecast` gives a free, already vig-free {w, d, l} triple. A
    # perfectly usable odds fallback when The Odds API quota is exhausted.
    forecasts: list[dict[str, Any]] = field(default_factory=list)
    season: str = ""

    def by_name_and_team(self) -> dict[tuple[str, str], UnderstatPlayer]:
        return {(p.name, p.team_title): p for p in self.players}


@tracer.capture_method
def fetch_league_data(
    context: SourceContext, season_start_year: int
) -> SourceResult[UnderstatData]:
    """Fetch season aggregates for the EPL.

    Args:
        season_start_year: the **starting** year. 2026/27 is `2026`.

    A warm-up GET to the site root is issued first to establish cookies. This is
    what `soccerdata` does, which implies session gating has been seen in the
    wild; it costs us one cheap request.
    """

    def _fetch() -> UnderstatData:
        _warm_up(context)

        url = f"{Endpoints.UNDERSTAT}/getLeagueData/EPL/{season_start_year}"
        # Understat serves this endpoint as `text/javascript;charset=utf-8`. The
        # body is ordinary JSON - only the header is unusual - but the default
        # `application/json` assertion rejected it before the parser ever saw it,
        # so every single fetch failed and the source was permanently degraded.
        #
        # The assertion itself is worth keeping: it is the second half of the
        # redirects-disabled defence, and it exists to catch a maintenance or
        # Cloudflare challenge page served as HTML with a 200. Widening it to the
        # two types Understat actually uses keeps that protection - an HTML
        # challenge page still fails - while accepting the real data.
        result = context.fetch_and_archive(
            SOURCE,
            url,
            headers=REQUIRED_HEADERS,
            expect_content_type=("application/json", "text/javascript"),
        )
        payload = result.json()

        players_raw = payload.get("players") or []
        check_understat_players(players_raw, context.quality)

        players = [
            UnderstatPlayer(
                understat_id=str(row.get("id", "")),
                name=row.get("player_name", ""),
                team_title=row.get("team_title", ""),
                position=row.get("position", ""),
                games=_to_int(row.get("games")),
                minutes=_to_int(row.get("time")),
                goals=_to_int(row.get("goals")),
                xg=_to_float(row.get("xG")),
                npxg=_to_float(row.get("npxG")),
                assists=_to_int(row.get("assists")),
                xa=_to_float(row.get("xA")),
                shots=_to_int(row.get("shots")),
                key_passes=_to_int(row.get("key_passes")),
                xg_chain=_to_float(row.get("xGChain")),
                xg_buildup=_to_float(row.get("xGBuildup")),
            )
            for row in players_raw
        ]

        teams = _coerce_teams(payload.get("teams"))
        data = UnderstatData(
            players=players,
            forecasts=payload.get("dates") or [],
            season=str(season_start_year),
        )

        if not players:
            # Entirely legitimate in pre-season. Say so plainly rather than
            # letting it look like a failure.
            logger.info(
                "Understat returned no players - expected before the season starts",
                extra={"season": season_start_year, "teams": len(teams)},
            )
        else:
            logger.info(
                "Parsed Understat league data",
                extra={
                    "players": len(players),
                    "teams": len(teams),
                    "forecasts": len(data.forecasts),
                },
            )
        return data

    return with_fallback(
        context,
        SOURCE,
        _fetch,
        serialise=lambda d: {
            "players": [vars(p) for p in d.players],
            "forecasts": d.forecasts,
            "season": d.season,
        },
        deserialise=lambda d: UnderstatData(
            players=[UnderstatPlayer(**row) for row in d.get("players", [])],
            forecasts=d.get("forecasts", []),
            season=d.get("season", ""),
        ),
    )


def _warm_up(context: SourceContext) -> None:
    """Establish cookies with a root GET before hitting the JSON API."""
    try:
        context.http.fetch(SOURCE, f"{Endpoints.UNDERSTAT}/", expect_content_type="text/html")
    except Exception as exc:
        logger.debug("Understat warm-up request failed", extra={"error": str(exc)[:200]})


def _coerce_teams(raw: Any) -> dict[str, Any]:
    """Normalise `teams` to a dict regardless of which shape we got.

    THE landmine. For a season with no data, `teams` is an empty **array**, not
    an empty object, so `.items()` on it raises AttributeError. Right now
    `/getLeagueData/EPL/2026` returns exactly that. Handling both shapes costs
    four lines and prevents a pre-season crash that would only be discovered in
    production.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {str(entry.get("id", index)): entry for index, entry in enumerate(raw)}
    return {}


def forecast_probabilities(
    data: UnderstatData,
) -> dict[tuple[str, str], tuple[float, float, float]]:
    """(home, away) -> (win, draw, loss) from `dates[].forecast`.

    These are model probabilities and are **already vig-free**. Do not push them
    through the devigging code - there is no margin to remove, and doing so would
    distort perfectly good numbers.
    """
    out: dict[tuple[str, str], tuple[float, float, float]] = {}
    for entry in data.forecasts:
        forecast = entry.get("forecast")
        home = (entry.get("h") or {}).get("title")
        away = (entry.get("a") or {}).get("title")
        if not forecast or not home or not away:
            continue
        out[(home, away)] = (
            _to_float(forecast.get("w")),
            _to_float(forecast.get("d")),
            _to_float(forecast.get("l")),
        )
    return out
