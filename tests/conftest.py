"""Shared test fixtures.

Two principles govern this suite:

1. **Tests never hit live APIs.** SPEC section 7. Everything is either a plain
   Python object or a recorded/synthetic payload replayed through `respx`. The
   CI test stage runs with no AWS credentials at all, which makes the guarantee
   structural rather than a matter of discipline.

2. **The synthetic payloads reproduce the real traps.** A fixture that is
   "clean" tests nothing interesting. So the bootstrap below deliberately
   contains: string-typed floats, an empty-string `chance_of_playing`, a
   `web_name` collision (two Wilsons), an initialised name with no space after
   the period, a player with a temporary code, and ownership that sums to
   roughly 1500.
"""

from __future__ import annotations

import os

import pytest

# Settings are read lazily, but set them before any import that might touch them.
os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("EMAIL_FROM", "test@example.com")
os.environ.setdefault("EMAIL_TO", "test@example.com")
os.environ.setdefault("SEASON", "2026-27")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")
os.environ.setdefault("AWS_REGION", "eu-west-2")
os.environ.setdefault("POWERTOOLS_METRICS_NAMESPACE", "fplBotTest")
os.environ.setdefault("POWERTOOLS_SERVICE_NAME", "fplbot-test")
# X-Ray tracing is meaningless outside Lambda. Powertools still constructs its
# provider at import time (so aws-xray-sdk must be installed - it is in
# layer/requirements.txt because the template sets Tracing: Active), but this
# stops it trying to emit segments to a daemon that is not there.
os.environ.setdefault("POWERTOOLS_TRACE_DISABLED", "1")

from fplbot.models.fpl import Bootstrap

# A Saturday 11:00 deadline, expressed as an epoch. All the deadline tests work
# relative to this so they read as "two days before" rather than as magic numbers.
DEADLINE_EPOCH = 1_786_662_000  # 2026-08-15 11:00:00 UTC (a Saturday)
HOUR = 3600


def _element(
    element_id: int,
    code: int,
    *,
    web_name: str,
    first_name: str = "",
    second_name: str = "",
    team: int = 1,
    element_type: int = 3,
    now_cost: int = 60,
    selected_by_percent: str = "5.0",
    **overrides,
) -> dict:
    """Build one element with realistic FPL typing.

    Note `selected_by_percent` and the expected-stat fields are STRINGS, exactly
    as the live API sends them, and `chance_of_playing_next_round` defaults to
    the empty string rather than to null. Those are the traps; a fixture that
    used proper types would let a broken parser pass.
    """
    base = {
        "id": element_id,
        "code": code,
        "element_type": element_type,
        "team": team,
        "web_name": web_name,
        "first_name": first_name,
        "second_name": second_name,
        "now_cost": now_cost,
        "selected_by_percent": selected_by_percent,
        "status": "a",
        "news": "",
        "news_added": None,
        # '' for a fit player, NOT null. The single easiest thing to get wrong.
        "chance_of_playing_this_round": "",
        "chance_of_playing_next_round": "",
        "minutes": 0,
        "starts": 0,
        "total_points": 0,
        "bps": 0,
        "form": "0.0",
        "points_per_game": "0.0",
        "ep_this": "0.0",
        "ep_next": "3.5",
        "expected_goals": "0.0",
        "expected_assists": "0.0",
        "expected_goals_per_90": 0.0,
        "expected_assists_per_90": 0.0,
        "transfers_in_event": 0,
        "transfers_out_event": 0,
        "transfers_in": 0,
        "transfers_out": 0,
        "can_transact": True,
        "has_temporary_code": False,
        "price_change_percent": "0",
    }
    base.update(overrides)
    return base


