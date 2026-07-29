"""Schemas for the FPL official API.

Endpoint coverage:

    bootstrap-static/        -> Bootstrap  (elements, events, teams, game_config)
    fixtures/                -> Fixture
    element-summary/{id}/    -> ElementSummary
    event-status/            -> handled inline; the payload is trivial

Everything inherits `DriftTolerantModel`, so an unknown field is recorded and
carried, never fatal.

A running theme in this module: fields that FPL sends as strings are exposed as
properties that return floats, with the string kept intact underneath. That way
we never lose the original value (useful when debugging a coercion) but nothing
downstream ever has to remember which fields are strings.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, computed_field

from fplbot.config import FALLBACK_POSITIONS
from fplbot.models.base import (
    DriftTolerantModel,
    parse_chance_of_playing,
    parse_optional_float,
    parse_timestamp,
)


# ---------------------------------------------------------------------------
# Teams and positions
# ---------------------------------------------------------------------------
class Team(DriftTolerantModel):
    id: int
    code: int | None = None
    name: str
    short_name: str

    # WARNING: as of 2026-07-25 these are **zero for all 20 teams**. Do not build
    # fixture difficulty on them - you will get a model that ranks every fixture
    # identically and looks like it is working. Use `fixtures[].team_h_difficulty`
    # and ClubElo instead. SPEC section 4.0.
    # [UNVERIFIED whether they ever populate this season.]
    strength: int | None = None
    strength_overall_home: int | None = None
    strength_overall_away: int | None = None
    strength_attack_home: int | None = None
    strength_attack_away: int | None = None
    strength_defence_home: int | None = None
    strength_defence_away: int | None = None

    @property
    def strengths_are_populated(self) -> bool:
        """True if the attack/defence splits carry information.

        Checked at runtime rather than assumed, so that if FPL starts populating
        them mid-season we notice and can use them, and if they stay zero we
        never silently divide by them.
        """
        values = [
            self.strength_attack_home,
            self.strength_attack_away,
            self.strength_defence_home,
            self.strength_defence_away,
        ]
        return any(v for v in values)


class ElementType(DriftTolerantModel):
    """A position. `singular_name_short` is GKP/DEF/MID/FWD."""

    id: int
    singular_name_short: str
    singular_name: str | None = None
    squad_select: int | None = None
    squad_min_play: int | None = None
    squad_max_play: int | None = None


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------
class Element(DriftTolerantModel):
    """One player.

    `id` is the FPL element id used everywhere in the API.
    `code` is the *photo* code, and is the join key to Fantasy Football Scout's
    predicted line-ups - see SPEC section 4.1. It is the single most useful field
    in this model, because it turns name matching from the primary path into a
    fallback.
    """

    id: int
    code: int
    element_type: int
    team: int
    team_code: int | None = None

    # -- names -------------------------------------------------------------
    first_name: str = ""
    # The full legal chain: Gabriel -> "dos Santos Magalhaes". Concatenating
    # first + second and fuzzy-matching on it therefore matches badly.
    second_name: str = ""
    web_name: str = ""
    # Populated for exactly the ~65 hard-to-match players. Highest-precision name
    # field FPL exposes; it goes first in any matching candidate list.
    known_name: str | None = None
    # `p154561` - the same digits as `code`, prefixed. A second confirmation of
    # the join rather than an independent identifier.
    opta_code: str | None = None
    # When true, `code` is a placeholder and must not be trusted as a join key.
    has_temporary_code: bool = False

    # -- price and ownership ----------------------------------------------
    now_cost: int = 0  # tenths of a million: 55 == GBP 5.5m
    cost_change_event: int = 0
    cost_change_start: int = 0
    # STRING in the payload. Also the basis of our best data-coherence canary:
    # summed over all players it should be about 1500 (15 squad slots x 100%).
    selected_by_percent: str = "0"
    # UNVERIFIED and the highest-value unknown in the spec. Currently '0' for
    # everyone. If it encodes progress towards the next price change it replaces
    # most price modelling - so we log it hourly from GW1 and correlate against
    # `cost_change_event` transitions. SPEC sections 4.0 and 8.
    price_change_percent: str | None = None

    # -- availability ------------------------------------------------------
    status: str = "a"  # a=available i=injured d=doubtful s=suspended u=unavailable n=on loan
    news: str = ""
    news_added: str | None = None
    # '' for fit players, NOT null. See parse_chance_of_playing.
    chance_of_playing_this_round: Any = None
    chance_of_playing_next_round: Any = None
    # Newer and stricter than `status`: FPL's own transfer machinery refuses the
    # player when this is False. Safer to hard-filter on than to interpret
    # `status`. [UNVERIFIED semantics - SPEC section 8 item 6.]
    can_transact: bool | None = None
    can_select: bool | None = None
    # FPL hands us the actual club statement behind each injury, keyed to the
    # element id. This largely removes the need to scrape club sites.
    scout_news_link: str | None = None

    # -- season aggregates -------------------------------------------------
    # DANGER: in pre-season these hold LAST season's values while `form`,
    # `event_points` and the transfer counters are zero. Anything that consumes
    # them must be gated on `SeasonPhase.has_started`. SPEC section 0.
    minutes: int = 0
    starts: int = 0
    total_points: int = 0
    event_points: int = 0
    bps: int = 0
    goals_scored: int = 0
    assists: int = 0
    clean_sheets: int = 0
    goals_conceded: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    saves: int = 0
    bonus: int = 0

    # -- transfer flow -----------------------------------------------------
    # *_event: CURRENT GAMEWEEK ONLY, reset to 0 at each deadline.
    # bare:    SEASON CUMULATIVE, monotonic.
    # Neither is the price algorithm's internal counter, which resets on each
    # *price change* rather than each deadline and is not exposed at all.
    transfers_in_event: int = 0
    transfers_out_event: int = 0
    transfers_in: int = 0
    transfers_out: int = 0

    # -- expected stats (Opta, via FPL). STRINGS. --------------------------
    expected_goals: str | None = None
    expected_assists: str | None = None
    expected_goal_involvements: str | None = None
    expected_goals_conceded: str | None = None
    # ... whereas the per-90 variants are genuine floats.
    expected_goals_per_90: float | None = None
    expected_assists_per_90: float | None = None
    expected_goal_involvements_per_90: float | None = None
    expected_goals_conceded_per_90: float | None = None

    # -- defensive contribution -------------------------------------------
    # All five are currently ZERO for every player, including players whose other
    # stats carried over from last season. Prior-season DefCon priors are
    # therefore NOT obtainable from this API - they come from the vaastav archive.
    # This matters most in GW1-5, when in-season sample size is about nil.
    clearances_blocks_interceptions: int = 0
    recoveries: int = 0
    tackles: int = 0
    defensive_contribution: int = 0
    defensive_contribution_per_90: float | None = None

    # -- form and rating. STRINGS. ----------------------------------------
    form: str | None = None
    points_per_game: str | None = None
    ict_index: str | None = None
    influence: str | None = None
    creativity: str | None = None
    threat: str | None = None
    # FPL's own expected points. Our benchmark: a model that cannot beat this is
    # not worth shipping. SPEC section 5.5.
    ep_this: str | None = None
    ep_next: str | None = None

    # -- set pieces --------------------------------------------------------
    # The *_order integers are the usable signal. The matching *_text fields are
    # empty strings for every player, so do not reach for them.
    penalties_order: int | None = None
    corners_and_indirect_freekicks_order: int | None = None
    direct_freekicks_order: int | None = None

    # -- derived, so callers never touch the raw strings -------------------

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ownership(self) -> float:
        """Ownership as a percentage, 0-100."""
        return parse_optional_float(self.selected_by_percent) or 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def price(self) -> float:
        """Price in millions."""
        return self.now_cost / 10.0

    @property
    def form_value(self) -> float:
        """Form as a float.

        Note what `form` actually means: points per game over the last **30
        calendar days**, not the last N matches. It therefore decays towards zero
        across an international break for reasons that have nothing to do with
        the player. Use it accordingly. SPEC section 4.0.
        """
        return parse_optional_float(self.form) or 0.0

    @property
    def ep_next_value(self) -> float:
        return parse_optional_float(self.ep_next) or 0.0

    @property
    def xg_per_90(self) -> float | None:
        return self.expected_goals_per_90

    @property
    def xa_per_90(self) -> float | None:
        return self.expected_assists_per_90

    @property
    def availability_pct(self) -> int:
        """Chance of playing, 0-100.

        `chance_of_playing_next_round` is primary because FPL's own UI reads only
        that one; `..._this_round` is null for every player outside a live
        gameweek, which is most of the time we run.
        """
        return parse_chance_of_playing(self.chance_of_playing_next_round)

    @property
    def news_added_at(self) -> Any:
        return parse_timestamp(self.news_added)

    @property
    def is_penalty_taker(self) -> bool:
        return self.penalties_order == 1

    @property
    def is_transactable(self) -> bool:
        """Whether the player may be bought at all.

        `can_transact` is preferred when present because it is FPL's own answer
        to this exact question. We fall back to `status` when it is absent, which
        it will be on any recorded fixture predating the field.
        """
        if self.can_transact is not None:
            return self.can_transact
        return self.status not in {"u", "n"}

    def position(self, element_types: dict[int, ElementType] | None = None) -> str:
        """GKP/DEF/MID/FWD.

        Resolved from bootstrap's `element_types` when available rather than
        hardcoded, so a hypothetical new position does not silently become a KeyError.
        """
        if element_types and self.element_type in element_types:
            return element_types[self.element_type].singular_name_short
        return FALLBACK_POSITIONS.get(self.element_type, "UNK")

    def display_name(self) -> str:
        return self.known_name or self.web_name or f"{self.first_name} {self.second_name}".strip()


# ---------------------------------------------------------------------------
# Events (gameweeks)
# ---------------------------------------------------------------------------
class Event(DriftTolerantModel):
    id: int
    name: str = ""
    # THE field for all deadline arithmetic. An integer. Never parse
    # `deadline_time` for maths - see SPEC section 3.
    deadline_time_epoch: int
    deadline_time: str | None = None
    # In pre-season `is_current` is False for every event and `is_next` may be
    # None. Both callers must handle that; see domain/deadline.py.
    is_current: bool = False
    is_next: bool = False
    is_previous: bool = False
    finished: bool = False
    data_checked: bool = False
    # Counts of each chip played in this gameweek. Wildcards inflate raw transfer
    # counts by roughly 29% while contributing about 1.4% of genuine transfer
    # pressure, so this is how we discount contaminated weeks. SPEC section 5.3.
    chip_plays: list[dict[str, Any]] = Field(default_factory=list)
    transfers_made: int = 0
    most_captained: int | None = None
    most_transferred_in: int | None = None
    # A non-empty `overrides.rules` means FPL changed the rules for this
    # gameweek. We assert it is empty and alarm if not. SPEC section 6.3.
    overrides: dict[str, Any] = Field(default_factory=dict)

    def chip_count(self, chip_name: str) -> int:
        for entry in self.chip_plays:
            if entry.get("chip_name") == chip_name:
                return int(entry.get("num_played", 0))
        return 0

    @property
    def wildcards_played(self) -> int:
        return self.chip_count("wildcard")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class Fixture(DriftTolerantModel):
    id: int
    # `event is None` means the fixture is unscheduled - a postponement, or a
    # match not yet assigned to a gameweek. It is the signal for a blank.
    event: int | None = None
    team_h: int
    team_a: int
    team_h_score: int | None = None
    team_a_score: int | None = None
    kickoff_time: str | None = None
    finished: bool = False
    finished_provisional: bool = False
    started: bool | None = None
    minutes: int = 0
    # True when the kickoff time is a placeholder - FPL's own UI renders "TBC".
    # A fixture with this set can move gameweek, so treat its difficulty and even
    # its existence as provisional.
    provisional_start_time: bool = False
    # Populated and on a clean 1-5 scale, unlike teams[].strength_*. This is the
    # difficulty signal we actually use.
    team_h_difficulty: int = 3
    team_a_difficulty: int = 3
    # Empty in pre-season. [UNVERIFIED shape - capture a real sample at GW1.]
    stats: list[dict[str, Any]] = Field(default_factory=list)
    pulse_id: int | None = None

    def opponent_of(self, team_id: int) -> int | None:
        if team_id == self.team_h:
            return self.team_a
        if team_id == self.team_a:
            return self.team_h
        return None

    def is_home_for(self, team_id: int) -> bool:
        return team_id == self.team_h

    def difficulty_for(self, team_id: int) -> int:
        return self.team_h_difficulty if team_id == self.team_h else self.team_a_difficulty


# ---------------------------------------------------------------------------
# element-summary/{id}/
# ---------------------------------------------------------------------------
class HistoryRow(DriftTolerantModel):
    """One gameweek of a player's season.

    The valuable part: `transfers_in`, `transfers_out`, `transfers_balance`,
    `selected` and `value` are all retrievable retroactively. That substantially
    reduces the cold-start problem - gameweek-granular transfer history does not
    require us to have been running. Only *intra-gameweek* velocity needs our own
    hourly snapshots.

    [UNVERIFIED - SPEC section 8 item 3: whether `transfers_in` here is
    per-gameweek or cumulative-to-that-gameweek. Settle it by diffing two
    consecutive rows; `handlers/backfill.py::_log_transfer_semantics` does exactly
    that and logs the verdict, so the first live backfill resolves it for us.]
    """

    element: int
    fixture: int | None = None
    round: int
    minutes: int = 0
    total_points: int = 0
    was_home: bool = False
    opponent_team: int | None = None
    goals_scored: int = 0
    assists: int = 0
    clean_sheets: int = 0
    bps: int = 0
    bonus: int = 0
    starts: int = 0
    value: int = 0
    selected: int = 0
    transfers_in: int = 0
    transfers_out: int = 0
    transfers_balance: int = 0
    expected_goals: str | None = None
    expected_assists: str | None = None
    defensive_contribution: int = 0


class ElementSummary(DriftTolerantModel):
    history: list[HistoryRow] = Field(default_factory=list)
    history_past: list[dict[str, Any]] = Field(default_factory=list)
    fixtures: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# game_config
# ---------------------------------------------------------------------------
class GameRules(DriftTolerantModel):
    """Squad construction rules.

    Read at runtime rather than hardcoded. This is the cheapest possible defence
    against a mid-season rule change: FPL has form for adjusting these, and a
    hardcoded 15 becomes a subtly wrong optimiser rather than a loud failure.
    SPEC section 4.0.
    """

    squad_squadsize: int = 15
    squad_team_limit: int = 3
    squad_total_spend: int = 1000
    transfers_sell_on_fee: float = 0.5
    max_extra_free_transfers: int = 4  # implies 5 banked maximum
    stats_form_days: int = 30


class ScoringRules(DriftTolerantModel):
    """Points scoring. Also read at runtime.

    `defensive_contribution` is {"DEF": 2, "FWD": 2, "GKP": 0, "MID": 2} - the
    *points*. The **thresholds** that must be hit to earn them are NOT in the API
    (community consensus is DEF 10, MID/FWD 12, UNVERIFIED). They live in
    ModelTunables with that caveat attached.
    """

    # Each of these is per-position ({"GKP": 10, "DEF": 6, ...}) *or* a single
    # flat int once FPL decides a stat no longer varies by position - first
    # observed 2026-07-28, when `assists` switched from a dict to a bare `3`.
    # `bonus` and `saves` were already flat by the time this model was written;
    # nothing says the rest won't follow, so all five take the same shape.
    goals_scored: dict[str, int] | int | None = Field(default_factory=dict)
    assists: dict[str, int] | int | None = Field(default_factory=dict)
    clean_sheets: dict[str, int] | int | None = Field(default_factory=dict)
    goals_conceded: dict[str, int] | int | None = Field(default_factory=dict)
    defensive_contribution: dict[str, int] | int | None = Field(default_factory=dict)
    bonus: dict[str, int] | int | None = None
    saves: dict[str, int] | int | None = None


class GameSettings(DriftTolerantModel):
    # Asserted equal to "UTC" at startup. If FPL ever changes it, every epoch
    # assumption in this codebase needs revisiting, and we want to hear about it
    # loudly rather than infer it from wrong recommendations.
    timezone: str = "UTC"


class GameConfig(DriftTolerantModel):
    rules: GameRules = Field(default_factory=GameRules)
    scoring: ScoringRules = Field(default_factory=ScoringRules)
    settings: GameSettings = Field(default_factory=GameSettings)


# ---------------------------------------------------------------------------
# bootstrap-static/
# ---------------------------------------------------------------------------
class Bootstrap(DriftTolerantModel):
    """The whole world in one payload: ~558 elements, 38 events, 20 teams."""

    elements: list[Element]
    events: list[Event]
    teams: list[Team]
    element_types: list[ElementType] = Field(default_factory=list)
    total_players: int = 0
    game_config: GameConfig = Field(default_factory=GameConfig)

    # -- lookups, built once ----------------------------------------------

    def elements_by_id(self) -> dict[int, Element]:
        return {e.id: e for e in self.elements}

    def elements_by_code(self) -> dict[int, Element]:
        """Photo-code -> Element. The FFS join. See SPEC section 4.1.

        Players with `has_temporary_code` are excluded: their code is a
        placeholder and joining on it would confidently attach one player's
        lineup slot to another's stats.
        """
        return {e.code: e for e in self.elements if not e.has_temporary_code}

    def teams_by_id(self) -> dict[int, Team]:
        return {t.id: t for t in self.teams}

    def element_types_by_id(self) -> dict[int, ElementType]:
        return {t.id: t for t in self.element_types}

    def team_of(self, element: Element) -> Team | None:
        return self.teams_by_id().get(element.team)
