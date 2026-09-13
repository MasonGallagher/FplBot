"""Orchestration: ingest -> score -> rank -> report -> send.

This is the only module that knows the whole story. Everything it calls is
either a source adapter (I/O, in `sources/`) or pure logic (in `domain/`), and
keeping that split means this file reads as a narrative rather than as an
algorithm.

THE SHAPE OF A RUN
------------------
    1. Fetch bootstrap. Assert timezone and business invariants.
    2. Snapshot - ALWAYS, even when we will not notify. The snapshot series is
       the training set and the only source of intra-gameweek velocity, so a run
       that skips it is a run whose data is gone forever.
    3. Find the next deadline. No deadline (off-season, season over) means
       snapshot and exit 0 - that is a normal outcome, not an error.
    4. Decide whether a notification tier is due. If not, exit after snapshotting.
    5. Take the idempotency lock BEFORE doing the expensive work.
    6. Ingest third-party sources, each behind its own breaker and fallback.
    7. Score every player, build the board, render, send.

FAILURE POLICY
--------------
Always produce output (SPEC 6.2). Per-source circuit breakers degrade individual
features rather than the whole run. The only refusal is above the hard staleness
ceiling, and then we email about the *failure* rather than about transfers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import numpy as np

from fplbot.config import HARD_STALENESS_CEILING_SECONDS, TUNABLES, get_settings
from fplbot.domain import availability as availability_domain
from fplbot.domain import (
    calibration,
    captaincy,
    devig,
    horizon,
    invariants,
    ranking,
    scoring,
    squad,
    teams,
)
from fplbot.domain import deadline as deadline_domain
from fplbot.domain import fixtures as fixtures_domain
from fplbot.domain.identity import PlayerResolver
from fplbot.domain.minutes import MinutesDistribution, estimate_minutes
from fplbot.http import HttpClient
from fplbot.models.domain import (
    AvailabilitySignal,
    DataQuality,
    FixtureContext,
    PlayerScore,
    RunContext,
    TeamGameweek,
)
from fplbot.models.fpl import Bootstrap
from fplbot.observability import Metric, count, logger, tracer
from fplbot.report import email as email_module
from fplbot.report import render
from fplbot.sources import clubelo, ffs, fpl, oddsapi, premierinjuries, understat
from fplbot.sources.base import SourceContext
from fplbot.storage import DynamoStore, LockAlreadyHeld, RawArchive


@dataclass
class RunOutcome:
    """What a run did, for the handler's response and for the logs."""

    status: str  # snapshot_only | sent | suppressed | failed | no_deadline
    gameweek: int | None = None
    tier: str | None = None
    detail: str = ""
    recommendations: int = 0

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "gameweek": self.gameweek,
            "tier": self.tier,
            "detail": self.detail,
            "recommendations": self.recommendations,
        }


@tracer.capture_method
def run(*, now_epoch: int | None = None, force_tier: str | None = None) -> RunOutcome:
    """Execute one poll.

    Args:
        now_epoch: override the clock. Used by tests and by replaying a recorded
            fixture set at the moment it was captured.
        force_tier: send a given tier regardless of the time remaining. For
            manual invocation and for testing the email end to end; it still
            respects the idempotency lock, so it cannot be used to spam.
    """
    settings = get_settings()
    now = now_epoch or int(time.time())
    quality = DataQuality()

    with HttpClient() as http:
        context = SourceContext(
            http=http,
            archive=RawArchive(),
            store=DynamoStore(),
            quality=quality,
        )

        # -- 1. Bootstrap. Required; everything else is optional. ----------
        bootstrap_result = fpl.fetch_bootstrap(context)
        bootstrap = bootstrap_result.data
        if bootstrap is None:
            return RunOutcome("failed", detail="bootstrap unavailable and no cached copy")

        deadline_domain.assert_timezone_is_utc(bootstrap)
        invariants.check_bootstrap(bootstrap, quality)

        unknown_teams = teams.assert_known_teams([t.name for t in bootstrap.teams])
        if unknown_teams:
            quality.add_caveat(
                f"Unrecognised team name(s) {unknown_teams} - promoted clubs have arrived "
                "and the alias map in domain/teams.py needs updating. Third-party data for "
                "these clubs will not join."
            )

        # -- 2. Snapshot. Always. ------------------------------------------
        # Even on a run that will notify nothing. This series is the only record
        # of intra-gameweek transfer velocity and the only training set for the
        # price model; an hour not snapshotted is an hour lost permanently.
        snapshot = fpl.build_snapshot(bootstrap)
        context.store.put_snapshot(snapshot)

        # -- 3. Deadline ----------------------------------------------------
        info = deadline_domain.next_deadline(bootstrap, now)
        if info is None:
            # Off-season or season complete. A clean, expected exit.
            logger.info("No upcoming deadline - snapshot stored, nothing to notify")
            return RunOutcome("no_deadline", detail="off-season or season complete")

        season_started = deadline_domain.season_has_started(bootstrap, now)

        logger.info(
            "Deadline located",
            extra={
                "gameweek": info.gameweek,
                "hours_remaining": round(info.hours_remaining, 2),
                "season_has_started": season_started,
            },
        )

        # -- 4. Is a tier due? ----------------------------------------------
        tier = force_tier or deadline_domain.due_tier(info.seconds_remaining)
        if tier is None:
            return RunOutcome(
                "snapshot_only",
                gameweek=info.gameweek,
                detail=f"{info.hours_remaining:.1f}h remaining; no tier crossed",
            )

        # -- 5. Idempotency lock, taken BEFORE the expensive work ------------
        # A conditional write on (season, gameweek, tier). Keyed on gameweek and
        # tier, never on wall-clock time, so Scheduler retries and manual
        # re-invocations are all safe.
        try:
            context.store.acquire_notification_lock(info.gameweek, tier)
        except LockAlreadyHeld:
            logger.info(
                "Notification already sent for this tier - suppressing",
                extra={"gameweek": info.gameweek, "tier": tier},
            )
            count(Metric.NOTIFICATION_SUPPRESSED)
            return RunOutcome("suppressed", gameweek=info.gameweek, tier=tier)

        run_context = RunContext(
            now_epoch=now,
            season=settings.season,
            gameweek=info.gameweek,
            deadline_epoch=info.deadline_epoch,
            seconds_to_deadline=info.seconds_remaining,
            tier=tier,
            is_confirmed_phase=deadline_domain.is_confirmed_phase(tier),
            season_has_started=season_started,
            data_quality=quality,
            tunables=TUNABLES.to_dict(),
        )

        try:
            outcome = _produce_and_send(context, bootstrap, run_context)
        except Exception as exc:
            # The lock must go back if we failed before sending, otherwise the
            # retry is suppressed and the tier is silently lost.
            context.store.release_notification_lock(info.gameweek, tier)
            logger.exception("Run failed after acquiring the lock")
            raise exc

        return outcome