@pytest.fixture
def bootstrap_payload() -> dict:
    """A small but trap-faithful bootstrap payload."""
    elements = [
        # A nailed, expensive forward with real underlying numbers.
        _element(
            1,
            100001,
            web_name="Haaland",
            first_name="Erling",
            second_name="Haaland",
            team=1,
            element_type=4,
            now_cost=150,
            selected_by_percent="55.4",
            minutes=2700,
            starts=30,
            bps=800,
            total_points=250,
            expected_goals_per_90=0.95,
            expected_assists_per_90=0.15,
            penalties_order=1,
            form="7.2",
        ),
        # A collision group: two Wilsons at DIFFERENT clubs. Surname alone can
        # never be a key - this is what forces the team constraint.
        _element(
            2,
            100002,
            web_name="Wilson",
            first_name="Callum",
            second_name="Wilson",
            team=2,
            element_type=4,
            selected_by_percent="3.1",
        ),
        _element(
            3,
            100003,
            web_name="Wilson",
            first_name="Harry",
            second_name="Wilson",
            team=3,
            element_type=3,
            selected_by_percent="1.2",
        ),
        # An initialised name with NO space after the period. Tokenises to
        # "jtimber" and matches nothing unless normalisation inserts the space.
        _element(
            4,
            100004,
            web_name="J.Timber",
            first_name="Jurrien",
            second_name="Timber",
            team=1,
            element_type=2,
            selected_by_percent="12.5",
            minutes=1800,
            starts=20,
        ),
        # The full legal-chain surname. Sources write "Gabriel"; FPL stores
        # "dos Santos Magalhaes".
        _element(
            5,
            100005,
            web_name="Gabriel",
            first_name="Gabriel",
            second_name="dos Santos Magalhaes",
            team=1,
            element_type=2,
            selected_by_percent="28.3",
            minutes=2600,
            starts=29,
        ),
        # An injured player, with FPL's own flag set.
        _element(
            6,
            100006,
            web_name="Injured",
            first_name="Ivan",
            second_name="Injured",
            team=2,
            element_type=3,
            selected_by_percent="8.0",
            status="d",
            chance_of_playing_next_round=25,
            news="Knock - 25% chance of playing",
            news_added="2026-08-13T09:14:33.123456Z",
        ),
        # A temporary code. Must be excluded from the FFS photo-code join.
        _element(
            7,
            100007,
            web_name="NewSigning",
            team=3,
            element_type=3,
            has_temporary_code=True,
            selected_by_percent="0.4",
        ),
        # A goalkeeper.
        _element(
            8,
            100008,
            web_name="Keeper",
            first_name="Karl",
            second_name="Keeper",
            team=1,
            element_type=1,
            now_cost=50,
            selected_by_percent="15.0",
            minutes=2700,
            starts=30,
        ),
        # Someone FPL will not let you transact.
        _element(
            9,
            100009,
            web_name="Unavailable",
            team=2,
            element_type=3,
            can_transact=False,
            status="u",
            selected_by_percent="0.1",
        ),
        # A smart-quote name, as Fantasy Football Scout emits it.
        _element(
            10,
            100010,
            web_name="O'Reilly",
            first_name="Nico",
            second_name="O'Reilly",
            team=1,
            element_type=2,
            selected_by_percent="6.2",
        ),
    ]

    # Ownership must sum to roughly 1500 for the invariant to hold. Top up with
    # filler players rather than fudging the check - the check is the point.
    current_total = sum(float(e["selected_by_percent"]) for e in elements)
    filler_count = 40
    per_filler = (1500.0 - current_total) / filler_count
    for index in range(filler_count):
        elements.append(
            _element(
                100 + index,
                200000 + index,
                web_name=f"Filler{index}",
                first_name="Fill",
                second_name=f"Er{index}",
                team=(index % 20) + 1,
                element_type=(index % 4) + 1,
                selected_by_percent=f"{per_filler:.4f}",
            )
        )

    events = []
    for gameweek in range(1, 39):
        events.append(
            {
                "id": gameweek,
                "name": f"Gameweek {gameweek}",
                "deadline_time_epoch": DEADLINE_EPOCH + (gameweek - 1) * 7 * 24 * HOUR,
                "deadline_time": "2026-08-15T11:00:00Z",
                # Pre-season: is_current is False for EVERY event, and is_next is
                # set only on the first. This is the shape that breaks naive code.
                "is_current": False,
                "is_next": gameweek == 1,
                "is_previous": False,
                "finished": False,
                "chip_plays": [],
                "overrides": {
                    "rules": {},
                    "scoring": {},
                    "element_types": [],
                    "pick_multiplier": None,
                },
            }
        )

    teams = [
        {
            "id": 1,
            "code": 3,
            "name": "Arsenal",
            "short_name": "ARS",
            "strength_attack_home": 0,
            "strength_attack_away": 0,
            "strength_defence_home": 0,
            "strength_defence_away": 0,
        },
        {
            "id": 2,
            "code": 4,
            "name": "Newcastle",
            "short_name": "NEW",
            "strength_attack_home": 0,
            "strength_attack_away": 0,
        },
        {"id": 3, "code": 5, "name": "Fulham", "short_name": "FUL"},
    ]
    # Pad to the twenty the invariant expects.
    remaining = [
        "Aston Villa",
        "Bournemouth",
        "Brentford",
        "Brighton",
        "Burnley",
        "Chelsea",
        "Crystal Palace",
        "Everton",
        "Leeds",
        "Liverpool",
        "Man City",
        "Man Utd",
        "Nott'm Forest",
        "Sunderland",
        "Spurs",
        "West Ham",
        "Wolves",
    ]
    for index, name in enumerate(remaining, start=4):
        teams.append({"id": index, "code": index, "name": name, "short_name": name[:3].upper()})

    return {
        "elements": elements,
        "events": events,
        "teams": teams,
        "element_types": [
            {"id": 1, "singular_name_short": "GKP", "singular_name": "Goalkeeper"},
            {"id": 2, "singular_name_short": "DEF", "singular_name": "Defender"},
            {"id": 3, "singular_name_short": "MID", "singular_name": "Midfielder"},
            {"id": 4, "singular_name_short": "FWD", "singular_name": "Forward"},
        ],
        "total_players": 11_000_000,
        "game_config": {
            "rules": {
                "squad_squadsize": 15,
                "squad_team_limit": 3,
                "squad_total_spend": 1000,
                "transfers_sell_on_fee": 0.5,
                "max_extra_free_transfers": 4,
                "stats_form_days": 30,
            },
            "scoring": {
                "goals_scored": {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4},
                "assists": {"GKP": 3, "DEF": 3, "MID": 3, "FWD": 3},
                "clean_sheets": {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0},
                "defensive_contribution": {"DEF": 2, "FWD": 2, "GKP": 0, "MID": 2},
            },
            "settings": {"timezone": "UTC"},
        },
    }


