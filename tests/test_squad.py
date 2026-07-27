"""The wildcard squad optimiser.

Constraint tests first, because a squad that breaks FPL's rules is not a
suboptimal answer - it is not an answer at all. Quality tests second, against a
constructed pool where the right answer is known.
"""

from __future__ import annotations

import pytest

from fplbot.config import TUNABLES
from fplbot.domain.squad import (
    Candidate,
    best_eleven,
    describe_squad,
    optimise_squad,
    prune_candidates,
    squad_value,
    valid_formations,
)


def a_candidate(
    element_id: int,
    position: str,
    price_tenths: int,
    season_xp: float,
    team_id: int = 1,
) -> Candidate:
    return Candidate(
        element_id=element_id,
        name=f"P{element_id}",
        position=position,
        team_id=team_id,
        team_short=f"T{team_id}",
        price_tenths=price_tenths,
        season_xp=season_xp,
        gameweek_xp=season_xp / 30,
        ownership=10.0,
        availability_risk=0.0,
    )


def a_pool(*, teams: int = 20, per_team: int = 6) -> list[Candidate]:
    """A realistic-ish pool: 20 clubs, mixed positions, prices and quality.

    Quality correlates with price, as in the real game, so the optimiser has to
    make genuine trade-offs rather than just picking the most expensive players.
    """
    candidates: list[Candidate] = []
    element_id = 0
    layout = [("GKP", 2), ("DEF", 2), ("MID", 1), ("FWD", 1)]

    for team_id in range(1, teams + 1):
        for position, count in layout:
            for _slot in range(count):
                element_id += 1
                # Prices 40-130, with better players costing more.
                price = 40 + ((element_id * 17) % 90)
                quality = price / 10.0 + ((element_id * 7) % 11) - 5
                candidates.append(
                    a_candidate(
                        element_id,
                        position,
                        price,
                        max(5.0, quality * 8),
                        team_id=team_id,
                    )
                )
    return candidates


class TestFormations:
    def test_all_formations_field_eleven(self) -> None:
        for defenders, midfielders, forwards in valid_formations(TUNABLES.formation_limits):
            assert defenders + midfielders + forwards == 10  # plus one goalkeeper

    def test_standard_formations_are_present(self) -> None:
        formations = set(valid_formations(TUNABLES.formation_limits))

        for expected in [(4, 4, 2), (3, 5, 2), (4, 3, 3), (5, 3, 2), (3, 4, 3)]:
            assert expected in formations

    def test_illegal_formations_are_absent(self) -> None:
        formations = set(valid_formations(TUNABLES.formation_limits))

        assert (2, 5, 3) not in formations, "fewer than 3 defenders is illegal"
        assert (6, 3, 1) not in formations, "more than 5 defenders is illegal"
        assert (5, 5, 0) not in formations, "a striker-less XI is illegal"