# ---------------------------------------------------------------------------
# The expensive half
# ---------------------------------------------------------------------------
def _produce_and_send(
    context: SourceContext, bootstrap: Bootstrap, run_context: RunContext
) -> RunOutcome:
    settings = get_settings()

    # -- 6. Ingest -------------------------------------------------------
    fixtures_result = fpl.fetch_fixtures(context)
    all_fixtures = fixtures_result.data or []

    team_gameweeks = fixtures_domain.build_team_gameweeks(
        all_fixtures, run_context.gameweek, [t.id for t in bootstrap.teams]
    )
    run_context.data_quality.add_caveat(fixtures_domain.describe_gameweek(team_gameweeks))

    provisional = fixtures_domain.provisional_fixtures(all_fixtures, run_context.gameweek)
    if provisional:
        run_context.data_quality.add_caveat(
            f"{len(provisional)} fixture(s) in this gameweek have a provisional (TBC) "
            "kickoff time and could still move gameweek."
        )

    # ClubElo first: cheapest, no key, and it carries the exact clean-sheet
    # probabilities that everything defensive depends on.
    elo_fixtures = clubelo.fetch_fixture_probabilities(context)
    _apply_clubelo(team_gameweeks, elo_fixtures.data or [], bootstrap)

    # Predicted line-ups: the exact-integer join, and the freshest minutes signal.
    lineups = ffs.fetch_lineups(
        context, {e.code: e.id for e in bootstrap.elements if not e.has_temporary_code}
    )

    injuries = premierinjuries.fetch_injuries(context)

    # Understat: gated to once per gameweek by the caller of this pipeline, in
    # keeping with the ToS position taken in docs/LEGAL.md.
    season_start_year = int(settings.season.split("-")[0])
    understat_result = understat.fetch_league_data(context, season_start_year)

    # FPL zeroes every per-90 rate and minutes counter at a season rollover, and
    # Understat's new-season league data starts empty too, so for the opening
    # gameweeks there is NOTHING player-specific in either. Last season's
    # Understat data is the only evidence that survives the turn of the year, and
    # without it every forward scores identically and the board sorts itself on
    # ownership alone. Fetched only while this season is still thin, so it costs
    # one extra request early on and nothing at all thereafter.
    last_season_result = None
    current_players = (understat_result.data.players if understat_result.data else []) or []
    if len(current_players) < MIN_UNDERSTAT_PLAYERS_FOR_SELF_SUFFICIENCY:
        last_season_result = understat.fetch_league_data(context, season_start_year - 1)

    odds_result = oddsapi.fetch_odds(context, _odds_api_key(settings))

    # Second choice for anything ClubElo did not cover. Runs after the odds fetch
    # because that is where the data comes from; it only fills gaps, so a healthy
    # ClubElo makes it a no-op.
    _apply_odds_clean_sheets(team_gameweeks, odds_result.data, bootstrap, run_context)

    # -- 7. Staleness gate ------------------------------------------------
    # Only weighs data we are actually advising FROM. A source dropped for being
    # too old no longer reports an age, so it cannot drag the run over the gate -
    # which is what withheld a GW1 board built from six healthy sources because
    # ClubElo had been timing out, unnoticed, for three weeks.
    worst_age = run_context.data_quality.worst_age_seconds
    if worst_age > HARD_STALENESS_CEILING_SECONDS:
        return _send_failure(
            run_context,
            f"the freshest available data is {worst_age / 3600:.1f} hours old, past the "
            f"{HARD_STALENESS_CEILING_SECONDS / 3600:.0f}-hour ceiling",
            context,
        )

    # -- 8. Identity resolution -------------------------------------------
    element_types = {t.id: t.singular_name_short for t in bootstrap.element_types}
    resolver = PlayerResolver(
        bootstrap.elements,
        element_types=element_types,
        aliases=context.store.get_aliases("premierinjuries"),
    )

    injury_by_element = _resolve_injuries(resolver, injuries.data, bootstrap, context)
    understat_by_element = _resolve_understat(resolver, understat_result.data, bootstrap, context)
    last_season_by_element = (
        _resolve_understat(resolver, last_season_result.data, bootstrap, context)
        if last_season_result is not None
        else {}
    )

    run_context.data_quality.unresolved_players = resolver.unresolved[:50]

    # -- 9. Transfer-flow analysis ----------------------------------------
    signals = _build_availability_signals(
        context, bootstrap, run_context, injury_by_element, lineups.data, team_gameweeks
    )

    # -- 10. Score ---------------------------------------------------------
    scoring_context = _build_scoring_context(
        bootstrap,
        run_context,
        understat_by_element,
        odds_result.data,
        element_types,
        last_season_by_element,
    )
    scores, minutes_by_element = _score_all(
        bootstrap, team_gameweeks, signals, scoring_context, lineups.data, run_context.now_epoch
    )

    # -- 11. Project the rest of the season --------------------------------
    # One extra Monte Carlo pass against a synthetic neutral fixture, multiplied
    # by each team's remaining fixture load. This is what the wildcard optimiser
    # maximises; the gameweek scores above are no use to it, because a wildcard
    # is a decision about the next several months rather than the next Saturday.
    projections = _project_season(
        bootstrap, all_fixtures, minutes_by_element, signals, scoring_context, run_context
    )

    # -- 12. Rank ----------------------------------------------------------
    current_event = next((e for e in bootstrap.events if e.id == run_context.gameweek), None)

    board = ranking.Board(
        buys_by_position=ranking.build_buy_board(scores, TUNABLES),
        sells=ranking.build_sell_list(scores, TUNABLES),
        watchlist=ranking.build_watchlist(scores),
        returning=_returning_players(scores),
        captains=captaincy.build_captain_picks(
            scores,
            TUNABLES,
            most_captained_element=current_event.most_captained if current_event else None,
        ),
        wildcard=_build_wildcard(scores, projections, run_context),
        horizon_note=horizon.summarise_horizon(projections, TUNABLES),
    )

    # -- 13. Benchmark against FPL's own ep_next ---------------------------
    _log_benchmark(scores)

    # -- 13b. Store the predictions so they can be graded later -------------
    # `_log_benchmark` above compares us against FPL's estimate, which tells us
    # whether we agree with FPL and nothing about whether either of us is right.
    # This is the other half: write down what we said, so that once the gameweek
    # has actually been played the backfill can score it against what happened.
    #
    # Deliberately outside the dry-run check. A dry run produces real predictions
    # from real data and simply does not email them, so they are exactly as
    # gradeable as any other - and skipping them would leave holes in the series
    # that later fitting has to work around.
    try:
        context.store.put_predictions(
            run_context.gameweek,
            run_context.tier,
            [record.as_dict() for record in calibration.summarise(scores)],
        )
    except Exception as exc:
        # A board the user is waiting for must not be lost because a write we
        # only need weeks from now failed. Log it and carry on.
        logger.warning("Could not store predictions", extra={"error": str(exc)[:200]})

    # -- 14. Render and send -----------------------------------------------
    html_body = render.render_html(run_context, board)
    text_body = render.render_text(run_context, board)

    top_pick = None
    all_buys = board.all_buys
    if all_buys:
        best = max(all_buys, key=lambda rec: rec.rank_score)
        top_pick = f"{best.score.name} ({best.score.team_short})"

    subject = email_module.build_subject(
        run_context.gameweek, run_context.tier, run_context.is_confirmed_phase, top_pick
    )

    context.archive.put_report(run_context.gameweek, run_context.tier, html_body)

    result = email_module.send_report(subject, html_body, text_body)
    if not result.sent and result.message_id != "dry-run":
        # Give the lock back so a retry can send. Better a duplicate attempt than
        # a tier that never delivers.
        context.store.release_notification_lock(run_context.gameweek, run_context.tier)
        return RunOutcome(
            "failed",
            gameweek=run_context.gameweek,
            tier=run_context.tier,
            detail=result.error or "send failed",
        )

    return RunOutcome(
        "sent",
        gameweek=run_context.gameweek,
        tier=run_context.tier,
        detail=result.message_id or "dry-run",
        recommendations=len(all_buys),
    )


