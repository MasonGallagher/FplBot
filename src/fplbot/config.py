"""Configuration, tunables and constants.

Two kinds of setting live here and it is worth being clear about the difference:

*Infrastructure* settings (table names, bucket names, email addresses) arrive
from environment variables set by the SAM template. They vary per deployment.

*Model* tunables (lambda, mu, shrinkage priors, thresholds) are code. They vary
per experiment, and they live in one frozen dataclass rather than being sprinkled
through the scoring functions - SPEC section 5 is explicit that every coefficient
is a placeholder awaiting fitted values, so they need to be replaceable in one
place. `ModelTunables` is deliberately serialisable so a fitted set can be
persisted alongside the recommendations that came out of it.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any

# ---------------------------------------------------------------------------
# Identity we present to the outside world
# ---------------------------------------------------------------------------
# An honest User-Agent with a contact URL is the single cheapest thing you can do
# to stay welcome on someone else's server. If we ever misbehave, the operator can
# tell us rather than silently blackholing our IP range.
CONTACT_URL = os.environ.get("FPLBOT_CONTACT_URL", "https://github.com/MasonGallagher/fplBot")
USER_AGENT = f"fplBot/1.0 (personal, non-commercial; +{CONTACT_URL})"


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
# All deadline arithmetic is integer seconds against `deadline_time_epoch`.
# Never date arithmetic - that is how DST bugs get in. SPEC section 3.
HOUR = 3600
# Two notifications per deadline: a planning report at T-24h and a confirmation
# once team news has landed. This was 48h/24h/3h; the 48h tier is gone because it
# fired before any press conference and was superseded by both of the others.
#
# The two that remain do different jobs and both earn their send. T-24h is early
# enough to plan a transfer and watch a price change, but can land before the
# press conferences that resolve "Currently Being Assessed". T-3h sits after
# them, and is the one to act on.
#
# Both are offsets from the deadline, never from a day of the week. Deadlines are
# not always Friday or Saturday - midweek rounds put them on a Tuesday or
# Wednesday, and the festive period scatters them further. Anchoring to the
# deadline epoch is what makes the tiers correct for all of those without
# special-casing; a weekend fixture is only ever the worked example below.
NOTIFY_TIERS_SECONDS: tuple[int, ...] = (24 * HOUR, 3 * HOUR)

# The tier we consider "confirmed" rather than "provisional". Team news from
# managers' press conferences has landed by T-3h; at T-24h a meaningful share of
# the injury table is still awaiting one. SPEC section 3, phase 2.
CONFIRMED_TIER_SECONDS = 3 * HOUR

# Refuse to send transfer advice built on data older than this. Past the ceiling
# we email about the *failure* instead. SPEC section 6.2.
HARD_STALENESS_CEILING_SECONDS = 24 * HOUR

# FPL sits behind Fastly with a 300s edge TTL. Polling faster than that returns
# byte-identical responses and manufactures a spurious transfer velocity of zero.
FPL_EDGE_TTL_SECONDS = 300

# Snapshots are the only training set for the price model and the only source of
# intra-gameweek transfer velocity. 400 days keeps a full season plus the summer.
# It costs pennies; a 30-day TTL would quietly destroy the dataset. SPEC section 2.
SNAPSHOT_TTL_DAYS = 400
ALIAS_TTL_DAYS = 400


# ---------------------------------------------------------------------------
# HTTP behaviour
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HttpPolicy:
    """Retry, spacing and breaker policy applied to every outbound request."""

    # Only these are transient. A 403 is a Cloudflare challenge - retrying makes
    # it worse and looks like an attack. Understat's 404 means a missing header,
    # not a transient fault. SPEC section 4.
    retry_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})
    max_attempts: int = 3
    backoff_base_seconds: float = 1.0
    backoff_cap_seconds: float = 60.0

    # Per-host serialisation. We never have more than one request in flight to a
    # given host, and we leave at least this long between them.
    min_host_spacing_seconds: float = 1.5

    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 20.0

    # Circuit breaker: after this many consecutive failures a source is cut off
    # for the rest of the invocation and we fall back to last-known-good.
    breaker_failure_threshold: int = 3
    breaker_reset_seconds: float = 300.0


HTTP_POLICY = HttpPolicy()


# ---------------------------------------------------------------------------
# Model tunables - every number here is a starting prior, not a finding
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelTunables:
    """Coefficients for the scoring and ranking model.

    SPEC section 5 is marked PROVISIONAL and says so loudly: these are informed
    guesses pending a fit against the vaastav historical archive (SPEC 5.5). They
    live together so that swapping in fitted values is a one-object change and so
    that the values used for a given run can be recorded next to its output.
    """

    # --- Ranking objective (SPEC 5.4) --------------------------------------
    # score = xP - lambda*(ownership * xP) + mu*ceiling
    # lambda penalises template players: their points are mostly *defensive*
    # value because the field already owns them. mu rewards ceiling, because
    # under a rank-attacking objective the upside tail is what moves you.
    ownership_penalty_lambda: float = 0.55
    ceiling_bonus_mu: float = 0.25

    monte_carlo_samples: int = 4000  # ~40 ms for 600 players on a 1 GB Lambda
    ceiling_percentile: float = 90.0
    floor_percentile: float = 10.0

    # --- Captaincy ---------------------------------------------------------
    # Captaincy is NOT the same decision as a transfer, and using the transfer
    # objective for it would be wrong in a specific direction.
    #
    # The armband doubles a player's score, which amplifies the mean AND the
    # variance. Two consequences pull in opposite directions:
    #
    #   * The tail matters more. A captain haul is what wins a gameweek outright,
    #     so the ceiling is worth more here than in a transfer decision.
    #   * The floor matters far more. A captain blank is a *double* zero and is
    #     the single most costly outcome available in a gameweek. Nothing in a
    #     transfer decision is as punishing.
    #
    # And ownership is penalised much more gently than for transfers. The
    # template captain is usually the template captain because he is genuinely
    # the best option, and captaincy differentials lose rank far faster than they
    # gain it. lambda is roughly a third of the transfer value.
    captain_ceiling_weight: float = 0.35
    captain_ownership_lambda: float = 0.18

    # Downside is penalised through TWO terms, and both are needed.
    #
    # `captain_downside_weight` penalises the spread between the mean and the
    # P10 floor - how far a bad week falls below expectation. This is the term
    # that does the real work, because it scales with the size of the downside.
    # A shortfall-only penalty is bounded by the target below (at most ~0.9
    # points) while the ceiling bonus is unbounded, so on its own it could never
    # actually punish volatility: a 50/50 of 0 and 12 would out-rank a certain 6
    # at identical mean, which is precisely backwards for the armband.
    #
    # `captain_floor_weight` then penalises the shortfall below an absolute
    # floor target - the distinct "he might return nothing at all" risk, which is
    # worse than a merely wide distribution.
    #
    # Together these keep a genuine premium (high mean, high ceiling, some
    # variance) comfortably ahead of a safe mid-price option, while refusing to
    # rate a coin flip above a certainty.
    captain_downside_weight: float = 0.45
    captain_floor_weight: float = 0.45
    # Below this expected floor a captaincy pick carries real blank risk.
    # Roughly "he at least played and did something".
    captain_floor_target: float = 2.0
    captain_picks: int = 5
    # Ownership below which a captain pick is called out as a differential.
    captain_differential_ownership: float = 15.0

    # --- Season-horizon projection (used by the wildcard optimiser) --------
    # Per-gameweek decay applied when projecting expected points to the end of
    # the season.
    #
    # This is deliberate and it is not merely conservatism. An undiscounted
    # 38-gameweek sum lets the optimiser build a squad around fixtures five
    # months away, and those fixtures will not survive contact with reality:
    # injuries, form, rotation, managerial changes, cup progression and
    # rescheduling all intervene. Weighting the near term more heavily produces a
    # squad that is actually good *now* and merely plausible later, which is the
    # right trade for something you act on this week.
    #
    # 0.985 per gameweek means GW+10 carries ~86% weight and GW+25 ~69%.
    # Set to 1.0 for a true undiscounted season sum.
    horizon_decay_per_gameweek: float = 0.985
    # Cap the projection horizon. Beyond this the numbers are noise dressed as
    # precision. None means "to the end of the season".
    horizon_max_gameweeks: int | None = None

    # --- Wildcard squad optimiser ------------------------------------------
    # FPL squad rules. Read from game_config at runtime where possible; these are
    # the fallbacks and the shape the optimiser is built around.
    wildcard_budget_tenths: int = 1000  # GBP 100.0m, in FPL's tenths-of-a-million
    squad_composition: dict[str, int] = field(
        default_factory=lambda: {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
    )
    max_players_per_club: int = 3

    # A squad is scored on its best valid starting XI, not on all fifteen. This
    # matters enormously: optimising all fifteen equally spends real money on
    # bench players who will almost never score. The bench still has *some*
    # value - injuries, rotation, autosubs - so it is weighted lightly rather
    # than at zero, which stops the optimiser filling the bench with players who
    # literally cannot play.
    bench_weight: float = 0.12
    # Formation limits for a valid starting XI (1 GKP is implied).
    formation_limits: dict[str, tuple[int, int]] = field(
        default_factory=lambda: {"GKP": (1, 1), "DEF": (3, 5), "MID": (2, 5), "FWD": (1, 3)}
    )

    # Local-search effort. The FPL squad problem is a multi-dimensional knapsack
    # and therefore NP-hard, but at this size a pruned greedy seed plus swap
    # search lands on or very near the optimum. More restarts cost milliseconds.
    optimiser_restarts: int = 6
    optimiser_max_passes: int = 40
    # Candidates kept per position after dominance pruning. A player who is more
    # expensive AND worse than another in the same position can never appear in
    # an optimal squad, so discarding them costs nothing and shrinks the search
    # space by roughly an order of magnitude.
    optimiser_candidates_per_position: int = 45

    # --- Attacking shrinkage (SPEC 5.2) ------------------------------------
    # Empirical-Bayes shrinkage expressed in "equivalent minutes": a player with
    # 90 minutes and 1.0 xG is not a 1.0 xG/90 player. Below roughly 450 minutes
    # a raw per-90 is noise, so we pull it towards the positional prior.
    shrinkage_prior_minutes: float = 450.0
    min_minutes_for_raw_per90: float = 450.0

    # Positional priors for xG90 / xA90, used as the shrinkage target and as the
    # cold-start value for players with no history at all.
    prior_xg90: dict[str, float] = field(
        default_factory=lambda: {"GKP": 0.00, "DEF": 0.05, "MID": 0.17, "FWD": 0.35}
    )
    prior_xa90: dict[str, float] = field(
        default_factory=lambda: {"GKP": 0.00, "DEF": 0.06, "MID": 0.15, "FWD": 0.13}
    )

    # How much of last season's per-90 rate to trust before this season starts,
    # expressed as equivalent minutes fed into the same shrinkage formula.
    #
    # Pre-season is the ONLY time this applies, and it matters more than it
    # sounds: `season_has_started` stays false until the GW1 deadline passes, so
    # the GW1 board - the first one that counts - is built entirely from this
    # path. Discarding last season's rates there modelled Haaland at the average
    # forward's 0.35 xG/90 instead of his own 0.78, and the ownership penalty
    # then ranked him below cheap differentials.
    #
    # 300 against a 450-minute prior weight puts at most 40% on last season.
    # Deliberately conservative: player quality persists across seasons, but
    # transfers, age and role changes mean it is evidence rather than fact.
    preseason_equivalent_minutes: float = 300.0

    # --- Set-piece and penalty multipliers ---------------------------------
    # `penalties_order == 1` is worth a large, separate bump: roughly 0.12 xG per
    # match in expectation for a first-choice taker at a side that wins penalties.
    penalty_taker_xg_bonus: float = 0.12
    penalty_second_taker_xg_bonus: float = 0.02
    direct_freekick_xg_bonus: float = 0.03
    corner_taker_xa_bonus: float = 0.04

    # --- Minutes model (SPEC 5.1) ------------------------------------------
    # Mean minutes conditional on each bucket. We model the bucket *distribution*
    # rather than an expected-minutes scalar because collapsing to a mean destroys
    # exactly the variance information the rank-attacking objective needs.
    minutes_by_bucket: dict[str, float] = field(
        default_factory=lambda: {"starter": 84.0, "rotation": 55.0, "cameo": 18.0, "out": 0.0}
    )

    # --- DefCon (SPEC 4.0 / 5.2) -------------------------------------------
    # UNVERIFIED, community-sourced. The API exposes the *points* for defensive
    # contribution but not the *thresholds*. Do not treat these as fact; they are
    # here with this comment precisely so they are easy to find and correct.
    defcon_threshold_by_position: dict[str, int] = field(
        default_factory=lambda: {"DEF": 10, "MID": 12, "FWD": 12, "GKP": 999}
    )
    # Over-dispersion of per-match defensive actions relative to Poisson. Defensive
    # counts cluster (a side under sustained pressure racks up clearances), so a
    # pure Poisson understates the hit rate against a threshold.
    defcon_overdispersion: float = 1.6

    # --- Availability risk (SPEC 5.3) --------------------------------------
    # Net transfers out, normalised by current owners, above which we start to
    # treat the flow as a signal rather than noise.
    transfer_flow_z_warning: float = 2.0
    transfer_flow_z_alarm: float = 3.0
    ewma_alpha: float = 0.3  # baseline decay over hourly snapshots
    min_snapshots_for_zscore: int = 12  # below this, say so rather than pretend

    # Wildcards inflate raw transfer counts by roughly 29% while contributing about
    # 1.4% of genuine transfer pressure. `events[].chip_plays` gives us the counts
    # to discount by. SPEC 5.3 step 3.
    chip_contamination_discount: float = 0.29

    # --- Name resolution (SPEC 4.7) ----------------------------------------
    fuzzy_auto_accept: int = 92
    fuzzy_review_floor: int = 87
    fuzzy_required_margin: int = 6  # a 95 that ties another 95 is a coin flip

    # --- Bonus points (SPEC 5.2) -------------------------------------------
    # BPS is predictable enough to model for the top players and pure noise below.
    bonus_model_top_n: int = 50

    def to_dict(self) -> dict[str, Any]:
        """Serialise for storage alongside the run that used these values."""
        return asdict(self)


TUNABLES = ModelTunables()


# ---------------------------------------------------------------------------
# Infrastructure settings - injected by the SAM template
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    """Environment-derived settings.

    Read once and cached. Fetching these lazily (rather than at import time) keeps
    the module importable in tests without a fully populated environment.
    """

    table_name: str
    bucket_name: str
    email_from: str
    email_to: tuple[str, ...]
    season: str
    environment: str
    aws_region: str
    odds_api_key_parameter: str | None
    dry_run: bool
    log_level: str

    @property
    def is_production(self) -> bool:
        return self.environment == "prod"


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(
            f"Required environment variable {name} is unset. "
            "It should be injected by the SAM template - check template.yaml."
        )
    return value or ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build settings from the environment. Cached for the life of the container."""
    recipients = tuple(addr.strip() for addr in _env("EMAIL_TO", "").split(",") if addr.strip())
    return Settings(
        table_name=_env("TABLE_NAME", required=True),
        bucket_name=_env("BUCKET_NAME", required=True),
        email_from=_env("EMAIL_FROM", required=True),
        email_to=recipients,
        season=_env("SEASON", "2026-27"),
        environment=_env("ENVIRONMENT", "dev"),
        # Lambda injects AWS_REGION itself, so this fallback only applies to local
        # runs - but it should still agree with where the stack actually lives.
        aws_region=_env("AWS_REGION", "eu-west-1"),
        odds_api_key_parameter=os.environ.get("ODDS_API_KEY_PARAMETER") or None,
        # DRY_RUN builds the whole report but does not send it. Invaluable for
        # `sam local invoke` and for the first live run of a season.
        dry_run=_env("DRY_RUN", "false").lower() in {"1", "true", "yes"},
        log_level=_env("LOG_LEVEL", "INFO"),
    )


