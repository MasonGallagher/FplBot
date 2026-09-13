"""Business invariants - the assertions types cannot make for you.

SPEC section 6.3 puts it well: *silent schema drift is a bigger risk than an
outright outage, because it yields confident, wrong recommendations.* An outage
is obvious. A payload where `selected_by_percent` has quietly become a fraction
rather than a percentage still parses perfectly, still produces a beautifully
formatted email, and inverts every rank-attacking calculation in it.

Pydantic will tell you a field is a string. It cannot tell you that ownership
across all players ought to sum to about 1500 because a squad has 15 slots and
each is filled 100% of the time. That is what lives here.

Design choice: these functions **report** rather than raise. A failed invariant
means "trust this less and tell the user", not "abandon the run". The exception
is the timezone assertion in `deadline.py`, which does raise, because there is no
sensible degraded behaviour when your clock assumptions are wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

from fplbot.models.domain import DataQuality
from fplbot.models.fpl import Bootstrap
from fplbot.observability import logger, report_invariant_violation

# 15 squad slots x 100% = 1500. Measured at 1499.5 on live data, which is a
# beautifully tight canary: it catches a unit change, a truncation, a partial
# payload or a filtered element list, all with one comparison.
EXPECTED_OWNERSHIP_SUM = 1500.0
OWNERSHIP_SUM_TOLERANCE = 50.0

EXPECTED_TEAM_COUNT = 20
EXPECTED_EVENT_COUNT = 38


@dataclass
class InvariantResult:
    name: str
    passed: bool
    detail: str

    def __bool__(self) -> bool:
        return self.passed


def check_bootstrap(
    bootstrap: Bootstrap, quality: DataQuality | None = None
) -> list[InvariantResult]:
    """Run every bootstrap invariant. Returns all results, passed and failed."""
    results = [
        _check_ownership_sum(bootstrap),
        _check_team_count(bootstrap),
        _check_event_count(bootstrap),
        _check_no_rule_overrides(bootstrap),
        _check_elements_present(bootstrap),
        _check_unique_codes(bootstrap),
    ]

    for result in results:
        if not result.passed:
            report_invariant_violation(result.name, result.detail)
            if quality is not None:
                quality.invariant_failures.append(f"{result.name}: {result.detail}")
        else:
            logger.debug("Invariant held", extra={"invariant": result.name})

    return results


def _check_ownership_sum(bootstrap: Bootstrap) -> InvariantResult:
    """Total ownership across all players should be about 1500%.

    A cheap, beautiful canary that the ownership data is coherent - and ownership
    is load-bearing for us, not decoration, because the whole ranking objective
    is expressed in terms of it.
    """
    total = sum(e.ownership for e in bootstrap.elements)
    delta = abs(total - EXPECTED_OWNERSHIP_SUM)
    passed = delta <= OWNERSHIP_SUM_TOLERANCE
    return InvariantResult(
        name="ownership_sums_to_1500",
        passed=passed,
        detail=(
            f"sum(selected_by_percent) = {total:.1f}, expected "
            f"{EXPECTED_OWNERSHIP_SUM} +/- {OWNERSHIP_SUM_TOLERANCE}. "
            "If this is far out, either the payload is partial or the units have changed; "
            "either way every ownership-weighted calculation this run is suspect."
        ),
    )


def _check_team_count(bootstrap: Bootstrap) -> InvariantResult:
    count = len(bootstrap.teams)
    return InvariantResult(
        name="twenty_teams",
        passed=count == EXPECTED_TEAM_COUNT,
        detail=f"len(teams) = {count}, expected {EXPECTED_TEAM_COUNT}",
    )


def _check_event_count(bootstrap: Bootstrap) -> InvariantResult:
    count = len(bootstrap.events)
    return InvariantResult(
        name="thirty_eight_events",
        passed=count == EXPECTED_EVENT_COUNT,
        detail=(
            f"len(events) = {count}, expected {EXPECTED_EVENT_COUNT}. "
            "A short list in July means the new season is not fully published yet."
        ),
    )


def _check_no_rule_overrides(bootstrap: Bootstrap) -> InvariantResult:
    """A non-empty `overrides.rules` means FPL changed the rules for a gameweek.

    Rare but real - it is how one-off scoring or squad-rule tweaks are shipped.
    We do not model per-gameweek rule variation, so if this fires the correct
    response is a human reading the override, not the bot guessing.
    """
    offenders = [
        event.id
        for event in bootstrap.events
        if (event.overrides or {}).get("rules") not in (None, {}, [])
    ]
    return InvariantResult(
        name="no_gameweek_rule_overrides",
        passed=not offenders,
        detail=(
            f"gameweeks with non-empty overrides.rules: {offenders}. "
            "FPL has altered the rules for these gameweeks; the scoring model does not "
            "account for per-gameweek rule variation."
        ),
    )


def _check_elements_present(bootstrap: Bootstrap) -> InvariantResult:
    """Sanity floor on squad size.

    20 clubs times a ~25-man registered squad is around 500-700. Anything under
    300 is a truncated payload, not a quiet transfer window.
    """
    count = len(bootstrap.elements)
    return InvariantResult(
        name="plausible_element_count",
        passed=count >= 300,
        detail=f"len(elements) = {count}; expected roughly 500-700 for 20 clubs",
    )


def _check_unique_codes(bootstrap: Bootstrap) -> InvariantResult:
    """Photo codes must be unique among non-temporary players.

    This is the FFS join key (SPEC 4.1). A duplicate code would silently attach
    one player's predicted lineup slot to another player's stats - a wrong
    recommendation that looks entirely plausible, which is the worst kind.
    """
    codes: dict[int, list[int]] = {}
    for element in bootstrap.elements:
        if element.has_temporary_code:
            continue
        codes.setdefault(element.code, []).append(element.id)
    duplicates = {code: ids for code, ids in codes.items() if len(ids) > 1}
    return InvariantResult(
        name="unique_photo_codes",
        passed=not duplicates,
        detail=(
            f"duplicate photo codes: {duplicates}. The Fantasy Football Scout join "
            "relies on this being unique; fall back to fuzzy matching for these players."
        ),
    )


# ---------------------------------------------------------------------------
# Per-source schema assertions
# ---------------------------------------------------------------------------
# SPEC 6.3 asks for these explicitly, per source. Each is a cheap shape check run
# immediately after parse, so a provider redesign surfaces as a named metric
# rather than as an empty section in the email that nobody notices for a month.

UNDERSTAT_PLAYER_KEYS = {
    "id",
    "player_name",
    "games",
    "time",
    "goals",
    "xG",
    "assists",
    "xA",
    "shots",
    "key_passes",
    "position",
    "team_title",
    "npg",
    "npxG",
    "xGChain",
    "xGBuildup",
    "yellow_cards",
    "red_cards",
}

PREMIER_INJURIES_STATUSES = {"Ruled Out", "25%", "50%", "75%", "100%"}

# Informational only - the check asserts structure, not this number. ClubElo
# served 44 when the spec was written and 45 by late July 2026; both are fine,
# because the parser keys on column NAMES.
CLUBELO_FIXTURE_COLUMN_COUNT = 45


def check_understat_players(players: list[dict], quality: DataQuality | None = None) -> bool:
    """Assert the first player row carries all 18 expected keys."""
    if not players:
        return True  # legitimately empty pre-season; handled by the caller
    missing = UNDERSTAT_PLAYER_KEYS - set(players[0].keys())
    if missing:
        detail = f"Understat player rows are missing expected keys: {sorted(missing)}"
        report_invariant_violation("understat_player_schema", detail)
        if quality:
            quality.invariant_failures.append(detail)
        return False
    return True


def check_clubelo_fixture_columns(columns: list[str], quality: DataQuality | None = None) -> bool:
    """Assert the /Fixtures CSV still has the SHAPE the derivation depends on.

    This used to compare the column count against a constant, which was the wrong
    assertion in both directions.

    **Too sensitive.** `_parse_fixture_row` walks columns by name - anything
    matching `R:h-a` - precisely so a new scoreline is picked up rather than
    silently dropped. An added column therefore cannot break the sum, but the
    count check fired anyway, and told the reader the clean-sheet derivation
    "will be wrong" when it demonstrably was not. A caveat that cries wolf is
    worse than no caveat, because the section it appears in is where the real
    integrity failures are reported.

    **Not sensitive enough.** A count says nothing about identity. Renaming
    `R:1-0` to `R:1:0`, or dropping it while adding an unrelated column, keeps
    the total at 45 and breaks the sum completely - which is the failure this was
    supposed to catch.

    So assert the structure instead:

    * the two columns read by name are present;
    * the scoreline set is non-empty and CONTIGUOUS from zero in both
      directions, since a gap is exactly what a rename or a drop produces;
    * the clean-sheet columns we sum actually exist.

    A trailing gap is expected, not a fault: ClubElo enumerates scorelines up to
    a total of six goals, so `R:7-0` does not exist and roughly 1-8% of
    probability mass sits in higher-scoring outcomes it never lists.
    """
    present = set(columns)
    problems: list[str] = []

    missing_named = [c for c in ("Home", "Away") if c not in present]
    if missing_named:
        problems.append(f"missing named column(s) {missing_named}")

    scorelines = [c for c in columns if c.startswith("R:")]
    if not scorelines:
        problems.append("no R:h-a scoreline columns at all")
    else:
        home_cs = sorted(
            int(c[2:].split("-", 1)[0])
            for c in scorelines
            if c.endswith("-0") and c[2:].split("-", 1)[0].isdigit()
        )
        away_cs = sorted(
            int(c[2:].split("-", 1)[1])
            for c in scorelines
            if c.startswith("R:0-") and c[2:].split("-", 1)[1].isdigit()
        )
        for label, series in (("R:x-0", home_cs), ("R:0-x", away_cs)):
            if not series:
                problems.append(f"no {label} columns, so that side's clean sheet cannot be summed")
            elif series != list(range(len(series))):
                problems.append(
                    f"{label} columns are not contiguous from 0 ({series}) - a gap means a "
                    "column was renamed or dropped, and the clean-sheet sum is now wrong"
                )

    if not problems:
        return True

    detail = (
        f"ClubElo /Fixtures schema changed: {'; '.join(problems)}. "
        f"Clean-sheet derivation sums the R:x-0 columns and is unreliable until this is checked."
    )
    report_invariant_violation("clubelo_fixture_columns", detail)
    if quality:
        quality.invariant_failures.append(detail)
    return False


def check_injury_vocabulary(statuses: set[str], quality: DataQuality | None = None) -> bool:
    """Assert PremierInjuries' `Status` scale has not grown an unmapped value.

    `Status` is a genuinely closed set: every value maps to an exact
    availability percentage in two independent places (`InjuryRecord.
    chance_of_playing`, `domain.minutes._injury_status_ceiling`), so an
    unrecognised one is a real semantic change - it would otherwise fall
    through to a default of "fully fit", which is exactly backwards for a
    genuinely new *negative* status.

    `Condition` used to be checked here too, against a two-value closed set
    (`{"Currently Being Assessed", "Not Available"}`). It never was a closed
    set - it is free descriptive text ("Passed Fit", "Late Fitness Test", a
    specific injury...) with exactly ONE load-bearing value in the whole
    field: "Currently Being Assessed" gates the Phase 2 re-check set
    (`AvailabilitySignal.awaiting_press_conference`), and nothing else
    anywhere branches on `Condition` at all. Validating the entire field
    against that two-item set flagged an ordinary descriptive note as an
    "unexpected value" most weeks. A caveat that cries wolf is worse than no
    caveat - see `check_clubelo_fixture_columns` for the same lesson learned
    the same way - because the section it appears in is exactly where a real
    integrity failure needs to be noticed rather than lost in routine noise.
    So `Condition` is free text and is not validated here.
    """
    unknown_status = statuses - PREMIER_INJURIES_STATUSES
    if not unknown_status:
        return True
    detail = f"unexpected PremierInjuries Status value(s) {sorted(unknown_status)}"
    report_invariant_violation("premierinjuries_vocabulary", detail)
    if quality:
        quality.invariant_failures.append(detail)
    return False