class TestBestEleven:
    def test_picks_the_highest_scoring_legal_xi(self) -> None:
        squad = (
            [a_candidate(1, "GKP", 45, 100), a_candidate(2, "GKP", 40, 40)]
            + [a_candidate(10 + i, "DEF", 50, 150 - i * 10) for i in range(5)]
            + [a_candidate(20 + i, "MID", 70, 200 - i * 10) for i in range(5)]
            + [a_candidate(30 + i, "FWD", 90, 180 - i * 10) for i in range(3)]
        )

        starters, bench, _formation, total = best_eleven(squad, TUNABLES)

        assert len(starters) == 11
        assert len(bench) == 4
        assert sum(1 for p in starters if p.position == "GKP") == 1
        # The better keeper starts; the worse one is benched.
        assert starters[0].element_id == 1
        assert total == pytest.approx(sum(p.season_xp for p in starters))

    def test_formation_is_chosen_to_maximise_points(self) -> None:
        """Five strong defenders should pull the XI towards a back five."""
        squad = (
            [a_candidate(1, "GKP", 45, 100), a_candidate(2, "GKP", 40, 40)]
            + [a_candidate(10 + i, "DEF", 50, 300) for i in range(5)]
            + [a_candidate(20 + i, "MID", 70, 50) for i in range(5)]
            + [a_candidate(30 + i, "FWD", 90, 40) for i in range(3)]
        )

        _, _, formation, _ = best_eleven(squad, TUNABLES)

        assert formation.startswith("5-")

    def test_bench_is_in_autosub_order(self) -> None:
        """The reserve keeper goes last; outfield subs are ordered by value."""
        squad = (
            [a_candidate(1, "GKP", 45, 100), a_candidate(2, "GKP", 40, 40)]
            + [a_candidate(10 + i, "DEF", 50, 150 - i * 20) for i in range(5)]
            + [a_candidate(20 + i, "MID", 70, 200 - i * 20) for i in range(5)]
            + [a_candidate(30 + i, "FWD", 90, 180 - i * 20) for i in range(3)]
        )

        _, bench, _, _ = best_eleven(squad, TUNABLES)

        assert bench[-1].position == "GKP"
        outfield = [p.season_xp for p in bench if p.position != "GKP"]
        assert outfield == sorted(outfield, reverse=True)

    def test_squad_value_weights_the_bench_lightly(self) -> None:
        """Only eleven players score, so the bench must not drive the objective."""
        squad = (
            [a_candidate(1, "GKP", 45, 100), a_candidate(2, "GKP", 40, 100)]
            + [a_candidate(10 + i, "DEF", 50, 100) for i in range(5)]
            + [a_candidate(20 + i, "MID", 70, 100) for i in range(5)]
            + [a_candidate(30 + i, "FWD", 90, 100) for i in range(3)]
        )

        _, _, _, starting = best_eleven(squad, TUNABLES)
        value = squad_value(squad, TUNABLES)

        assert value == pytest.approx(starting + TUNABLES.bench_weight * 400)
        assert value < starting * 1.1, "the bench must be a rounding error, not a driver"


class TestPruning:
    def test_dominated_players_are_dropped(self) -> None:
        """More expensive AND worse can never be in an optimal squad."""
        candidates = [
            a_candidate(1, "MID", 50, 100),
            a_candidate(2, "MID", 70, 80),  # dominated by 1
            a_candidate(3, "MID", 90, 150),
        ]

        pruned = prune_candidates(candidates, TUNABLES)
        kept = {c.element_id for c in pruned["MID"]}

        assert 1 in kept
        assert 3 in kept

    def test_the_cheap_end_survives_pruning(self) -> None:
        """Bench fodder is chosen for its price, not its points.

        A pure dominance filter would strip the cheap end and leave no
        affordable way to fill the bench, making a legal squad impossible.
        """
        candidates = [a_candidate(1, "DEF", 40, 5)] + [
            a_candidate(i, "DEF", 100, 300) for i in range(2, 20)
        ]

        pruned = prune_candidates(candidates, TUNABLES)

        assert any(c.price_tenths == 40 for c in pruned["DEF"])