# ---------------------------------------------------------------------------
# Ingest helpers
# ---------------------------------------------------------------------------
def _odds_api_key(settings) -> str | None:
    """Read the Odds API key from SSM Parameter Store.

    Parameter Store rather than an environment variable: the key is a secret, and
    a SecureString parameter is encrypted at rest, auditable in CloudTrail and
    rotatable without redeploying. It is also free, unlike Secrets Manager, which
    charges per secret per month for what is here a single low-value API key.
    """
    if not settings.odds_api_key_parameter:
        return None
    try:
        import boto3

        ssm = boto3.client("ssm", region_name=settings.aws_region)
        response = ssm.get_parameter(Name=settings.odds_api_key_parameter, WithDecryption=True)
        return response["Parameter"]["Value"]
    except Exception as exc:
        logger.warning("Could not read Odds API key from SSM", extra={"error": str(exc)})
        return None


def _apply_clubelo(
    team_gameweeks: dict[int, TeamGameweek],
    matches: list[clubelo.MatchProbabilities],
    bootstrap: Bootstrap,
) -> None:
    """Attach ClubElo clean-sheet and expected-goal figures to each fixture.

    ClubElo's club names go through the hardcoded alias map, never a fuzzy match.
    Twenty names is not a fuzzy-matching problem, and mistaking Manchester City
    for Manchester United would poison every fixture in the gameweek.
    """
    if not matches:
        return

    teams_by_name = {t.name: t.id for t in bootstrap.teams}
    by_pair: dict[tuple[int, int], clubelo.MatchProbabilities] = {}

    for match in matches:
        home_id = teams.resolve_team_id(match.home_club, teams_by_name)
        away_id = teams.resolve_team_id(match.away_club, teams_by_name)
        if home_id and away_id:
            by_pair[(home_id, away_id)] = match

    matched = 0
    for team_id, team_gameweek in team_gameweeks.items():
        enriched = []
        for fixture in team_gameweek.fixtures:
            pair = (
                (team_id, fixture.opponent_id)
                if fixture.is_home
                else (fixture.opponent_id, team_id)
            )
            match = by_pair.get(pair)
            if match is None:
                enriched.append(fixture)
                continue
            matched += 1
            # FixtureContext is frozen, so we rebuild rather than mutate. Frozen
            # dataclasses in the domain layer are deliberate: it makes accidental
            # action-at-a-distance impossible.
            #
            # dataclasses.replace, not a field-by-field constructor call: the
            # constructor call used to list every field explicitly, which is
            # exactly how `kickoff_epoch` got silently dropped for every
            # ClubElo-enriched fixture when that field was added - a new field
            # is invisible to a list that has to be updated by hand, but
            # `replace` carries it forward for free.
            enriched.append(
                replace(
                    fixture,
                    clean_sheet_probability=match.clean_sheet_for(is_home=fixture.is_home),
                    expected_team_goals=match.expected_goals_for(is_home=fixture.is_home),
                    expected_goals_conceded=match.expected_conceded_for(is_home=fixture.is_home),
                    win_probability=(match.home_win if fixture.is_home else match.away_win),
                )
            )
        team_gameweeks[team_id] = TeamGameweek(
            team_id=team_id, gameweek=team_gameweek.gameweek, fixtures=tuple(enriched)
        )

    logger.info("Applied ClubElo probabilities", extra={"fixtures_matched": matched})