@pytest.fixture
def bootstrap(bootstrap_payload: dict) -> Bootstrap:
    return Bootstrap.model_validate(bootstrap_payload)


@pytest.fixture
def fixtures_payload() -> list[dict]:
    """Fixtures covering the normal, double, blank and postponed cases.

    Gameweek 1: team 1 plays twice (a double), teams 2 and 3 once each against
    them, team 4 has no fixture at all (a blank). Plus one unscheduled fixture
    with `event: null`, which is how a postponement looks.
    """
    return [
        {
            "id": 1,
            "event": 1,
            "team_h": 1,
            "team_a": 2,
            "team_h_difficulty": 2,
            "team_a_difficulty": 4,
            "kickoff_time": "2026-08-15T14:00:00Z",
            "finished": False,
            "provisional_start_time": False,
            "stats": [],
        },
        {
            "id": 2,
            "event": 1,
            "team_h": 3,
            "team_a": 1,
            "team_h_difficulty": 5,
            "team_a_difficulty": 2,
            "kickoff_time": "2026-08-17T19:00:00Z",
            "finished": False,
            "provisional_start_time": True,
            "stats": [],
        },
        {
            "id": 3,
            "event": 1,
            "team_h": 5,
            "team_a": 6,
            "team_h_difficulty": 3,
            "team_a_difficulty": 3,
            "kickoff_time": "2026-08-15T14:00:00Z",
            "finished": False,
            "provisional_start_time": False,
            "stats": [],
        },
        # Postponed / not yet scheduled.
        {
            "id": 4,
            "event": None,
            "team_h": 4,
            "team_a": 7,
            "team_h_difficulty": 3,
            "team_a_difficulty": 3,
            "kickoff_time": None,
            "finished": False,
            "provisional_start_time": False,
            "stats": [],
        },
    ]
