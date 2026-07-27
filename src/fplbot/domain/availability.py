"""Availability risk - the headline feature.

The idea: **transfer flow is a leading indicator of team news.** When a player
picks up a knock in training, the news reaches Twitter, local reporters and
well-connected managers before FPL updates `news` or `chance_of_playing`. Those
managers transfer him out. So a spike in `transfers_out_event` can tell us
something is wrong hours before the official flag appears.

That is the promise. Turning it into something usable takes four pieces of care,
each of which is where a naive implementation goes wrong.

1. **NORMALISE BY OWNERSHIP. NEVER USE ABSOLUTE COUNTS.**
   A 3%-owned player and a 40%-owned player with the same absolute net-outflow
   are telling completely different stories: the first has lost a large fraction
   of his owners, the second a trivial one. We use

       net_event / (selected_by_percent x total_players)

   which is net flow as a fraction of *current owners*. Absolute counts are
   dominated by ownership and would put the same five template players at the
   top of the list every single week.

2. **Z-SCORE AGAINST A PER-PLAYER BASELINE, NOT A GLOBAL ONE.**
   Players have wildly different baseline churn. A rotation-risk midfielder is
   always being shuffled; a nailed defender is not. An EWMA of the player's own
   recent normalised flow is the right reference, and a global distribution would
   flag the volatile players every week and never flag the stable one whose
   sudden movement is the actual signal.

3. **DISCRIMINATE THE CAUSE.** This is the hard part and the source of most
   false positives. A transfer spike has at least five plausible causes and only
   one of them is injury news:

       bad news        sharp, ownership-normalised, ONE-DIRECTIONAL, often
                       out-of-hours (news breaks at 21:00, not 11:00)
       price rise      net IN, correlates with cost_change_event momentum
       fixture swing   gradual, coincides with a fixture change, affects
                       team-mates too
       post-DGW churn  affects a whole team's players at once
       chip weeks      contaminate everything

   The team-mate correlation test is the most valuable discriminator: an injury
   is idiosyncratic to one player, whereas a fixture swing or post-blank churn
   moves an entire club's roster together.

4. **CHIP CONTAMINATION.** Wildcards inflate raw transfer counts by roughly 29%
   while contributing about 1.4% of genuine transfer pressure - a wildcarding
   manager is rebuilding a squad, not reacting to news about your player.
   `events[].chip_plays` gives us the counts to discount by, and without it
   GW1-2, GW20-21 and every post-blank week read as alarming.

COLD START - AND SAYING SO
--------------------------
Z-scoring needs snapshot history. Per-gameweek flows come free from
`element-summary` history, but *intra-gameweek* velocity does not exist until the
bot has been running. **This feature will be weak for its first few gameweeks**,
and SPEC section 5.3 is explicit that we must say so in the email rather than
present a low-confidence signal as though it were sharp. `cold_start_caveat`
below produces that sentence.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from fplbot.config import TUNABLES
from fplbot.models.domain import AvailabilitySignal, RiskLevel
from fplbot.models.fpl import Element, Event
from fplbot.observability import logger


@dataclass
class FlowObservation:
    """One player's normalised transfer flow at one point in time."""

    element_id: int
    timestamp: str
    net_per_owner: float
    transfers_in_event: int
    transfers_out_event: int
    owners: int
    cost_change_event: int


def normalise_flow(
    transfers_in_event: int,
    transfers_out_event: int,
    ownership_percent: float,
    total_players: int,
    *,
    chip_discount: float = 0.0,
) -> tuple[float, int]:
    """Net transfer flow as a fraction of current owners.

    Returns (net_per_owner, owner_count). Negative means net outflow, which is
    the direction that signals bad news.

    `chip_discount` shrinks the raw counts to account for wildcard churn. It is
    applied to both directions because a wildcard shows up as both an in and an
    out somewhere.
    """
    owners = max(1, int(ownership_percent / 100.0 * total_players))
    discount = 1.0 - chip_discount
    net = (transfers_in_event - transfers_out_event) * discount
    return net / owners, owners


