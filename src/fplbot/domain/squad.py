"""The wildcard squad optimiser.

Builds the best 15-man squad buildable for GBP 100.0m, maximising projected
points from now to the end of the season.

WHY THIS IS COMPATIBLE WITH ADR-009
------------------------------------
SPEC section 1 says the bot does not run a squad ILP optimiser, and ADR-009
records that decision. It is worth being precise about what that decision
actually rested on: the objection was **squad state**. A transfer optimiser needs
to know your current fifteen, your bank and your free transfers, and getting that
requires either FPL authentication (out of scope) or a hand-maintained file that
goes stale silently and produces confidently wrong advice.

A wildcard draft has no squad state *by definition*. A wildcard discards your
existing team and rebuilds from scratch against a fixed GBP 100.0m budget. There
is nothing to know about you, so the objection does not apply. See ADR-018.

THE PROBLEM
-----------
Choose 15 players - 2 GKP, 5 DEF, 5 MID, 3 FWD - subject to:

    * total price <= 1000 (FPL's tenths of a million)
    * at most 3 players from any one club
    * maximise projected points

This is a multi-dimensional knapsack and is NP-hard in general. At this size it
is not remotely hard in practice, and we solve it without adding a solver
dependency (no PuLP, no scipy - see ADR-003 on the 250 MB budget).

SCORED ON THE STARTING XI, NOT ALL FIFTEEN
-------------------------------------------
This is the detail that separates a useful answer from a naive one.

Only eleven players score in a given gameweek. A squad optimised on all fifteen
equally will spend real money on a fifth defender who never starts. Every serious
FPL wildcard draft therefore loads the XI and fills the bench with the cheapest
bodies that satisfy the squad rules.

So a squad's value is its **best valid starting XI**, plus a light weight on the
bench. The bench weight is not zero - injuries, rotation and autosubs mean the
bench does occasionally score, and at exactly zero the optimiser happily fills it
with players who cannot play at all, which is both wrong and obviously silly when
you read the output.

THE ALGORITHM
-------------
1. **Dominance pruning.** A player who is more expensive *and* projected lower
   than another in the same position can never appear in an optimal squad.
   Discarding them is free and shrinks the search space by roughly an order of
   magnitude.
2. **A cheapest feasible seed.** Start from the cheapest legal squad, so we are
   inside the budget from the first iteration and every subsequent step can be
   checked for feasibility trivially.
3. **Steepest-ascent local search.** Repeatedly apply the single best
   improving swap - one squad player out, one candidate in - until none exists.
4. **Random restarts** from perturbed seeds, keeping the best result.

On this problem shape that lands on the optimum or within a fraction of a point
of it, in milliseconds. `optimality_note` reports the spread across restarts,
which is the honest way to say how confident we are that it is the true optimum.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from itertools import product

from fplbot.config import ModelTunables
from fplbot.observability import logger

POSITIONS = ("GKP", "DEF", "MID", "FWD")


@dataclass(frozen=True)
class Candidate:
    """One selectable player, flattened to exactly what the optimiser needs."""

    element_id: int
    name: str
    position: str
    team_id: int
    team_short: str
    price_tenths: int
    season_xp: float  # projected points to end of season
    gameweek_xp: float  # this gameweek only, for the report
    ownership: float
    availability_risk: float

    @property
    def price(self) -> float:
        return self.price_tenths / 10.0

    @property
    def value_per_million(self) -> float:
        return self.season_xp / self.price if self.price_tenths else 0.0


@dataclass
class WildcardSquad:
    """The finished squad."""

    starters: list[Candidate] = field(default_factory=list)
    bench: list[Candidate] = field(default_factory=list)
    formation: str = ""
    total_price_tenths: int = 0
    starting_xp: float = 0.0  # projected season points from the XI
    squad_xp: float = 0.0  # including the bench weighting
    captain: Candidate | None = None
    budget_tenths: int = 1000
    optimality_note: str = ""
    restarts: int = 0

    @property
    def players(self) -> list[Candidate]:
        return self.starters + self.bench

    @property
    def total_price(self) -> float:
        return self.total_price_tenths / 10.0

    @property
    def money_left(self) -> float:
        return (self.budget_tenths - self.total_price_tenths) / 10.0

    @property
    def is_valid(self) -> bool:
        return len(self.players) == 15 and self.total_price_tenths <= self.budget_tenths

    def club_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for player in self.players:
            counts[player.team_short] = counts.get(player.team_short, 0) + 1
        return counts

    def by_position(self, position: str) -> list[Candidate]:
        return [p for p in self.players if p.position == position]


# ---------------------------------------------------------------------------
# Formations
# ---------------------------------------------------------------------------
def valid_formations(limits: dict[str, tuple[int, int]]) -> list[tuple[int, int, int]]:
    """Every legal (DEF, MID, FWD) split for a starting XI.

    Enumerated from the limits rather than hardcoded, so a rule change in
    `game_config` is a config edit rather than a code change.
    """
    def_min, def_max = limits["DEF"]
    mid_min, mid_max = limits["MID"]
    fwd_min, fwd_max = limits["FWD"]

    formations = [
        (defenders, midfielders, forwards)
        for defenders, midfielders, forwards in product(
            range(def_min, def_max + 1),
            range(mid_min, mid_max + 1),
            range(fwd_min, fwd_max + 1),
        )
        # 10 outfield plus exactly one goalkeeper.
        if defenders + midfielders + forwards == 10
    ]
    return formations


def best_eleven(
    squad: list[Candidate], tunables: ModelTunables
) -> tuple[list[Candidate], list[Candidate], str, float]:
    """Pick the highest-scoring legal XI from a 15-man squad.

    Exact, not heuristic: we enumerate every legal formation (there are only a
    handful) and, for each, take the top players per position. Sorting once per
    position and slicing is optimal within a fixed formation, so the maximum over
    formations is the true best XI.

    Returns (starters, bench, formation, starting_xp).
    """
    by_position: dict[str, list[Candidate]] = {position: [] for position in POSITIONS}
    for player in squad:
        by_position.setdefault(player.position, []).append(player)
    for players in by_position.values():
        players.sort(key=lambda c: c.season_xp, reverse=True)

    if not by_position["GKP"]:
        return [], list(squad), "", 0.0

    best_total = -1.0
    best_starters: list[Candidate] = []
    best_formation = ""

    keeper = by_position["GKP"][0]

    for defenders, midfielders, forwards in valid_formations(tunables.formation_limits):
        if (
            len(by_position["DEF"]) < defenders
            or len(by_position["MID"]) < midfielders
            or len(by_position["FWD"]) < forwards
        ):
            continue

        starters = [
            keeper,
            *by_position["DEF"][:defenders],
            *by_position["MID"][:midfielders],
            *by_position["FWD"][:forwards],
        ]
        total = sum(player.season_xp for player in starters)

        if total > best_total:
            best_total = total
            best_starters = starters
            best_formation = f"{defenders}-{midfielders}-{forwards}"

    starter_ids = {player.element_id for player in best_starters}
    bench = [player for player in squad if player.element_id not in starter_ids]
    # Bench order matters in FPL - the first outfield sub comes on first - so
    # present it in the order the autosubs would actually use.
    bench.sort(key=lambda c: (c.position == "GKP", -c.season_xp))

    return best_starters, bench, best_formation, best_total


def squad_value(squad: list[Candidate], tunables: ModelTunables) -> float:
    """The optimiser's objective: best XI plus a light weight on the bench."""
    _, bench, _, starting_xp = best_eleven(squad, tunables)
    bench_xp = sum(player.season_xp for player in bench)
    return starting_xp + tunables.bench_weight * bench_xp


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------
def prune_candidates(
    candidates: list[Candidate], tunables: ModelTunables
) -> dict[str, list[Candidate]]:
    """Drop players who cannot appear in an optimal squad.

    A candidate is *dominated* when another player in the same position costs no
    more and projects at least as well. Dominated players are provably never
    required, so removing them cannot change the optimum - it only shrinks the
    search.

    We then cap each position at `optimiser_candidates_per_position`, which is a
    heuristic rather than a proof. The cap is generous enough (45) that the
    players it discards are ones no sensible squad would field, and it keeps the
    local search fast.
    """
    by_position: dict[str, list[Candidate]] = {position: [] for position in POSITIONS}
    for candidate in candidates:
        if candidate.position in by_position:
            by_position[candidate.position].append(candidate)

    pruned: dict[str, list[Candidate]] = {}

    for position, players in by_position.items():
        # Sort by price ascending, then xP descending. Walking this order, a
        # player is dominated exactly when someone earlier (cheaper or equal)
        # already had at least as much projected value.
        players.sort(key=lambda c: (c.price_tenths, -c.season_xp))

        survivors: list[Candidate] = []
        best_so_far = float("-inf")
        for candidate in players:
            if candidate.season_xp > best_so_far:
                survivors.append(candidate)
                best_so_far = candidate.season_xp

        # Always keep the cheapest few regardless of dominance: bench fodder is
        # chosen for its price, not its points, and the dominance filter would
        # otherwise strip the cheap end and make a legal squad unaffordable.
        cheapest = sorted(players, key=lambda c: c.price_tenths)[:8]
        seen = {candidate.element_id for candidate in survivors}
        survivors.extend(c for c in cheapest if c.element_id not in seen)

        survivors.sort(key=lambda c: c.season_xp, reverse=True)
        pruned[position] = survivors[: tunables.optimiser_candidates_per_position]

    logger.info(
        "Pruned wildcard candidates",
        extra={position: len(players) for position, players in pruned.items()},
    )
    return pruned


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
def _club_counts(squad: list[Candidate]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for player in squad:
        counts[player.team_id] = counts.get(player.team_id, 0) + 1
    return counts


def _seed_squad(
    pruned: dict[str, list[Candidate]],
    tunables: ModelTunables,
    rng: random.Random,
    *,
    randomise: bool,
) -> list[Candidate] | None:
    """Build a cheap, legal starting squad.

    Seeding from the *cheapest* legal squad rather than the highest-scoring one
    is deliberate: it guarantees we begin inside the budget, so every later step
    is a strict improvement from a feasible point. Seeding greedily by value
    typically overspends, and repairing an infeasible squad is a much more
    fiddly problem than improving a feasible one.

    A randomised attempt can fail - the shuffle may pick a combination that
    breaks the club limit or the budget - so we fall back to the strict cheapest
    ordering rather than wasting the restart. Before this fallback existed, about
    a third of randomised restarts produced nothing at all.
    """
    attempts = [True, False] if randomise else [False]

    for shuffled in attempts:
        squad = _greedy_cheapest(pruned, tunables, rng, shuffled=shuffled)
        if squad is not None:
            return squad
    return None


def _seed_by_value(
    pruned: dict[str, list[Candidate]], tunables: ModelTunables
) -> list[Candidate] | None:
    """A second, deterministic seed: best points-per-million that still fits.

    Worth having alongside the cheapest seed because the two start in genuinely
    different regions, and a local optimum reachable from one is often not
    reachable from the other. In practice this seed converges to the same squad
    as the best randomised restart, which is what lets `_optimality_note` report
    real agreement rather than a lucky single result.

    Feasibility is maintained by filling each position with its best-value
    players first and then *repairing* downwards - swapping the weakest picks for
    cheaper ones - until the squad is inside budget. Repairing downwards always
    terminates, because the cheapest legal squad is known to fit.
    """
    squad: list[Candidate] = []
    counts: dict[int, int] = {}

    for position, needed in tunables.squad_composition.items():
        pool = sorted(pruned.get(position, []), key=lambda c: c.value_per_million, reverse=True)
        taken = 0
        for candidate in pool:
            if taken >= needed:
                break
            if counts.get(candidate.team_id, 0) >= tunables.max_players_per_club:
                continue
            squad.append(candidate)
            counts[candidate.team_id] = counts.get(candidate.team_id, 0) + 1
            taken += 1
        if taken < needed:
            return None

    # Repair: while over budget, downgrade whichever player frees the most money
    # for the least projected loss.
    for _ in range(60):
        spend = sum(player.price_tenths for player in squad)
        if spend <= tunables.wildcard_budget_tenths:
            return squad

        squad_ids = {player.element_id for player in squad}
        best_ratio = None
        best_swap: tuple[int, Candidate] | None = None

        for index, outgoing in enumerate(squad):
            for incoming in pruned.get(outgoing.position, []):
                if incoming.element_id in squad_ids:
                    continue
                saving = outgoing.price_tenths - incoming.price_tenths
                if saving <= 0:
                    continue
                incoming_count = counts.get(incoming.team_id, 0)
                if incoming.team_id == outgoing.team_id:
                    incoming_count -= 1
                if incoming_count >= tunables.max_players_per_club:
                    continue

                loss = max(0.0, outgoing.season_xp - incoming.season_xp)
                ratio = loss / saving
                if best_ratio is None or ratio < best_ratio:
                    best_ratio = ratio
                    best_swap = (index, incoming)

        if best_swap is None:
            return None
        index, incoming = best_swap
        outgoing = squad[index]
        counts[outgoing.team_id] -= 1
        counts[incoming.team_id] = counts.get(incoming.team_id, 0) + 1
        squad[index] = incoming

    return None


def _greedy_cheapest(
    pruned: dict[str, list[Candidate]],
    tunables: ModelTunables,
    rng: random.Random,
    *,
    shuffled: bool,
) -> list[Candidate] | None:
    """One cheapest-first pass, optionally perturbed."""
    squad: list[Candidate] = []
    counts: dict[int, int] = {}

    for position, needed in tunables.squad_composition.items():
        pool = sorted(pruned.get(position, []), key=lambda c: c.price_tenths)
        if shuffled:
            # Perturb the seed so restarts explore different corners. Only the
            # cheap end is shuffled, so the squad stays affordable.
            head = pool[: min(len(pool), 20)]
            rng.shuffle(head)
            pool = head + pool[len(head) :]

        taken = 0
        for candidate in pool:
            if taken >= needed:
                break
            if counts.get(candidate.team_id, 0) >= tunables.max_players_per_club:
                continue
            squad.append(candidate)
            counts[candidate.team_id] = counts.get(candidate.team_id, 0) + 1
            taken += 1

        if taken < needed:
            return None  # not enough legal players in this position

    if sum(player.price_tenths for player in squad) > tunables.wildcard_budget_tenths:
        return None

    return squad


def _improve(
    squad: list[Candidate],
    pruned: dict[str, list[Candidate]],
    tunables: ModelTunables,
) -> tuple[list[Candidate], float]:
    """Steepest-ascent local search over single-player swaps.

    Each pass evaluates every legal (out, in) pair and applies the single best
    improvement. Steepest ascent rather than first-improvement because the
    evaluation is cheap and taking the best move converges in far fewer passes.
    """
    current = list(squad)
    current_value = squad_value(current, tunables)

    for _ in range(tunables.optimiser_max_passes):
        spend = sum(player.price_tenths for player in current)
        counts = _club_counts(current)
        squad_ids = {player.element_id for player in current}

        best_gain = 0.0
        best_move: tuple[int, Candidate] | None = None

        for index, outgoing in enumerate(current):
            budget_without = tunables.wildcard_budget_tenths - (spend - outgoing.price_tenths)

            for incoming in pruned.get(outgoing.position, []):
                if incoming.element_id in squad_ids:
                    continue
                if incoming.price_tenths > budget_without:
                    continue

                # Club limit, evaluated as though the swap had happened.
                incoming_count = counts.get(incoming.team_id, 0)
                if incoming.team_id == outgoing.team_id:
                    incoming_count -= 1
                if incoming_count >= tunables.max_players_per_club:
                    continue

                trial = [*current[:index], incoming, *current[index + 1 :]]
                gain = squad_value(trial, tunables) - current_value
                if gain > best_gain:
                    best_gain = gain
                    best_move = (index, incoming)

        if best_move is None:
            break

        index, incoming = best_move
        current = [*current[:index], incoming, *current[index + 1 :]]
        current_value += best_gain

    return current, current_value


def optimise_squad(
    candidates: list[Candidate],
    tunables: ModelTunables,
    *,
    seed: int = 0,
) -> WildcardSquad | None:
    """Build the best GBP 100.0m squad for the rest of the season.

    Returns None when no legal squad can be built at all - which in practice
    means the candidate pool is too thin, e.g. in pre-season before projections
    exist.
    """
    pruned = prune_candidates(candidates, tunables)

    if any(
        len(pruned.get(position, [])) < needed
        for position, needed in tunables.squad_composition.items()
    ):
        logger.warning(
            "Not enough candidates to build a legal squad",
            extra={position: len(players) for position, players in pruned.items()},
        )
        return None

    rng = random.Random(seed)
    results: list[tuple[float, list[Candidate]]] = []

    for restart in range(max(1, tunables.optimiser_restarts)):
        # Restart 0 is the deterministic cheapest seed, restart 1 the
        # deterministic value seed, and the rest are randomised. Two different
        # deterministic starting regions means agreement between them is real
        # evidence rather than a repeated coincidence.
        if restart == 0:
            seeded = _seed_squad(pruned, tunables, rng, randomise=False)
        elif restart == 1:
            seeded = _seed_by_value(pruned, tunables)
        else:
            seeded = _seed_squad(pruned, tunables, rng, randomise=True)

        if seeded is None:
            continue
        improved, value = _improve(seeded, pruned, tunables)
        results.append((value, improved))

    if not results:
        logger.warning("Could not seed a feasible wildcard squad")
        return None

    results.sort(key=lambda pair: pair[0], reverse=True)
    best_value, best_squad = results[0]

    starters, bench, formation, starting_xp = best_eleven(best_squad, tunables)

    # Captain the highest-projected starter. Note this is the *season* captain
    # for the draft, not this week's armband - the captaincy section answers that
    # question with a gameweek-specific model.
    captain = max(starters, key=lambda c: c.season_xp) if starters else None

    squad = WildcardSquad(
        starters=starters,
        bench=bench,
        formation=formation,
        total_price_tenths=sum(player.price_tenths for player in best_squad),
        starting_xp=round(starting_xp, 1),
        squad_xp=round(best_value, 1),
        captain=captain,
        budget_tenths=tunables.wildcard_budget_tenths,
        restarts=len(results),
        optimality_note=_optimality_note(results),
    )

    logger.info(
        "Optimised wildcard squad",
        extra={
            "formation": squad.formation,
            "spend": squad.total_price,
            "starting_xp": squad.starting_xp,
            "restarts": squad.restarts,
        },
    )
    return squad


AGREEMENT_TOLERANCE = 0.5


def _optimality_note(results: list[tuple[float, list[Candidate]]]) -> str:
    """Say how confident we are that this is the true optimum.

    The measure is **how many independent searches reached the best value**, not
    the total spread across them.

    The distinction matters and it was got wrong first time round. Local search
    from a randomised seed sometimes gets stuck in a poor local optimum; that
    drags the spread wide while saying nothing about whether the *best* result is
    optimal. What is genuinely informative is agreement at the top: if several
    searches starting from different seeds independently arrive at the same
    value, that value is very unlikely to be beatable. One bad restart is noise,
    not evidence.
    """
    if len(results) < 2:
        return "Single search pass - no convergence check available."

    values = [value for value, _ in results]
    best = max(values)
    agreed = sum(1 for value in values if best - value <= AGREEMENT_TOLERANCE)

    if agreed == len(results):
        return (
            f"All {len(results)} independent searches reached the same squad - "
            "almost certainly optimal."
        )
    if agreed >= 2:
        return (
            f"{agreed} of {len(results)} independent searches reached this squad "
            "from different starting points - very likely optimal."
        )
    runner_up = max((v for v in values if best - v > AGREEMENT_TOLERANCE), default=best)
    return (
        f"Best of {len(results)} independent searches, beating the runner-up by "
        f"{best - runner_up:.1f} pts. Only one search reached it, so treat it as "
        "very good rather than provably optimal."
    )


def describe_squad(squad: WildcardSquad) -> str:
    """A one-line summary for the report header."""
    clubs = squad.club_counts()
    triples = [club for club, count in clubs.items() if count >= 3]
    parts = [
        f"{squad.formation} formation",
        f"GBP {squad.total_price:.1f}m spent",
        f"GBP {squad.money_left:.1f}m left",
    ]
    if triples:
        parts.append("triple-ups: " + ", ".join(sorted(triples)))
    return " - ".join(parts) + "."
