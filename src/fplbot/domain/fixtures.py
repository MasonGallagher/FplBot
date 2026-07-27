"""Blank, double and postponed gameweek detection.

Blanks and doubles are, pound for pound, the highest-leverage fixture signal in
FPL and they are trivial to compute - which makes getting them wrong an unforced
error. A player in a blank scores zero no matter how good he is; a player in a
double gets two bites at the same expectation.

Three specific traps, all of which this module handles explicitly:

1. **Counting fixtures, not matches.** A double gameweek is a *team* having two
   or more fixtures. The test is `>= 2`, not `== 2`, because triple gameweeks
   exist (they are rare, they happen after heavy postponement backlogs, and
   `== 2` silently downgrades one to a single).

2. **`len(fixtures) > 10`.** This heuristic appears in a lot of community code
   and it is neither necessary nor sufficient. A gameweek with one blank and one
   double has exactly ten fixtures and is emphatically not normal. Count per
   team, always.

3. **Postponements.** `fixtures[].event is null` means unscheduled. Separately,
   `provisional_start_time: true` means the kickoff time is a placeholder - FPL's
   own UI renders "TBC" - and such a fixture can still move gameweek. We surface
   these rather than pretending they are settled.
"""

from __future__ import annotations

from collections import defaultdict

from fplbot.models.domain import FixtureContext, FixtureKind, TeamGameweek
from fplbot.models.fpl import Fixture
from fplbot.observability import logger


def fixtures_for_gameweek(fixtures: list[Fixture], gameweek: int) -> list[Fixture]:
    """Fixtures assigned to a gameweek.

    Fixtures with `event is None` are excluded because they are unscheduled -
    they belong to no gameweek yet, and including them would invent matches.
    """
    return [f for f in fixtures if f.event == gameweek]


def build_team_gameweeks(
    fixtures: list[Fixture],
    gameweek: int,
    team_ids: list[int],
) -> dict[int, TeamGameweek]:
    """Classify every team's fixture load for a gameweek.

    Every team gets an entry, including those with none - a team absent from the
    result would be indistinguishable from a lookup miss at the call site, and
    "this team has no fixture" is the single most important thing we can know
    about it.
    """
    scheduled = fixtures_for_gameweek(fixtures, gameweek)
    per_team: dict[int, list[FixtureContext]] = defaultdict(list)

    for fixture in scheduled:
        for team_id, is_home in ((fixture.team_h, True), (fixture.team_a, False)):
            opponent = fixture.opponent_of(team_id)
            if opponent is None:
                continue
            per_team[team_id].append(
                FixtureContext(
                    fixture_id=fixture.id,
                    team_id=team_id,
                    opponent_id=opponent,
                    is_home=is_home,
                    difficulty=fixture.difficulty_for(team_id),
                    provisional=fixture.provisional_start_time,
                )
            )

    result = {
        team_id: TeamGameweek(
            team_id=team_id,
            gameweek=gameweek,
            fixtures=tuple(per_team.get(team_id, [])),
        )
        for team_id in team_ids
    }

    blanks = [tid for tid, tgw in result.items() if tgw.kind is FixtureKind.BLANK]
    doubles = [tid for tid, tgw in result.items() if tgw.kind is FixtureKind.DOUBLE]
    provisional = [tid for tid, tgw in result.items() if tgw.kind is FixtureKind.PROVISIONAL]

    if blanks or doubles or provisional:
        logger.info(
            "Irregular gameweek detected",
            extra={
                "gameweek": gameweek,
                "blank_teams": blanks,
                "double_teams": doubles,
                "provisional_teams": provisional,
                "scheduled_fixtures": len(scheduled),
            },
        )

    return result


def postponed_fixtures(fixtures: list[Fixture]) -> list[Fixture]:
    """Fixtures with no gameweek assigned - postponed or not yet scheduled."""
    return [f for f in fixtures if f.event is None and not f.finished]


def provisional_fixtures(fixtures: list[Fixture], gameweek: int) -> list[Fixture]:
    """Fixtures in this gameweek whose kickoff time is a placeholder.

    Worth surfacing in the report's caveats: a provisional fixture can move to
    another gameweek entirely, which turns a recommendation into a blank.
    """
    return [f for f in fixtures_for_gameweek(fixtures, gameweek) if f.provisional_start_time]


def describe_gameweek(team_gameweeks: dict[int, TeamGameweek]) -> str:
    """One-line human summary for the email header."""
    blanks = sum(1 for t in team_gameweeks.values() if t.kind is FixtureKind.BLANK)
    doubles = sum(1 for t in team_gameweeks.values() if t.kind is FixtureKind.DOUBLE)
    if not blanks and not doubles:
        return "Standard gameweek - all 20 teams play once."
    parts = []
    if doubles:
        parts.append(f"{doubles} team(s) with a double")
    if blanks:
        parts.append(f"{blanks} team(s) blanking")
    return "Irregular gameweek: " + ", ".join(parts) + "."


def fixture_difficulty_multiplier(difficulty: int, is_home: bool) -> float:
    """Convert FPL's 1-5 difficulty into an attacking-output multiplier.

    Calibration note: these are a starting prior, not a finding. FDR is a coarse,
    partly subjective scale, and we lean on it only because `teams[].strength_*`
    is currently all zeros and therefore useless. Where ClubElo gives us a real
    scoreline distribution we prefer that and this function is bypassed
    entirely - see `scoring.py`.

    Home advantage in the Premier League is worth roughly 0.2-0.3 goals per game
    historically, which is about a 10% swing on a typical team total.
    """
    # Difficulty 1 = easiest opponent = highest expected output.
    by_difficulty = {1: 1.30, 2: 1.15, 3: 1.00, 4: 0.87, 5: 0.75}
    multiplier = by_difficulty.get(difficulty, 1.0)
    return multiplier * (1.06 if is_home else 0.94)