def chip_discount_for_event(event: Event | None, total_players: int) -> float:
    """How much of this gameweek's transfer volume is chip noise.

    Scaled by how many managers actually played a wildcard, so a quiet week gets
    almost no discount and a post-blank stampede gets close to the full 29%.
    """
    if event is None or total_players <= 0:
        return 0.0
    wildcard_rate = event.wildcards_played / total_players
    # The 29% figure is the inflation observed in a heavy wildcard week; scale it
    # by how heavy this week actually is, capped so we never discount everything.
    return min(TUNABLES.chip_contamination_discount, wildcard_rate * 4.0)


def ewma_baseline(values: list[float], alpha: float = TUNABLES.ewma_alpha) -> tuple[float, float]:
    """Exponentially weighted mean and standard deviation.

    EWMA rather than a plain rolling mean because relevance decays: what a player's
    flow looked like three days ago matters more than what it looked like two
    weeks ago, and a hard window boundary would make the z-score jump whenever an
    old observation falls out of it.

    Returns (mean, std). Std is floored at a small positive value so that a
    perfectly flat history cannot produce a division by zero and an infinite
    z-score - which is exactly what happens for a player nobody has transferred
    all week, i.e. the common case.
    """
    if not values:
        return 0.0, 1e-6

    mean = values[0]
    variance = 0.0
    for value in values[1:]:
        delta = value - mean
        mean += alpha * delta
        variance = (1 - alpha) * (variance + alpha * delta * delta)

    return mean, max(math.sqrt(variance), 1e-6)


def flow_zscore(current: float, history: list[float]) -> float | None:
    """Z-score of the current flow against the player's own EWMA baseline.

    Returns None when there is not enough history to be meaningful. Returning
    None rather than a number computed from three observations is the point:
    a confident-looking z-score derived from almost no data is worse than an
    honest gap, because it will end up in an email as though it meant something.
    """
    if len(history) < TUNABLES.min_snapshots_for_zscore:
        return None
    mean, std = ewma_baseline(history)
    return (current - mean) / std