# ---------------------------------------------------------------------------
# Source endpoints
# ---------------------------------------------------------------------------
class Endpoints:
    """Base URLs, kept in one place so a provider move is a one-line change."""

    # Trailing slashes are required on the FPL API - without them you get a 301
    # to the slashed form, and we run with redirects disabled.
    FPL = "https://fantasy.premierleague.com/api/"

    # Every Understat endpoint returns 404 without X-Requested-With: XMLHttpRequest.
    # That is a missing-header 404, not a missing-resource 404, and it must never
    # be retried. SPEC section 4.2.
    UNDERSTAT = "https://understat.com"

    FFS_TEAM_NEWS = "https://www.fantasyfootballscout.co.uk/team-news"

    PREMIER_INJURIES = "https://www.premierinjuries.com/injury-table.php"

    # HTTP only. https://api.clubelo.com is connection-refused, not merely
    # certificate-invalid - do not "fix" this to https. SPEC section 4.4.
    CLUBELO = "http://api.clubelo.com"

    ODDS_API = "https://api.the-odds-api.com/v4"

    # The default branch is `master`, not `main`.
    VAASTAV_RAW = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"


# ---------------------------------------------------------------------------
# FPL constants that are safe to hardcode
# ---------------------------------------------------------------------------
# Element type id -> position code. This mapping is in bootstrap's `element_types`
# and we read it from there at runtime; this is only the fallback for fixtures
# recorded before that block existed.
FALLBACK_POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# Appearance points, and the minutes threshold for the second one.
POINTS_APPEARANCE_SHORT = 1
POINTS_APPEARANCE_LONG = 2
MINUTES_FOR_FULL_APPEARANCE = 60

# Goal points by position. These *are* in game_config.scoring, and we read them
# from there - see fplbot.models.fpl.ScoringRules. This is the fallback only.
FALLBACK_GOAL_POINTS = {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4}
FALLBACK_CLEAN_SHEET_POINTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
POINTS_ASSIST = 3