def _apply_odds_clean_sheets(
    team_gameweeks: dict[int, TeamGameweek],
    odds: oddsapi.OddsData | None,
    bootstrap: Bootstrap,
    run_context: RunContext,
) -> None:
    """Fill missing clean-sheet probabilities from the betting market.

    Only touches fixtures ClubElo left empty, so this is a no-op whenever ClubElo
    is healthy - a real scoreline model beats anything derived, and ClubElo's is
    already vig-free.

    It matters because the alternative was FPL's 1-5 difficulty scale, which
    rates a FIXTURE rather than a team. In GW1 2026-27 it gave newly-promoted
    Ipswich at home the same difficulty 2 as Arsenal at home, both sides were
    modelled as conceding 1.01 goals, and the entire defender board collapsed
    into a 0.24 xP band - at which point the ranking was decided by nothing but
    who was least owned.

    Costs no extra API credits: the 1X2 and totals markets are already fetched
    for match probabilities, and two numbers are enough to pin a two-parameter
    goal model.
    """
    if odds is None or not odds.matches:
        return

    teams_by_name = {t.name: t.id for t in bootstrap.teams}
    rates: dict[tuple[int, int], tuple[float, float]] = {}
    for match in odds.matches:
        home_id = teams.resolve_team_id(match.home_team, teams_by_name)
        away_id = teams.resolve_team_id(match.away_team, teams_by_name)
        if not (home_id and away_id):
            continue
        derived = devig.goal_rates_from_odds(
            match.home_win, match.draw, match.away_win, match.over_2_5, match.under_2_5
        )
        if derived:
            rates[(home_id, away_id)] = derived

    if not rates:
        return

    filled = 0
    for team_id, team_gameweek in team_gameweeks.items():
        enriched: list[FixtureContext] = []
        for fixture in team_gameweek.fixtures:
            pair = (
                (team_id, fixture.opponent_id)
                if fixture.is_home
                else (fixture.opponent_id, team_id)
            )
            derived = rates.get(pair)
            if derived is None or fixture.clean_sheet_probability is not None:
                enriched.append(fixture)
                continue

            lam_home, lam_away = derived
            conceded = lam_away if fixture.is_home else lam_home
            scored = lam_home if fixture.is_home else lam_away
            filled += 1
            # dataclasses.replace - see `_apply_clubelo` for why, not a
            # field-by-field constructor call that silently drops any field
            # (kickoff_epoch, previously) not added to the list by hand.
            enriched.append(
                replace(
                    fixture,
                    clean_sheet_probability=devig.clean_sheet_probability_from_goals(conceded),
                    expected_team_goals=scored,
                    expected_goals_conceded=conceded,
                )
            )
        team_gameweeks[team_id] = TeamGameweek(
            team_id=team_id, gameweek=team_gameweek.gameweek, fixtures=tuple(enriched)
        )

    if filled:
        logger.info("Filled clean sheets from the market", extra={"fixtures": filled})
        run_context.data_quality.add_caveat(
            f"ClubElo was unavailable, so clean-sheet probabilities for {filled} fixture(s) "
            "were derived from devigged betting odds instead. That is a market consensus "
            "rather than a scoreline model, and slightly less precise."
        )


