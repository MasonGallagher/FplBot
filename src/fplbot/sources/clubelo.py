"""ClubElo - team strength and exact scoreline probabilities.

http://api.clubelo.com

**HTTP only.** `https://api.clubelo.com` is connection-refused, not merely
certificate-invalid. Do not "fix" this to HTTPS - it will simply stop working.
SPEC section 4.4.

No key, no rate limit, CSV responses. Pound for pound the best value-per-effort
source in the stack, and the reason is `/Fixtures`: it returns a full **scoreline
probability distribution** per match, `R:0-0` through `R:6-0`, which lets us
derive clean-sheet probability exactly rather than assuming a Poisson.

    Home clean sheet = sum of R:x-0   (0-0, 1-0, 2-0, ... 6-0)
    Away clean sheet = sum of R:0-y   (0-0, 0-1, 0-2, ... 0-6)

That is *precisely* the FPL clean-sheet input, for free, from a model rather than
from a bookmaker's shaded prices. And because these are model probabilities, they
are **already vig-free** - there is no margin to remove, and applying a devigging
step to them would be actively wrong.

SECURITY NOTE
-------------
Because this is plain HTTP, an on-path attacker could alter the response. The
mitigation is cheap and worth having: sanity-bound Elo ratings to roughly
1000-2300 and reject rows outside it. A tampered Elo of 9999 would otherwise
propagate into every fixture difficulty calculation in the run. This will not
stop a determined adversary, but it turns "silently wrong recommendations" into
"loud, logged rejection", which is the difference that matters.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime

from fplbot.config import Endpoints
from fplbot.domain.invariants import check_clubelo_fixture_columns
from fplbot.observability import logger, tracer
from fplbot.sources.base import SourceContext, SourceResult, with_fallback

SOURCE = "clubelo"

# Plausible bounds for a top-flight European club. Bayern's peak is around 2100;
# a relegation-bound side sits near 1450. Anything outside 1000-2300 is either a
# lower-league club that slipped through the filter or a tampered response.
ELO_MIN = 1000.0
ELO_MAX = 2300.0


@dataclass(frozen=True)
class EloRating:
    club: str
    elo: float
    rank: int | None
    country: str
    level: int


@dataclass(frozen=True)
class MatchProbabilities:
    """One fixture's probability distribution from ClubElo.

    All fields are genuine model probabilities. No devigging required or wanted.
    """

    home_club: str
    away_club: str
    kickoff: date | None
    home_win: float
    draw: float
    away_win: float
    home_clean_sheet: float
    away_clean_sheet: float
    expected_home_goals: float
    expected_away_goals: float

    def clean_sheet_for(self, *, is_home: bool) -> float:
        return self.home_clean_sheet if is_home else self.away_clean_sheet

    def expected_goals_for(self, *, is_home: bool) -> float:
        return self.expected_home_goals if is_home else self.expected_away_goals

    def expected_conceded_for(self, *, is_home: bool) -> float:
        return self.expected_away_goals if is_home else self.expected_home_goals


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------
@tracer.capture_method
def fetch_ratings(
    context: SourceContext, on: date | None = None
) -> SourceResult[dict[str, EloRating]]:
    """Current Elo ratings, filtered to the twenty Premier League clubs.

    The endpoint returns every club in the world, so the filter
    `Country == "ENG" AND Level == "1"` is what isolates the top flight. It
    should yield exactly twenty rows; anything else is worth a raised eyebrow in
    the logs.
    """
    day = on or datetime.now(UTC).date()
    url = f"{Endpoints.CLUBELO}/{day:%Y-%m-%d}"

    def _fetch() -> dict[str, EloRating]:
        result = context.fetch_and_archive(
            SOURCE,
            url,
            # ClubElo is inconsistent about which CSV content type it serves.
            expect_content_type=("text/csv", "text/plain", "application/csv"),
            ext="csv",
        )
        rows = list(csv.DictReader(io.StringIO(result.text())))
        ratings: dict[str, EloRating] = {}
        rejected = 0

        for row in rows:
            if row.get("Country") != "ENG" or row.get("Level") != "1":
                continue
            try:
                elo = float(row["Elo"])
            except (KeyError, TypeError, ValueError):
                rejected += 1
                continue

            # The MITM sanity bound described in the module docstring.
            if not ELO_MIN <= elo <= ELO_MAX:
                rejected += 1
                logger.warning(
                    "Rejected out-of-range Elo rating",
                    extra={"club": row.get("Club"), "elo": elo, "bounds": [ELO_MIN, ELO_MAX]},
                )
                continue

            club = row["Club"]
            ratings[club] = EloRating(
                club=club,
                elo=elo,
                rank=int(row["Rank"]) if row.get("Rank", "").strip().isdigit() else None,
                country=row["Country"],
                level=int(row["Level"]),
            )

        if len(ratings) != 20:
            logger.warning(
                "Unexpected Premier League club count from ClubElo",
                extra={"count": len(ratings), "rejected": rejected},
            )
        return ratings

    return with_fallback(
        context,
        SOURCE,
        _fetch,
        serialise=lambda r: {"ratings": {k: vars(v) for k, v in r.items()}},
        deserialise=lambda d: {k: EloRating(**v) for k, v in d["ratings"].items()},
    )


# ---------------------------------------------------------------------------
# Fixtures with scoreline probabilities
# ---------------------------------------------------------------------------
@tracer.capture_method
def fetch_fixture_probabilities(
    context: SourceContext,
) -> SourceResult[list[MatchProbabilities]]:
    """`/Fixtures` - 44 columns of goal-difference and scoreline probabilities.

    The response is global, so we filter to `Country == "ENG"`. The column count
    is asserted, because our clean-sheet derivation *sums* the scoreline columns
    and a schema change would make that sum quietly wrong rather than absent.
    """
    url = f"{Endpoints.CLUBELO}/Fixtures"

    def _fetch() -> list[MatchProbabilities]:
        result = context.fetch_and_archive(
            SOURCE,
            url,
            expect_content_type=("text/csv", "text/plain", "application/csv"),
            ext="csv",
        )
        reader = csv.DictReader(io.StringIO(result.text()))
        columns = reader.fieldnames or []
        if not check_clubelo_fixture_columns(columns, context.quality):
            # The 200 came back, but the shape our derivation depends on is not
            # there - Home/Away missing, no R: columns, or a gap that makes the
            # clean-sheet sum wrong. Every row would silently parse to nothing
            # useful (or, in the gap case, to a wrong number that *looks*
            # useful), and returning that quietly as "0 fixtures" or "here are
            # some numbers" would tell `with_fallback` this fetch succeeded -
            # which skips the last-known-good fallback entirely and forces the
            # odds-derived clean sheets, a source this module's own docstring
            # rates as strictly worse. Raise instead, so the exception path
            # degrades to yesterday's ClubElo data - almost certainly still
            # correct, since scoreline distributions do not move fixture to
            # fixture - and says so honestly rather than reporting OK on zero
            # usable rows. `check_clubelo_fixture_columns` has already logged
            # and recorded the specific diagnostic above.
            raise ValueError("ClubElo /Fixtures failed the column-shape check")

        matches: list[MatchProbabilities] = []
        for row in reader:
            if row.get("Country") not in (None, "", "ENG"):
                continue
            parsed = _parse_fixture_row(row)
            if parsed is not None:
                matches.append(parsed)

        logger.info("Parsed ClubElo fixtures", extra={"count": len(matches)})
        return matches

    return with_fallback(
        context,
        f"{SOURCE}_fixtures",
        _fetch,
        serialise=lambda ms: {
            "matches": [
                {**vars(m), "kickoff": m.kickoff.isoformat() if m.kickoff else None} for m in ms
            ]
        },
        deserialise=lambda d: [
            MatchProbabilities(
                **{
                    **m,
                    "kickoff": date.fromisoformat(m["kickoff"]) if m.get("kickoff") else None,
                }
            )
            for m in d["matches"]
        ],
    )


def _parse_fixture_row(row: dict[str, str]) -> MatchProbabilities | None:
    """Turn one CSV row into probabilities.

    The scoreline columns are named `R:h-a`. We walk every column matching that
    shape rather than enumerating them, so a future 7-0 column is picked up
    automatically instead of being silently dropped from the sums.
    """
    home = row.get("Home") or row.get("Club") or ""
    away = row.get("Away") or ""
    if not home or not away:
        return None

    home_cs = 0.0  # away scores 0
    away_cs = 0.0  # home scores 0
    home_win = draw = away_win = 0.0
    expected_home = expected_away = 0.0

    for column, raw in row.items():
        if not column or not column.startswith("R:"):
            continue
        try:
            probability = float(raw)
        except (TypeError, ValueError):
            continue
        if probability <= 0:
            continue

        try:
            home_goals_str, away_goals_str = column[2:].split("-", 1)
            home_goals, away_goals = int(home_goals_str), int(away_goals_str)
        except ValueError:
            continue

        # Clean sheets. Home keeps one when the AWAY side scores zero.
        if away_goals == 0:
            home_cs += probability
        if home_goals == 0:
            away_cs += probability

        # 1X2 by summing scorelines by sign of the goal difference.
        if home_goals > away_goals:
            home_win += probability
        elif home_goals == away_goals:
            draw += probability
        else:
            away_win += probability

        expected_home += home_goals * probability
        expected_away += away_goals * probability

    if home_win + draw + away_win <= 0:
        return None

    kickoff = None
    for key in ("Date", "date", "From"):
        value = row.get(key)
        if value:
            try:
                kickoff = date.fromisoformat(value)
                break
            except ValueError:
                continue

    return MatchProbabilities(
        home_club=home,
        away_club=away,
        kickoff=kickoff,
        home_win=home_win,
        draw=draw,
        away_win=away_win,
        home_clean_sheet=min(1.0, home_cs),
        away_clean_sheet=min(1.0, away_cs),
        expected_home_goals=expected_home,
        expected_away_goals=expected_away,
    )


def index_by_teams(matches: list[MatchProbabilities]) -> dict[tuple[str, str], MatchProbabilities]:
    """(home_club, away_club) -> probabilities, for joining onto FPL fixtures.

    Club names here are ClubElo's own spellings ("Man City", "Tottenham"), which
    is why `domain.teams` exists. They go through the hardcoded alias map, never
    a fuzzy match - twenty names is not a problem worth a fuzzy matcher, and the
    failure mode of confusing the two Manchester clubs is severe.
    """
    return {(m.home_club, m.away_club): m for m in matches}
