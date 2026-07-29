"""The Odds API - optional, quota-bound.

https://api.the-odds-api.com/v4/  sport key: `soccer_epl`

Free tier is **500 credits per month**, and one credit is charged per region per
market. That is a hard budget and it is easy to blow through in a single careless
run, so the request plan is fixed and explicit:

    1 x /odds?regions=uk&markets=h2h,totals          =  2 credits  (all fixtures)
    1 x /events                                      =  1
   10 x /events/{id}/odds?markets=player_goal_scorer_anytime = 10
                                             total   = 13 credits per run
                                                      ~104 per month at 8 runs

Two rules keep it there:

* **The 1X2 market key is `h2h`, not `h2h_3_way`.** This is worth stating because
  the name is misleading: `h2h` reads like a two-way market, and `h2h_3_way`
  reads like the soccer one. The API disagrees - requesting `h2h_3_way` here
  returns
  `422 {"error_code":"INVALID_MARKET","message":"Markets not supported by this
  endpoint: h2h_3_way"}`, and every odds fetch failed that way. For soccer,
  `h2h` already returns three outcomes: home, away and `"Draw"`.

* **Use exactly one region.** `regions=uk,eu,us` triples the cost for near
  identical prices. There is no third-region edge worth 2x the quota.
* **Pull `totals` and `h2h` from the cheap featured endpoint**, which
  returns all ten matches for 2 credits. Fetching them per event would cost 30.

Player props are the expensive part because they are only available per event -
hence the ten separate calls. That is the whole reason this source is optional.

`x-requests-remaining` is read from every response and we **hard-stop at a
reserve floor**. Running the quota to zero mid-month would silently disable the
source for the remaining gameweeks, which is a worse outcome than skipping props
on one run. The breaker is independent so exhaustion degrades to ClubElo cleanly.

[UNVERIFIED - SPEC section 8 item 8: all authenticated response shapes. Only the
401 was confirmed during research. The parsing below follows the documented v4
schema and is written defensively; expect to adjust it after the first live call,
and note that no `clean_sheet` market key was found - use ClubElo for that.]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aws_lambda_powertools.metrics import MetricUnit

from fplbot.config import Endpoints
from fplbot.domain.devig import devig_1x2, devig_goalscorers
from fplbot.observability import Metric, emit, logger, tracer
from fplbot.sources.base import SourceContext, SourceResult, with_fallback

SOURCE = "oddsapi"

SPORT_KEY = "soccer_epl"
REGION = "uk"  # exactly one - see the module docstring

# Stop using the API entirely below this many credits, keeping a reserve for the
# rest of the month. 13 credits per run times a few remaining gameweeks.
CREDIT_RESERVE_FLOOR = 60

# Cap on per-event player-prop calls in a single run. Belt and braces against a
# double gameweek producing twenty fixtures and quietly doubling the spend.
MAX_PROP_EVENTS = 10


@dataclass
class MatchOdds:
    event_id: str
    home_team: str
    away_team: str
    home_win: float | None = None
    draw: float | None = None
    away_win: float | None = None
    over_2_5: float | None = None
    under_2_5: float | None = None


@dataclass
class OddsData:
    matches: list[MatchOdds] = field(default_factory=list)
    # Player name -> P(scores anytime). Names because the API has no FPL ids;
    # these go through the resolver like any other third-party name.
    goalscorer_probabilities: dict[str, float] = field(default_factory=dict)
    credits_remaining: int | None = None
    credits_used_this_run: int = 0
    props_fetched: bool = False


@tracer.capture_method
def fetch_odds(context: SourceContext, api_key: str | None) -> SourceResult[OddsData]:
    """Fetch match odds and, budget permitting, goalscorer props.

    Returns a SKIPPED-style failure when no key is configured. That is not an
    error: the source is explicitly optional and ClubElo covers the same ground
    for 1X2 and clean sheets. The only thing we genuinely lose is player-level
    goalscorer pricing.
    """
    if not api_key:
        logger.info("No Odds API key configured - skipping (ClubElo covers 1X2 and clean sheets)")
        context.quality.add_caveat(
            "Odds API not configured; match probabilities come from ClubElo's model instead."
        )
        return SourceResult(SOURCE, None, _skipped_state(), "no api key configured")

    def _fetch() -> OddsData:
        data = OddsData()

        # -- Featured endpoint: all fixtures, 2 credits ---------------------
        featured = context.fetch_and_archive(
            SOURCE,
            f"{Endpoints.ODDS_API}/sports/{SPORT_KEY}/odds",
            params={
                "apiKey": api_key,
                "regions": REGION,
                "markets": "h2h,totals",
                "oddsFormat": "decimal",
            },
        )
        data.credits_remaining = _read_quota(featured.headers)
        data.credits_used_this_run += 2
        data.matches = _parse_featured(featured.json())

        # -- Player props, only if the budget allows -----------------------
        if data.credits_remaining is not None and data.credits_remaining < CREDIT_RESERVE_FLOOR:
            logger.warning(
                "Odds API credit reserve reached - skipping player props",
                extra={"remaining": data.credits_remaining, "floor": CREDIT_RESERVE_FLOOR},
            )
            context.quality.add_caveat(
                f"Odds API quota low ({data.credits_remaining} credits); "
                "goalscorer probabilities omitted this run."
            )
            return data

        events = context.fetch_and_archive(
            SOURCE,
            f"{Endpoints.ODDS_API}/sports/{SPORT_KEY}/events",
            params={"apiKey": api_key},
        )
        data.credits_used_this_run += 1
        event_ids = [e["id"] for e in events.json()][:MAX_PROP_EVENTS]

        prices: dict[str, tuple[float, float]] = {}
        for event_id in event_ids:
            try:
                response = context.fetch_and_archive(
                    SOURCE,
                    f"{Endpoints.ODDS_API}/sports/{SPORT_KEY}/events/{event_id}/odds",
                    params={
                        "apiKey": api_key,
                        "regions": REGION,
                        "markets": "player_goal_scorer_anytime",
                        "oddsFormat": "decimal",
                    },
                )
                data.credits_used_this_run += 1
                data.credits_remaining = _read_quota(response.headers) or data.credits_remaining
                prices.update(_parse_goalscorer_prices(response.json()))
            except Exception as exc:
                logger.warning(
                    "Player prop fetch failed for one event",
                    extra={"event_id": event_id, "error": str(exc)[:200]},
                )

        # Devig each player's Yes/No pair INDEPENDENTLY with the power method.
        # Anytime goalscorer is not a partition - see domain/devig.py.
        by_index = dict(enumerate(prices.values()))
        devigged = devig_goalscorers(by_index)
        data.goalscorer_probabilities = {
            name: devigged[index] for index, name in enumerate(prices.keys())
        }
        data.props_fetched = True

        logger.info(
            "Fetched odds",
            extra={
                "matches": len(data.matches),
                "goalscorers": len(data.goalscorer_probabilities),
                "credits_used": data.credits_used_this_run,
                "credits_remaining": data.credits_remaining,
            },
        )
        return data

    return with_fallback(
        context,
        SOURCE,
        _fetch,
        serialise=lambda d: {
            "matches": [vars(m) for m in d.matches],
            "goalscorer_probabilities": d.goalscorer_probabilities,
            "credits_remaining": d.credits_remaining,
        },
        deserialise=lambda d: OddsData(
            matches=[MatchOdds(**m) for m in d.get("matches", [])],
            goalscorer_probabilities=d.get("goalscorer_probabilities", {}),
            credits_remaining=d.get("credits_remaining"),
        ),
    )


def _skipped_state():
    from fplbot.models.domain import SourceState

    return SourceState.SKIPPED


def _read_quota(headers: dict[str, str]) -> int | None:
    """Read and record `x-requests-remaining`.

    Emitted as a CloudWatch metric so the SAM template can alarm before the quota
    runs out, rather than after the source has silently gone dark.
    """
    raw = headers.get("x-requests-remaining")
    if raw is None:
        return None
    try:
        remaining = int(raw)
    except ValueError:
        return None
    emit(Metric.ODDS_CREDITS_REMAINING, remaining, MetricUnit.Count)
    return remaining


def _parse_featured(payload: list[dict[str, Any]]) -> list[MatchOdds]:
    """Parse the featured-odds response.

    Written defensively because the authenticated response shape is UNVERIFIED.
    Every lookup tolerates absence: a missing market costs us one field, not the
    whole run.
    """
    matches: list[MatchOdds] = []
    for event in payload:
        odds = MatchOdds(
            event_id=event.get("id", ""),
            home_team=event.get("home_team", ""),
            away_team=event.get("away_team", ""),
        )
        # Take the first bookmaker offering each market. Averaging across books
        # would be marginally better but costs complexity for a second-order gain,
        # and the devigging matters far more than the book choice.
        for bookmaker in event.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                key = market.get("key")
                outcomes = {o.get("name"): o.get("price") for o in market.get("outcomes", [])}
                if key == "h2h" and odds.home_win is None:
                    odds.home_win = outcomes.get(odds.home_team)
                    odds.away_win = outcomes.get(odds.away_team)
                    odds.draw = outcomes.get("Draw")
                elif key == "totals" and odds.over_2_5 is None:
                    for outcome in market.get("outcomes", []):
                        if outcome.get("point") == 2.5:
                            if outcome.get("name") == "Over":
                                odds.over_2_5 = outcome.get("price")
                            elif outcome.get("name") == "Under":
                                odds.under_2_5 = outcome.get("price")
        matches.append(odds)
    return matches


def _parse_goalscorer_prices(payload: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Extract per-player Yes/No decimal prices.

    Returns player name -> (yes, no). The `no` side is frequently absent; the
    devigger handles that case with a flat margin assumption and the caller
    should reflect the extra uncertainty in the pick's confidence.
    """
    prices: dict[str, tuple[float, float]] = {}
    for bookmaker in payload.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") != "player_goal_scorer_anytime":
                continue
            per_player: dict[str, dict[str, float]] = {}
            for outcome in market.get("outcomes", []):
                name = outcome.get("description") or outcome.get("name")
                side = (outcome.get("name") or "").lower()
                price = outcome.get("price")
                if not name or price is None:
                    continue
                per_player.setdefault(name, {})[side] = float(price)
            for name, sides in per_player.items():
                if name in prices:
                    continue
                yes = sides.get("yes") or next(
                    (v for k, v in sides.items() if k not in {"no"}), None
                )
                if yes:
                    prices[name] = (yes, sides.get("no", 0.0))
    return prices


def match_probabilities(odds: MatchOdds) -> tuple[float, float, float] | None:
    """Devig a match's 1X2 with Shin.

    Shin is documented as approximately unbiased specifically in the EPL, which
    is the only league we care about. See domain/devig.py for why the method
    choice matters more than it looks.
    """
    if not (odds.home_win and odds.draw and odds.away_win):
        return None
    return devig_1x2(odds.home_win, odds.draw, odds.away_win)