def _resolve_injuries(
    resolver: PlayerResolver,
    data: premierinjuries.InjuryData | None,
    bootstrap: Bootstrap,
    context: SourceContext,
) -> dict[int, premierinjuries.InjuryRecord]:
    """Map injury records onto FPL element ids and persist new aliases."""
    if data is None:
        return {}

    teams_by_name = {t.name: t.id for t in bootstrap.teams}
    out: dict[int, premierinjuries.InjuryRecord] = {}
    suppressed_bans: list[str] = []

    for record in data.records:
        team_id = (
            teams.resolve_team_id(record.team_name, teams_by_name) if record.team_name else None
        )
        resolution = resolver.resolve(
            record.name,
            source="premierinjuries",
            source_id=record.source_id,
            team_id=team_id,
        )
        if resolution.resolved and resolution.element_id is not None:
            out[resolution.element_id] = record
        elif record.is_suspension:
            # Not a data-quality failure. PremierInjuries keeps listing players
            # FPL has removed from the game, and a long ban is the usual reason
            # it removed them - Mykhailo Mudryk was listed "Suspended" here while
            # absent from bootstrap-static entirely, so the resolver had nothing
            # to match against and correctly rejected its best guess ("Gusto",
            # score 48).
            #
            # Reporting that as an unmatched name puts a permanent, unfixable
            # entry in the caveats, and the caveat list is where genuine
            # mismatches are meant to stand out - a mismatch on an ACTIVE player
            # silently drops his injury data, which is the case worth seeing. A
            # banned player who cannot be selected costs nothing by being absent.
            suppressed_bans.append(record.name)

    # Persist anything newly resolved so this player is never fuzzy-matched again.
    for source_id, (element_id, method, score) in resolver.newly_resolved.items():
        context.store.put_alias(
            "premierinjuries", source_id, element_id, method=method.value, score=score
        )

    if suppressed_bans:
        # Logged, not surfaced. Worth being able to find; not worth a caveat.
        logger.info(
            "Suspended players absent from FPL - excluded from unresolved names",
            extra={"players": suppressed_bans},
        )
        resolver.unresolved = [
            entry
            for entry in resolver.unresolved
            if not any(entry.startswith(f"{name} (premierinjuries") for name in suppressed_bans)
        ]

    logger.info(
        "Resolved injury records",
        extra={
            "records": len(data.records),
            "matched": len(out),
            "unresolved": len(resolver.unresolved),
        },
    )
    return out


def _resolve_understat(
    resolver: PlayerResolver,
    data: understat.UnderstatData | None,
    bootstrap: Bootstrap,
    context: SourceContext,
) -> dict[int, understat.UnderstatPlayer]:
    """Map Understat players onto FPL element ids."""
    if data is None or not data.players:
        return {}

    teams_by_name = {t.name: t.id for t in bootstrap.teams}
    aliases = context.store.get_aliases("understat")
    out: dict[int, understat.UnderstatPlayer] = {}

    for player in data.players:
        team_id = teams.resolve_team_id(player.team_title, teams_by_name)
        cached = aliases.get(player.understat_id)
        if cached:
            out[cached] = player
            continue
        resolution = resolver.resolve(
            player.name,
            source="understat",
            source_id=player.understat_id,
            team_id=team_id,
        )
        if resolution.resolved and resolution.element_id is not None:
            out[resolution.element_id] = player
            context.store.put_alias(
                "understat",
                player.understat_id,
                resolution.element_id,
                method=resolution.method.value,
                score=resolution.score,
            )

    logger.info(
        "Resolved Understat players",
        extra={"players": len(data.players), "matched": len(out)},
    )
    return out


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------
def _hours_to_kickoff(team_gameweek: TeamGameweek | None, now_epoch: int) -> float | None:
    """Hours from now until this team's earliest fixture this gameweek kicks off.

    None when the team has no fixture (a blank) or the kickoff time is unknown
    (provisional/TBC) - in both cases `lineup_confidence` treats None as full
    trust rather than guessing at staleness from an absent input.
    """
    if team_gameweek is None:
        return None
    kickoff_epoch = team_gameweek.earliest_kickoff_epoch
    if kickoff_epoch is None:
        return None
    return (kickoff_epoch - now_epoch) / 3600


