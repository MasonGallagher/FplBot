"""The rank-attacking objective.

The property that matters most: a template player and a differential with the
same expected points must NOT rank equally. If they do, the objective has
collapsed back to expected points and the whole premise is gone.
"""

from __future__ import annotations

import numpy as np
import pytest

from fplbot.config import TUNABLES
from fplbot.domain.ranking import (
    assess_confidence,
    build_buy_board,
    build_sell_list,
    build_watchlist,
    explain,
    rank_score,
    warnings_for,
)
from fplbot.models.domain import (
    AvailabilitySignal,
    Confidence,
    Distribution,
    FixtureKind,
    PlayerScore,
    RiskLevel,
)


def a_score(
    element_id: int = 1,
    *,
    mean: float = 6.0,
    spread: float = 3.0,
    ownership: float = 10.0,
    price: float = 8.0,
    position: str = "MID",
    fixture_kind: FixtureKind = FixtureKind.NORMAL,
    availability: AvailabilitySignal | None = None,
    components: dict | None = None,
    minutes_played: int = 2000,
) -> PlayerScore:
    rng = np.random.default_rng(element_id)
    samples = np.clip(rng.normal(mean, spread, 3000), 0, None)
    # Recentre so the requested mean survives the clipping.
    samples = samples * (mean / max(samples.mean(), 1e-9))

    return PlayerScore(
        element_id=element_id,
        name=f"Player{element_id}",
        team_short="ARS",
        team_id=1,
        position=position,
        minutes_played=minutes_played,
        price=price,
        ownership=ownership,
        distribution=Distribution(samples=samples),
        availability=availability or AvailabilitySignal(element_id=element_id, risk=0.0),
        fixture_kind=fixture_kind,
        fixture_count=0 if fixture_kind is FixtureKind.BLANK else 1,
        components=components or {"goals": 2.0, "assists": 1.0, "clean_sheet": 0.5},
        opponents=["CHE(H)"],
        ep_next=mean * 0.9,
    )


class TestObjective:
    def test_the_differential_beats_the_template_at_equal_xp(self) -> None:
        """THE core claim.

        A 60%-owned player's points wash out against the field - his value is
        largely defensive. A 4%-owned player with the same expectation accrues
        almost entirely to you.
        """
        template = a_score(1, mean=6.0, ownership=60.0)
        differential = a_score(2, mean=6.0, ownership=4.0)

        assert rank_score(differential, TUNABLES) > rank_score(template, TUNABLES)

    def test_ceiling_is_rewarded(self) -> None:
        """Rank is gained in the tail, not in the mean."""
        steady = a_score(1, mean=6.0, spread=1.0, ownership=10.0)
        explosive = a_score(2, mean=6.0, spread=6.0, ownership=10.0)

        assert explosive.ceiling > steady.ceiling
        assert rank_score(explosive, TUNABLES) > rank_score(steady, TUNABLES)

    def test_a_big_enough_xp_edge_still_wins(self) -> None:
        """The objective tilts towards differentials; it does not ignore quality."""
        template = a_score(1, mean=12.0, ownership=60.0)
        differential = a_score(2, mean=3.0, ownership=2.0)

        assert rank_score(template, TUNABLES) > rank_score(differential, TUNABLES)

    def test_ownership_of_zero_is_unpenalised(self) -> None:
        unowned = a_score(1, mean=6.0, ownership=0.0)

        expected = 6.0 + TUNABLES.ceiling_bonus_mu * unowned.ceiling
        assert rank_score(unowned, TUNABLES) == pytest.approx(expected, rel=1e-6)


class TestBuyBoard:
    def test_ranked_within_each_position(self) -> None:
        """Squads have positional slots, so that is the axis on which the choice
        is real. Comparing a 4.0m defender with a 14.0m forward on raw xP is not
        a decision anyone actually makes."""
        scores = [
            a_score(1, mean=8.0, position="MID"),
            a_score(2, mean=5.0, position="MID"),
            a_score(3, mean=7.0, position="DEF", price=5.0),
        ]

        board = build_buy_board(scores, TUNABLES, per_position=5)

        assert [rec.element_id for rec in board["MID"]] == [1, 2]
        assert [rec.element_id for rec in board["DEF"]] == [3]

    def test_blanks_are_filtered_out(self) -> None:
        scores = [
            a_score(1, mean=8.0, fixture_kind=FixtureKind.BLANK),
            a_score(2, mean=4.0),
        ]

        board = build_buy_board(scores, TUNABLES)

        assert [rec.element_id for rec in board["MID"]] == [2]

    def test_effectively_ruled_out_players_are_filtered(self) -> None:
        scores = [
            a_score(1, mean=9.0, availability=AvailabilitySignal(element_id=1, risk=0.99)),
            a_score(2, mean=4.0),
        ]

        board = build_buy_board(scores, TUNABLES)

        assert [rec.element_id for rec in board["MID"]] == [2]

    def test_every_recommendation_carries_a_why(self) -> None:
        """SPEC 6.1: a pick with no 'why' is one the user cannot argue with."""
        board = build_buy_board([a_score(1), a_score(2)], TUNABLES)

        for rec in board["MID"]:
            assert rec.why
            assert rec.why.endswith(".")
            assert rec.confidence in set(Confidence)

    def test_runner_up_is_within_a_comparable_price_band(self) -> None:
        """A runner-up you cannot afford is a different decision, not an alternative."""
        scores = [
            a_score(1, mean=8.0, price=8.0),
            a_score(2, mean=7.5, price=13.0),  # too expensive to substitute
            a_score(3, mean=7.0, price=8.5),  # the real alternative
        ]

        board = build_buy_board(scores, TUNABLES, per_position=1)

        assert board["MID"][0].runner_up is not None
        assert "Player3" in board["MID"][0].runner_up