def classify_flow_cause(
    element: Element,
    zscore: float | None,
    net_per_owner: float,
    team_mate_zscores: list[float],
    *,
    hour_of_day: int | None = None,
) -> str | None:
    """Attribute a transfer spike to a cause. The false-positive filter.

    Order matters: we test the benign explanations first, so that "bad news" is
    what remains once everything else has been ruled out rather than the default
    conclusion.
    """
    if zscore is None or abs(zscore) < TUNABLES.transfer_flow_z_warning:
        return None

    # Net INFLOW during a price-rise run is a bandwagon, not a warning. The
    # direction alone settles this one.
    if net_per_owner > 0:
        if element.cost_change_event > 0:
            return "price_bandwagon"
        return "positive_momentum"

    # Team-mates moving together means something happened to the CLUB - a fixture
    # swing, a blank resolving, a manager sacking - not to this player. This is
    # the single most useful discriminator we have, because injury news is
    # idiosyncratic by nature.
    if team_mate_zscores:
        moving_together = sum(1 for z in team_mate_zscores if z < -TUNABLES.transfer_flow_z_warning)
        if moving_together >= max(3, len(team_mate_zscores) // 3):
            return "team_wide_churn"

    # A sharp, one-directional, idiosyncratic outflow. Out-of-hours movement
    # strengthens the case considerably: news breaks in the evening, whereas
    # routine transfer planning happens during the day.
    if zscore <= -TUNABLES.transfer_flow_z_alarm:
        if hour_of_day is not None and (hour_of_day >= 20 or hour_of_day <= 7):
            return "bad_news_out_of_hours"
        return "bad_news"

    return "mild_outflow"


def build_availability_signal(
    element: Element,
    *,
    injury_status: str | None = None,
    injury_condition: str | None = None,
    injury_reason: str | None = None,
    potential_return: str | None = None,
    predicted_to_start: bool | None = None,
    net_per_owner: float | None = None,
    zscore: float | None = None,
    flow_cause: str | None = None,
    snapshots_available: int = 0,
    news_age_hours: float | None = None,
) -> AvailabilitySignal:
    """Fuse every availability input into one risk score in [0, 1].

    The score multiplies into expected points, so 0 means "will definitely play"
    and 1 means "will definitely not".

    Cross-validation drives the confidence, not the score: agreement between FPL
    and PremierInjuries lands in `corroborating_sources`, disagreement in
    `conflicting_sources`, and the report shows both rather than silently picking
    a winner. SPEC 5.3 step 4.
    """
    signal = AvailabilitySignal(
        element_id=element.id,
        risk=0.0,
        fpl_chance_pct=element.availability_pct,
        fpl_status=element.status,
        fpl_news=element.news,
        news_age_hours=news_age_hours,
        scout_news_link=element.scout_news_link,
        net_flow_per_owner=net_per_owner,
        flow_zscore=zscore,
        flow_cause=flow_cause,
        snapshots_available=snapshots_available,
        injury_status=injury_status,
        injury_condition=injury_condition,
        injury_reason=injury_reason,
        potential_return=potential_return,
        predicted_to_start=predicted_to_start,
    )

    # -- Base risk from FPL's own number ----------------------------------
    risk = 1.0 - (element.availability_pct / 100.0)

    # -- Third-party corroboration ----------------------------------------
    if injury_status is not None:
        third_party_risk = 1.0 - (
            {"Ruled Out": 0, "25%": 25, "50%": 50, "75%": 75}.get(injury_status, 100) / 100.0
        )
        if abs(third_party_risk - risk) < 0.2:
            signal.corroborating_sources.append("premierinjuries")
            # Agreement between two independent sources should sharpen the
            # estimate, so we take the more pessimistic of two close readings.
            risk = max(risk, third_party_risk)
        else:
            signal.conflicting_sources.append("premierinjuries")
            signal.notes.append(
                f"FPL says {element.availability_pct}% but PremierInjuries says "
                f"{injury_status}. Treat with caution - the sources disagree."
            )
            # Disagreement means we know less, not more. Sit between them rather
            # than picking a side, and let the confidence downgrade reflect it.
            risk = (risk + third_party_risk) / 2

    # -- Transfer-flow inference ------------------------------------------
    # This only ever ADDS risk, and only when the cause has been attributed to
    # news. A managers' stampede is evidence; it is not proof, and it must not be
    # able to rule a fit player out on its own.
    if flow_cause in {"bad_news", "bad_news_out_of_hours"} and zscore is not None:
        severity = min(1.0, abs(zscore) / (TUNABLES.transfer_flow_z_alarm * 2))
        inferred = 0.35 * severity
        if flow_cause == "bad_news_out_of_hours":
            inferred *= 1.3  # out-of-hours movement is the stronger tell
        risk = min(1.0, risk + inferred)
        signal.notes.append(
            f"Transfer outflow is {abs(zscore):.1f} standard deviations below this "
            f"player's own baseline, with no corresponding price or fixture "
            f"explanation. FPL has not flagged him. This is a leading indicator, "
            f"not a confirmation."
        )

    # -- Predicted line-ups ------------------------------------------------
    if predicted_to_start is False and risk < 0.3:
        risk = max(risk, 0.30)
        signal.notes.append("Not in Fantasy Football Scout's predicted XI.")
    elif predicted_to_start is True:
        signal.corroborating_sources.append("ffs")

    # -- Suspensions are deterministic ------------------------------------
    if signal.is_suspension:
        risk = 1.0
        signal.notes.append(
            "Suspended - a ban is deterministic, not a fitness question. "
            "No return probability is modelled."
        )

    # -- Awaiting a press conference --------------------------------------
    if signal.awaiting_press_conference:
        signal.notes.append(
            "Listed as 'Currently Being Assessed'. A manager's press conference "
            "will resolve this, typically Thursday or Friday - after the 48-hour "
            "run. The T-3h report is the one to act on for this player."
        )

    signal.risk = round(min(1.0, max(0.0, risk)), 3)
    signal.level = _risk_level(signal.risk)
    return signal


def _risk_level(risk: float) -> RiskLevel:
    if risk >= 0.95:
        return RiskLevel.OUT
    if risk >= 0.6:
        return RiskLevel.SERIOUS
    if risk >= 0.3:
        return RiskLevel.DOUBT
    if risk >= 0.1:
        return RiskLevel.WATCH
    return RiskLevel.CLEAR


# ---------------------------------------------------------------------------
# Series construction from stored snapshots
# ---------------------------------------------------------------------------
def flow_series_from_snapshots(
    snapshots: list[dict], total_players: int, chip_discount: float = 0.0
) -> dict[int, list[float]]:
    """Turn stored hourly snapshots into a per-player normalised flow series.

    Snapshots arrive newest-first from DynamoDB; we reverse so the EWMA runs
    forwards in time, which is the only order in which an exponentially weighted
    average means anything.
    """
    series: dict[int, list[float]] = defaultdict(list)
    for snapshot in reversed(snapshots):
        players = snapshot.get("players", [])
        snapshot_total = snapshot.get("total_players") or total_players
        for row in players:
            try:
                ownership = float(row.get("selected_by_percent") or 0)
            except (TypeError, ValueError):
                continue
            net, _ = normalise_flow(
                int(row.get("transfers_in_event") or 0),
                int(row.get("transfers_out_event") or 0),
                ownership,
                snapshot_total,
                chip_discount=chip_discount,
            )
            series[int(row["id"])].append(net)
    return dict(series)


def cold_start_caveat(snapshots_available: int) -> str | None:
    """The sentence that must appear in the email when history is thin.

    SPEC section 5.3 requires this explicitly. Presenting a z-score built from
    four observations as though it were a sharp signal is exactly the kind of
    confident wrongness the whole spec is written to avoid.
    """
    if snapshots_available >= TUNABLES.min_snapshots_for_zscore:
        return None
    return (
        f"Injury inference from transfer flow is running on {snapshots_available} "
        f"hourly snapshots, below the {TUNABLES.min_snapshots_for_zscore} needed for a "
        "meaningful baseline. Per-gameweek flows are available from FPL's own history, "
        "but intra-gameweek velocity needs the bot to have been running. "
        "Treat this section as weak for the next few gameweeks."
    )


def detect_return_signals(
    elements: list[Element],
    previous_chances: dict[int, int],
    flow_series: dict[int, list[float]],
) -> list[tuple[Element, str]]:
    """Players stepping back towards fitness - the buy-low window.

    The pattern from SPEC 5.3 step 5: `chance_of_playing` climbing through
    25 -> 50 -> 75 -> 100, transfers accelerating in, minutes returning. Worth its
    own section in the report because it is the one signal where acting *before*
    the crowd is both possible and cheap - the price has not moved yet.
    """
    returning: list[tuple[Element, str]] = []
    for element in elements:
        previous = previous_chances.get(element.id)
        current = element.availability_pct
        if previous is None or current <= previous:
            continue
        if previous >= 100:
            continue

        reasons = [f"chance of playing improved from {previous}% to {current}%"]

        series = flow_series.get(element.id, [])
        if len(series) >= 3 and sum(series[-3:]) > 0:
            reasons.append("transfers turning net positive")
        if element.status == "a" and previous < 100:
            reasons.append("FPL status now available")

        returning.append((element, "; ".join(reasons)))

    if returning:
        logger.info("Detected return-from-injury signals", extra={"count": len(returning)})
    return returning