def _build_availability_signals(
    context: SourceContext,
    bootstrap: Bootstrap,
    run_context: RunContext,
    injuries: dict[int, premierinjuries.InjuryRecord],
    lineups: ffs.LineupData | None,
    team_gameweeks: dict[int, TeamGameweek],
) -> dict[int, AvailabilitySignal]:
    """Fuse FPL, PremierInjuries, predicted line-ups and transfer flow."""
    snapshots = context.store.recent_snapshots(limit=72)
    current_event = next((e for e in bootstrap.events if e.id == run_context.gameweek), None)
    chip_discount = availability_domain.chip_discount_for_event(
        current_event, bootstrap.total_players
    )

    flow_series = availability_domain.flow_series_from_snapshots(
        snapshots, bootstrap.total_players, chip_discount
    )

    caveat = availability_domain.cold_start_caveat(len(snapshots))
    if caveat:
        run_context.data_quality.add_caveat(caveat)

    # Per-team z-scores, so we can test whether team-mates are moving together -
    # the discriminator that separates an injury from a fixture swing.
    zscores: dict[int, float | None] = {}
    net_per_owner: dict[int, float] = {}

    for element in bootstrap.elements:
        series = flow_series.get(element.id, [])
        net, _ = availability_domain.normalise_flow(
            element.transfers_in_event,
            element.transfers_out_event,
            element.ownership,
            bootstrap.total_players,
            chip_discount=chip_discount,
        )
        net_per_owner[element.id] = net
        zscores[element.id] = availability_domain.flow_zscore(net, series)

    by_team: dict[int, list[float]] = {}
    for element in bootstrap.elements:
        z = zscores.get(element.id)
        if z is not None:
            by_team.setdefault(element.team, []).append(z)

    hour_of_day = datetime.fromtimestamp(run_context.now_epoch, tz=UTC).hour
    signals: dict[int, AvailabilitySignal] = {}

    for element in bootstrap.elements:
        record = injuries.get(element.id)
        z = zscores.get(element.id)
        cause = availability_domain.classify_flow_cause(
            element,
            z,
            net_per_owner[element.id],
            [other for other in by_team.get(element.team, []) if other != z],
            hour_of_day=hour_of_day,
        )

        predicted = None
        if lineups is not None and lineups.predicted_starters:
            predicted = lineups.is_predicted_to_start(element.id)

        news_age = None
        if element.news_added_at is not None:
            news_age = (run_context.now_epoch - element.news_added_at.timestamp()) / 3600

        hours_to_kickoff = _hours_to_kickoff(team_gameweeks.get(element.team), run_context.now_epoch)

        signals[element.id] = availability_domain.build_availability_signal(
            element,
            injury_status=record.status if record else None,
            injury_condition=record.condition if record else None,
            injury_reason=record.reason if record else None,
            potential_return=(
                record.potential_return.isoformat() if record and record.potential_return else None
            ),
            predicted_to_start=predicted,
            net_per_owner=net_per_owner[element.id],
            zscore=z,
            flow_cause=cause,
            snapshots_available=len(snapshots),
            news_age_hours=news_age,
            hours_to_kickoff=hours_to_kickoff,
        )

    # Suppressed on the confirmed tier: by T-3h the pressers have happened, so a
    # player still carrying this flag is a genuine unknown rather than one we are
    # simply early for. On the T-24h report it is the opposite - the flag means
    # "ask again later", and the T-3h report is where that answer arrives.
    awaiting = sum(1 for s in signals.values() if s.awaiting_press_conference)
    if awaiting and not run_context.is_confirmed_phase:
        run_context.data_quality.add_caveat(
            f"{awaiting} player(s) are listed as 'Currently Being Assessed'. Their status "
            "will be resolved by managers' press conferences, which land after this run. "
            "The T-3h report is the one to act on for them."
        )

    return signals


# Below this many players in the current season's Understat data, the season is
# too young to stand on its own and last season is fetched as the prior. Around
# a third of a squad-sized league: enough that rates have begun to mean
# something, and reached within a handful of gameweeks.
MIN_UNDERSTAT_PLAYERS_FOR_SELF_SUFFICIENCY = 200