class TestConfidence:
    def test_awaiting_a_press_conference_is_low(self) -> None:
        score = a_score(
            1,
            availability=AvailabilitySignal(
                element_id=1, risk=0.1, injury_condition="Currently Being Assessed"
            ),
        )

        assert assess_confidence(score) is Confidence.LOW

    def test_conflicting_sources_are_low(self) -> None:
        signal = AvailabilitySignal(element_id=1, risk=0.2)
        signal.conflicting_sources.append("premierinjuries")

        assert assess_confidence(a_score(1, availability=signal)) is Confidence.LOW

    def test_corroborated_and_clear_is_high(self) -> None:
        signal = AvailabilitySignal(element_id=1, risk=0.0, level=RiskLevel.CLEAR)
        signal.corroborating_sources.extend(["premierinjuries", "ffs"])

        assert assess_confidence(a_score(1, spread=1.0, availability=signal)) is Confidence.HIGH

    def test_a_provisional_fixture_is_low(self) -> None:
        """The fixture could move gameweek, turning the pick into a blank."""
        score = a_score(1, fixture_kind=FixtureKind.PROVISIONAL)

        assert assess_confidence(score) is Confidence.LOW

    def test_confidence_is_independent_of_attractiveness(self) -> None:
        """Two separate axes, implying different actions.

        A superb differential with unclear fitness is 'wait for T-3h', not 'skip'.
        """
        great_but_uncertain = a_score(
            1,
            mean=12.0,
            availability=AvailabilitySignal(
                element_id=1, risk=0.1, injury_condition="Currently Being Assessed"
            ),
        )

        assert great_but_uncertain.mean > 10
        assert assess_confidence(great_but_uncertain) is Confidence.LOW


class TestExplanations:
    def test_leads_with_the_dominant_component(self) -> None:
        """Generated from the breakdown, so it cannot drift from the model."""
        score = a_score(1, components={"goals": 4.0, "clean_sheet": 0.1, "assists": 0.2})

        assert "goal threat" in explain(score, TUNABLES)

    def test_flags_a_differential(self) -> None:
        assert "rank gain" in explain(a_score(1, ownership=2.0), TUNABLES)

    def test_flags_a_template_player(self) -> None:
        assert "defensive value" in explain(a_score(1, ownership=55.0), TUNABLES)

    def test_flags_a_double(self) -> None:
        score = a_score(1, fixture_kind=FixtureKind.DOUBLE)
        score.fixture_count = 2

        assert "plays 2 times" in explain(score, TUNABLES)


class TestWarnings:
    def test_press_conference_warning(self) -> None:
        score = a_score(
            1,
            availability=AvailabilitySignal(
                element_id=1, risk=0.1, injury_condition="Currently Being Assessed"
            ),
        )

        assert any("T-3h" in warning for warning in warnings_for(score))

    def test_transfer_outflow_warning(self) -> None:
        score = a_score(
            1,
            availability=AvailabilitySignal(
                element_id=1, risk=0.2, flow_cause="bad_news", flow_zscore=-3.5
            ),
        )

        assert any("outflow" in warning for warning in warnings_for(score))