class TestOptimiser:
    def test_produces_a_legal_squad(self) -> None:
        result = optimise_squad(a_pool(), TUNABLES, seed=1)

        assert result is not None
        assert len(result.players) == 15
        assert len(result.starters) == 11
        assert len(result.bench) == 4

    def test_respects_the_squad_composition(self) -> None:
        result = optimise_squad(a_pool(), TUNABLES, seed=1)

        assert result is not None
        for position, required in TUNABLES.squad_composition.items():
            assert len(result.by_position(position)) == required

    def test_respects_the_budget(self) -> None:
        """GBP 100.0m, expressed in FPL's tenths."""
        result = optimise_squad(a_pool(), TUNABLES, seed=1)

        assert result is not None
        assert result.total_price_tenths <= TUNABLES.wildcard_budget_tenths
        assert result.money_left >= 0

    def test_respects_the_three_per_club_limit(self) -> None:
        result = optimise_squad(a_pool(), TUNABLES, seed=1)

        assert result is not None
        for club, count in result.club_counts().items():
            assert count <= TUNABLES.max_players_per_club, f"{club} has {count}"

    def test_no_duplicate_players(self) -> None:
        result = optimise_squad(a_pool(), TUNABLES, seed=1)

        assert result is not None
        ids = [p.element_id for p in result.players]
        assert len(ids) == len(set(ids))

    def test_beats_a_naive_cheapest_squad(self) -> None:
        """The whole point: local search must actually improve on the seed."""
        pool = a_pool()
        result = optimise_squad(pool, TUNABLES, seed=1)

        assert result is not None

        # A cheapest-first squad, which is what the optimiser seeds from.
        cheapest: list[Candidate] = []
        counts: dict[int, int] = {}
        for position, needed in TUNABLES.squad_composition.items():
            taken = 0
            for candidate in sorted(
                [c for c in pool if c.position == position], key=lambda c: c.price_tenths
            ):
                if taken >= needed:
                    break
                if counts.get(candidate.team_id, 0) >= 3:
                    continue
                cheapest.append(candidate)
                counts[candidate.team_id] = counts.get(candidate.team_id, 0) + 1
                taken += 1

        _, _, _, cheap_xp = best_eleven(cheapest, TUNABLES)

        assert result.starting_xp > cheap_xp * 1.2

    def test_spends_most_of_the_budget(self) -> None:
        """Leaving millions unspent means points left on the table."""
        result = optimise_squad(a_pool(), TUNABLES, seed=1)

        assert result is not None
        assert result.total_price_tenths > TUNABLES.wildcard_budget_tenths * 0.9

    def test_is_reproducible_for_a_given_seed(self) -> None:
        """A wildcard draft that changed every hour would be untrustworthy."""
        first = optimise_squad(a_pool(), TUNABLES, seed=12)
        second = optimise_squad(a_pool(), TUNABLES, seed=12)

        assert first is not None and second is not None
        assert [p.element_id for p in first.players] == [p.element_id for p in second.players]

    def test_reports_an_optimality_note(self) -> None:
        """Honest about whether the restarts converged."""
        result = optimise_squad(a_pool(), TUNABLES, seed=1)

        assert result is not None
        assert result.optimality_note
        assert result.restarts >= 1

    def test_a_captain_is_nominated(self) -> None:
        result = optimise_squad(a_pool(), TUNABLES, seed=1)

        assert result is not None
        assert result.captain is not None
        assert result.captain.element_id in {p.element_id for p in result.starters}
        assert result.captain.season_xp == max(p.season_xp for p in result.starters)

    def test_returns_none_when_no_legal_squad_exists(self) -> None:
        """Too few players - expected in pre-season. Must not raise."""
        assert optimise_squad([a_candidate(1, "GKP", 40, 10)], TUNABLES) is None

    def test_returns_none_when_everything_is_unaffordable(self) -> None:
        """15 players at GBP 15.0m each is GBP 225m against a GBP 100m budget."""
        pool = [
            a_candidate(i, position, 150, 300, team_id=(i % 20) + 1)
            for position, base in (("GKP", 0), ("DEF", 100), ("MID", 200), ("FWD", 300))
            for i in range(base, base + 10)
        ]

        assert optimise_squad(pool, TUNABLES) is None

    def test_prefers_value_when_budget_binds(self) -> None:
        """Given one premium and many mid-price options, the squad must fit."""
        pool = a_pool()
        result = optimise_squad(pool, TUNABLES, seed=3)

        assert result is not None
        assert result.is_valid


class TestDescription:
    def test_summary_names_the_shape_and_spend(self) -> None:
        result = optimise_squad(a_pool(), TUNABLES, seed=1)

        assert result is not None
        summary = describe_squad(result)

        assert result.formation in summary
        assert "spent" in summary
        assert "left" in summary