def _build_scoring_context(
    bootstrap: Bootstrap,
    run_context: RunContext,
    understat_players: dict[int, understat.UnderstatPlayer],
    odds: oddsapi.OddsData | None,
    element_types: dict[int, str],
    last_season_players: dict[int, understat.UnderstatPlayer] | None = None,
) -> scoring.ScoringContext:
    """Assemble the scorer's inputs.

    The RNG is seeded from the gameweek and tier rather than left to the clock.
    That makes a run reproducible: re-invoking for the same gameweek and tier
    produces the same board, so a difference between two runs means the *data*
    changed, not the dice. When you are debugging why a pick moved, that property
    is worth a great deal.
    """
    seed = abs(hash((run_context.season, run_context.gameweek, run_context.tier))) % (2**32)
    rng = np.random.default_rng(seed)

    xg90 = {}
    xa90 = {}
    for element_id, player in understat_players.items():
        if player.npxg_per_90 is not None:
            xg90[element_id] = player.npxg_per_90
        if player.xa_per_90 is not None:
            xa90[element_id] = player.xa_per_90

    # Bonus is only modelled for the players where BPS is predictable. Ranking by
    # season BPS is a reasonable proxy for "involved in the good stuff".
    top_bps = {
        element.id
        for element in sorted(bootstrap.elements, key=lambda e: e.bps, reverse=True)[
            : TUNABLES.bonus_model_top_n
        ]
    }

    goalscorer: dict[int, float] = {}
    if odds and odds.goalscorer_probabilities:
        # Odds API gives names, not ids. Match against display names within the
        # already-loaded element list; anything that does not match is dropped
        # rather than guessed.
        by_name = {element.display_name().lower(): element.id for element in bootstrap.elements}
        for name, probability in odds.goalscorer_probabilities.items():
            element_id = by_name.get(name.lower())
            if element_id:
                goalscorer[element_id] = probability

    games_played: dict[int, int] = {}
    if run_context.season_has_started:
        finished = sum(1 for e in bootstrap.events if e.finished and e.id < run_context.gameweek)
        games_played = {team.id: finished for team in bootstrap.teams}

    # NON-penalty xG, matching the current-season block above. Penalties are
    # modelled separately from `penalties_order`, so using raw xG here would
    # double-count designated takers - and would do it only in the prior, which
    # is the sort of asymmetry that is very hard to spot later.
    #
    # The per-90 properties already return None below any minutes, so there is no
    # separate minutes guard.
    prior_xg: dict[int, float] = {}
    prior_xa: dict[int, float] = {}
    for element_id, player in (last_season_players or {}).items():
        if player.npxg_per_90 is not None:
            prior_xg[element_id] = player.npxg_per_90
        if player.xa_per_90 is not None:
            prior_xa[element_id] = player.xa_per_90

    return scoring.ScoringContext(
        element_types=element_types,
        scoring_rules=bootstrap.game_config.scoring,
        season_has_started=run_context.season_has_started,
        tunables=TUNABLES,
        rng=rng,
        xg90_by_element=xg90,
        xa90_by_element=xa90,
        prior_xg90_by_element=prior_xg,
        prior_xa90_by_element=prior_xa,
        goalscorer_probability=goalscorer,
        team_games_played=games_played,
        top_bps_elements=top_bps,
    )


def _score_all(
    bootstrap: Bootstrap,
    team_gameweeks: dict[int, TeamGameweek],
    signals: dict[int, AvailabilitySignal],
    scoring_context: scoring.ScoringContext,
    lineups: ffs.LineupData | None,
    now_epoch: int,
) -> tuple[list[PlayerScore], dict[int, MinutesDistribution]]:
    """Score every transactable player.

    Also returns the minutes distributions, because the season-horizon
    projection needs exactly the same ones. Recomputing them there would be
    wasteful, and - worse - would risk the two drifting apart if the minutes
    model ever gains an input that only one caller passes.
    """
    teams_by_id = bootstrap.teams_by_id()
    scores: list[PlayerScore] = []
    minutes_by_element: dict[int, MinutesDistribution] = {}

    for element in bootstrap.elements:
        # `can_transact` is FPL's own answer to "may this player be bought", and
        # it is a safer hard filter than interpreting `status` ourselves.
        if not element.is_transactable:
            continue

        team_gameweek = team_gameweeks.get(element.team)
        if team_gameweek is None:
            continue

        signal = signals.get(element.id) or AvailabilitySignal(element_id=element.id, risk=0.0)

        predicted = None
        if lineups is not None and lineups.predicted_starters:
            predicted = lineups.is_predicted_to_start(element.id)

        minutes_dist = estimate_minutes(
            element,
            signal,
            season_has_started=scoring_context.season_has_started,
            predicted_to_start=predicted,
            games_played=scoring_context.team_games_played.get(element.team, 0),
            hours_to_kickoff=_hours_to_kickoff(team_gameweek, now_epoch),
        )
        minutes_by_element[element.id] = minutes_dist

        opponent_names = [
            f"{teams_by_id[f.opponent_id].short_name}{'(H)' if f.is_home else '(A)'}"
            for f in team_gameweek.fixtures
            if f.opponent_id in teams_by_id
        ]

        scores.append(
            scoring.build_player_score(
                element,
                teams_by_id[element.team].short_name if element.team in teams_by_id else "???",
                team_gameweek,
                minutes_dist,
                signal,
                scoring_context,
                opponent_names,
            )
        )

    logger.info("Scored players", extra={"count": len(scores)})
    return scores, minutes_by_element


def _project_season(
    bootstrap: Bootstrap,
    all_fixtures: list,
    minutes_by_element: dict[int, MinutesDistribution],
    signals: dict[int, AvailabilitySignal],
    scoring_context: scoring.ScoringContext,
    run_context: RunContext,
) -> dict[int, horizon.SeasonProjection]:
    """Project every player's remaining season.

    Uses the same minutes distributions and scoring context as the gameweek
    pass, so the two views of a player cannot disagree about who he is - only
    about which fixtures he faces.
    """
    fixture_loads = horizon.build_fixture_loads(
        all_fixtures,
        [team.id for team in bootstrap.teams],
        run_context.gameweek,
        max_gameweeks=TUNABLES.horizon_max_gameweeks,
    )

    return horizon.project_all(
        [element for element in bootstrap.elements if element.is_transactable],
        minutes_by_element,
        signals,
        fixture_loads,
        scoring_context,
        TUNABLES,
        run_context.gameweek,
    )