class TestSellList:
    def test_blanks_are_flagged(self) -> None:
        scores = [a_score(1, ownership=25.0, fixture_kind=FixtureKind.BLANK)]

        sells = build_sell_list(scores, TUNABLES)

        assert len(sells) == 1
        assert "blank" in sells[0].why.lower()

    def test_low_ownership_players_are_excluded(self) -> None:
        """A sell list is only useful for players a reader plausibly owns."""
        scores = [a_score(1, ownership=0.2, fixture_kind=FixtureKind.BLANK)]

        assert build_sell_list(scores, TUNABLES) == []

    def test_high_ownership_low_return_is_a_rank_liability(self) -> None:
        """Everyone else owns him, so the downside is shared but the opportunity
        cost is yours alone."""
        scores = [a_score(1, mean=1.5, ownership=45.0)]

        sells = build_sell_list(scores, TUNABLES)

        assert len(sells) == 1
        assert "holding costs rank" in sells[0].why.lower()

    def test_sorted_by_severity(self) -> None:
        scores = [
            a_score(
                1,
                ownership=25.0,
                mean=4.0,
                availability=AvailabilitySignal(element_id=1, risk=0.55),
            ),
            a_score(
                2,
                ownership=25.0,
                mean=4.0,
                fixture_kind=FixtureKind.BLANK,
                availability=AvailabilitySignal(element_id=2, risk=0.99, injury_reason="Suspended"),
            ),
        ]

        sells = build_sell_list(scores, TUNABLES)

        assert sells[0].element_id == 2


class TestWatchlist:
    def test_only_signals_fpl_has_not_published(self) -> None:
        """Once FPL publishes news the signal is no longer leading anything."""
        already_known = a_score(
            1,
            availability=AvailabilitySignal(
                element_id=1,
                risk=0.3,
                flow_cause="bad_news",
                flow_zscore=-3.5,
                fpl_news="Knock - 50% chance",
            ),
        )
        not_yet_known = a_score(
            2,
            availability=AvailabilitySignal(
                element_id=2, risk=0.0, flow_cause="bad_news", flow_zscore=-4.0
            ),
        )

        watchlist = build_watchlist([already_known, not_yet_known])

        assert [score.element_id for score in watchlist] == [2]

    def test_sorted_by_severity_of_the_anomaly(self) -> None:
        mild = a_score(
            1,
            availability=AvailabilitySignal(
                element_id=1, risk=0.0, flow_cause="bad_news", flow_zscore=-2.5
            ),
        )
        severe = a_score(
            2,
            availability=AvailabilitySignal(
                element_id=2, risk=0.0, flow_cause="bad_news", flow_zscore=-5.0
            ),
        )

        watchlist = build_watchlist([mild, severe])

        assert watchlist[0].element_id == 2


class TestDataSufficiency:
    """A player with no Premier League history is a guess wearing a name.

    Written after GW1 2026-27, where three forwards with ZERO PL minutes ranked
    3rd, 4th and 5th - above a proven 0.50 xG/90 forward on 2658 minutes - purely
    because almost nobody owned them. Their rates were the positional prior, so
    their distributions were narrow and confident-looking, and the ownership
    discount then rewarded them for being unknown.
    """

    def test_no_history_is_low_confidence(self) -> None:
        assert assess_confidence(a_score(minutes_played=0)) is Confidence.LOW

    def test_thin_history_is_medium_at_best(self) -> None:
        assert assess_confidence(a_score(minutes_played=600)) is not Confidence.HIGH

    def test_an_established_player_is_unaffected(self) -> None:
        """The check must not quietly downgrade everybody."""
        score = a_score(
            minutes_played=2500,
            availability=AvailabilitySignal(
                element_id=1, risk=0.0, corroborating_sources=["ffs", "premierinjuries"]
            ),
        )

        assert assess_confidence(score) is Confidence.HIGH


class TestDifferentialStake:
    """The ownership discount is a bet that our xP is right and the crowd's is
    wrong. It should be sized by how much we actually know."""

    def test_an_unknown_player_loses_his_differential_edge(self) -> None:
        """The correction that is easy to get backwards. Shrinking the PENALTY
        would raise an unowned player's score, since his penalty was tiny. What
        shrinks is his entitlement: effective ownership moves to the baseline."""
        score = a_score(mean=4.0, ownership=1.0, minutes_played=0)

        earned = rank_score(score, TUNABLES, Confidence.HIGH)
        unearned = rank_score(score, TUNABLES, Confidence.LOW)

        assert unearned < earned, "an unknown must not out-score a proven equal"

    def test_a_proven_player_is_scored_at_full_stake(self) -> None:
        score = a_score(mean=4.0, ownership=60.0, minutes_played=2500)

        assert rank_score(score, TUNABLES, Confidence.HIGH) == rank_score(score, TUNABLES)

    def test_two_equally_owned_players_are_split_on_evidence(self) -> None:
        """Same xP, same ownership: the one we actually know about must win."""
        proven = a_score(element_id=1, mean=3.6, ownership=5.0, minutes_played=2600)
        unknown = a_score(element_id=2, mean=3.6, ownership=5.0, minutes_played=0)

        assert rank_score(proven, TUNABLES, assess_confidence(proven)) > rank_score(
            unknown, TUNABLES, assess_confidence(unknown)
        )