def _build_wildcard(
    scores: list[PlayerScore],
    projections: dict[int, horizon.SeasonProjection],
    run_context: RunContext,
) -> squad.WildcardSquad | None:
    """Assemble candidates and run the wildcard optimiser.

    Availability filtering happens here rather than inside the optimiser, which
    keeps the optimiser a pure combinatorial routine with no opinions about
    football. A player carrying serious injury risk is excluded outright: a
    wildcard is a full rebuild and there is no reason to spend any of the budget
    on someone who may not play.
    """
    if not projections:
        run_context.data_quality.add_caveat(
            "No season projections available, so no wildcard squad was built. "
            "This is expected before the season starts."
        )
        return None

    candidates = [
        squad.Candidate(
            element_id=score.element_id,
            name=score.name,
            position=score.position,
            team_id=score.team_id,
            team_short=score.team_short,
            price_tenths=round(score.price * 10),
            season_xp=projections[score.element_id].season_xp,
            gameweek_xp=score.mean,
            ownership=score.ownership,
            availability_risk=score.availability.risk,
        )
        for score in scores
        if score.element_id in projections and score.availability.risk < 0.5
    ]

    if len(candidates) < 15:
        run_context.data_quality.add_caveat(
            f"Only {len(candidates)} players were eligible for the wildcard optimiser, "
            "which is too few to build a legal squad."
        )
        return None

    # Seeded from the gameweek so the same run reproduces the same squad. A
    # wildcard draft that changed every hour for no reason would be impossible
    # to trust or to argue with.
    result = squad.optimise_squad(candidates, TUNABLES, seed=run_context.gameweek)

    if result is None:
        run_context.data_quality.add_caveat(
            "The wildcard optimiser could not find a legal squad within budget."
        )
    return result


def _returning_players(scores: list[PlayerScore]) -> list[tuple[PlayerScore, str]]:
    """Players whose availability is improving - the buy-low window."""
    out: list[tuple[PlayerScore, str]] = []
    for score in scores:
        availability = score.availability
        if availability.is_suspension:
            continue
        # Partially fit and improving: FPL still shows a doubt but the player is
        # in the predicted XI, which is the earliest reliable "he is back" signal.
        if 0 < availability.fpl_chance_pct < 100 and availability.predicted_to_start:
            out.append(
                (
                    score,
                    f"FPL still lists {availability.fpl_chance_pct}% but he is in the "
                    "predicted XI - the price has not caught up yet",
                )
            )
    out.sort(key=lambda pair: pair[0].mean, reverse=True)
    return out[:8]


def _log_benchmark(scores: list[PlayerScore]) -> None:
    """Compare our xP against FPL's own `ep_next`.

    SPEC 5.5: *a model that cannot beat `ep_next` is not worth shipping.* This
    logs the comparison every run so drift is visible over a season rather than
    discovered during a post-mortem. It does not gate the send - the real
    evaluation is the backtest harness - but it is the cheapest possible ongoing
    sanity check.
    """
    paired = [(s.mean, s.ep_next) for s in scores if s.ep_next > 0]
    if len(paired) < 30:
        return

    ours = np.array([p[0] for p in paired])
    theirs = np.array([p[1] for p in paired])

    # Spearman via ranks - no scipy needed, and rank correlation is the right
    # measure here because we care about ordering, not calibration.
    correlation = float(np.corrcoef(_ranks(ours), _ranks(theirs))[0, 1])

    logger.info(
        "Benchmark against FPL ep_next",
        extra={
            "players": len(paired),
            "spearman_vs_ep_next": round(correlation, 4),
            "our_mean": round(float(ours.mean()), 3),
            "ep_next_mean": round(float(theirs.mean()), 3),
        },
    )


def _ranks(values: np.ndarray) -> np.ndarray:
    """Ordinal ranks, ties broken arbitrarily. Good enough for a sanity check."""
    order = values.argsort()
    ranks = np.empty_like(order, dtype=np.float64)
    ranks[order] = np.arange(len(values))
    return ranks


def _send_failure(
    run_context: RunContext, reason: str, context: SourceContext | None = None
) -> RunOutcome:
    """Email about the failure rather than about transfers.

    The lock is RELEASED afterwards, and that is the important part. The lock
    means "this tier has had its board"; a failure notice is not a board.
    Holding it turned a transient upstream outage into a permanently missed
    gameweek - the source recovers twenty minutes later, the next hourly run
    finds the tier already claimed, exits `suppressed`, and the deadline passes
    in silence.

    Releasing it lets any later run inside the same tier deliver the real thing.
    The cost is that a persistent outage repeats this notice hourly, which is
    noisy but honest, and far cheaper than a silent gameweek.
    """
    logger.error("Refusing to send recommendations", extra={"reason": reason})
    count(Metric.RUN_ABANDONED_STALE)

    html_body = render.render_failure_html(run_context, reason)
    subject = f"fplBot GW{run_context.gameweek}: could not produce a reliable board"
    email_module.send_report(subject, html_body, f"{subject}\n\n{reason}")

    if context is not None:
        try:
            context.store.release_notification_lock(run_context.gameweek, run_context.tier)
            logger.info(
                "Released the tier lock after a failure notice",
                extra={"gameweek": run_context.gameweek, "tier": run_context.tier},
            )
        except Exception as exc:
            logger.warning("Could not release the lock", extra={"error": str(exc)[:200]})

    return RunOutcome("failed", gameweek=run_context.gameweek, tier=run_context.tier, detail=reason)
