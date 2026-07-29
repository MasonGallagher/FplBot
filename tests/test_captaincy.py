"""Captain picks.

The property that matters most: captaincy must NOT rank the same way transfers
do. If it did, the objective has collapsed back into the transfer one and the
section is actively misleading - it would push differentials at the exact
decision where differentials are most punishing.
"""

from __future__ import annotations

import numpy as np
import pytest

from fplbot.config import TUNABLES
from fplbot.domain.captaincy import (
    HAUL_THRESHOLD,
    assess_captain_confidence,
    build_captain_picks,
    captain_score,
    captain_warnings,
    explain_captain,
)
from fplbot.domain.ranking import rank_score
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
    ownership: float = 20.0,
    price: float = 9.0,
    position: str = "MID",
    fixture_kind: FixtureKind = FixtureKind.NORMAL,
    availability: AvailabilitySignal | None = None,
    samples: np.ndarray | None = None,
) -> PlayerScore:
    if samples is None:
        rng = np.random.default_rng(element_id)
        samples = np.clip(rng.normal(mean, spread, 4000), 0, None)
        samples = samples * (mean / max(samples.mean(), 1e-9))

    return PlayerScore(
        element_id=element_id,
        name=f"Player{element_id}",
        team_short="ARS",
        team_id=1,
        position=position,
        price=price,
        ownership=ownership,
        distribution=Distribution(samples=samples),
        availability=availability or AvailabilitySignal(element_id=element_id, risk=0.0),
        fixture_kind=fixture_kind,
        fixture_count=0 if fixture_kind is FixtureKind.BLANK else 1,
        components={"goals": 3.0, "assists": 1.0},
        opponents=["CHE(H)"],
        ep_next=mean * 0.9,
    )


class TestObjectiveDiffersFromTransfers:
    def test_ownership_is_penalised_far_more_gently(self) -> None:
        """THE distinction.

        The transfer objective discounts a template player heavily because his
        points wash out against the field. For the armband that logic weakens
        sharply: the template captain is usually the template captain because he
        is genuinely the best option, and a captaincy differential loses ground
        faster than it gains it.
        """
        template = a_score(1, mean=8.0, ownership=65.0)
        differential = a_score(2, mean=8.0, ownership=4.0)

        transfer_gap = rank_score(differential, TUNABLES) - rank_score(template, TUNABLES)
        captain_gap = captain_score(differential, TUNABLES) - captain_score(template, TUNABLES)

        assert transfer_gap > 0, "the transfer objective prefers the differential"
        assert captain_gap > 0, "captaincy still prefers it, all else equal"
        assert captain_gap < transfer_gap, (
            "but by much less - the armband is not the place to chase ownership edge"
        )

    def test_the_mean_dominates(self) -> None:
        """Doubling makes raw expectation matter roughly twice as much."""
        strong = a_score(1, mean=9.0, ownership=60.0)
        weak = a_score(2, mean=4.0, ownership=3.0)

        assert captain_score(strong, TUNABLES) > captain_score(weak, TUNABLES)

    def test_a_weak_floor_is_punished(self) -> None:
        """A captain blank is a DOUBLE zero - the worst outcome in a gameweek.

        Two players with the same mean but different downside must not score
        equally.
        """
        rng = np.random.default_rng(7)
        # Same mean, very different floors.
        steady = np.full(4000, 6.0)
        boom_or_bust = np.where(rng.random(4000) < 0.5, 0.0, 12.0)

        safe = a_score(1, samples=steady, ownership=20.0)
        volatile = a_score(2, samples=boom_or_bust, ownership=20.0)

        assert safe.mean == pytest.approx(volatile.mean, rel=0.05)
        assert safe.floor > volatile.floor
        assert captain_score(safe, TUNABLES) > captain_score(volatile, TUNABLES)

    def test_a_genuine_premium_still_beats_a_safe_mid_price_option(self) -> None:
        """The downside penalty must not collapse into "always captain the safest".

        A high-mean, high-ceiling forward carries real variance and is still the
        right armband. If penalising downside inverted that, the objective would
        be broken in the opposite direction.
        """
        rng = np.random.default_rng(11)
        # A premium: 8.0 mean, occasional blanks, big ceiling.
        premium = np.clip(rng.gamma(1.6, 5.0, 4000), 0, None)
        premium = premium * (8.0 / premium.mean())
        # A steady midfielder: 5.0 every week.
        steady = np.full(4000, 5.0)

        explosive = a_score(1, samples=premium, ownership=40.0)
        reliable = a_score(2, samples=steady, ownership=10.0)

        assert explosive.floor < reliable.floor
        assert captain_score(explosive, TUNABLES) > captain_score(reliable, TUNABLES)

    def test_ceiling_is_still_rewarded_at_equal_downside(self) -> None:
        """Holding the downside fixed, more upside is better."""
        rng = np.random.default_rng(5)
        base = np.clip(rng.normal(6.0, 2.0, 4000), 0, None)
        # Same distribution, but with the top decile stretched upwards.
        stretched = base.copy()
        top = stretched >= np.percentile(stretched, 90)
        stretched[top] = stretched[top] * 1.8

        flat = a_score(1, samples=base)
        explosive = a_score(2, samples=stretched)

        assert explosive.ceiling > flat.ceiling
        assert explosive.floor == pytest.approx(flat.floor, rel=0.05)
        assert captain_score(explosive, TUNABLES) > captain_score(flat, TUNABLES)


class TestPicks:
    def test_returns_five_by_default(self) -> None:
        scores = [a_score(i, mean=8.0 - i * 0.3) for i in range(1, 12)]

        picks = build_captain_picks(scores, TUNABLES)

        assert len(picks) == 5
        assert TUNABLES.captain_picks == 5

    def test_ordered_by_the_captaincy_objective(self) -> None:
        scores = [a_score(i, mean=4.0 + i) for i in range(1, 6)]

        picks = build_captain_picks(scores, TUNABLES)
        objective = [pick.captain_score for pick in picks]

        assert objective == sorted(objective, reverse=True)

    def test_expected_points_are_doubled(self) -> None:
        """What lands in your score is 2x, and that is what we report."""
        picks = build_captain_picks([a_score(1, mean=7.5)], TUNABLES)

        assert picks[0].expected_points == pytest.approx(15.0, abs=0.5)
        assert picks[0].captained_ceiling == pytest.approx(picks[0].score.ceiling * 2)
        assert picks[0].captained_floor == pytest.approx(picks[0].score.floor * 2)

    def test_blanks_are_excluded(self) -> None:
        """A captained blank is zero, doubled."""
        scores = [
            a_score(1, mean=12.0, fixture_kind=FixtureKind.BLANK),
            a_score(2, mean=5.0),
        ]

        picks = build_captain_picks(scores, TUNABLES)

        assert [p.element_id for p in picks] == [2]

    def test_availability_filter_is_stricter_than_the_buy_board(self) -> None:
        """0.5 here versus 0.95 for a transfer, because the downside is doubled.

        A player worth a punt in your squad is not automatically worth the
        armband.
        """
        risky = a_score(1, mean=12.0, availability=AvailabilitySignal(element_id=1, risk=0.6))
        safe = a_score(2, mean=5.0)

        picks = build_captain_picks([risky, safe], TUNABLES)

        assert [p.element_id for p in picks] == [2]

    def test_the_template_captain_is_labelled(self) -> None:
        """From `events[].most_captained` - FPL's own answer."""
        scores = [a_score(1, mean=9.0), a_score(2, mean=8.0)]

        picks = build_captain_picks(scores, TUNABLES, most_captained_element=1)

        assert picks[0].is_template is True
        assert picks[1].is_template is False

    def test_differentials_are_flagged(self) -> None:
        scores = [a_score(1, mean=8.0, ownership=3.0), a_score(2, mean=8.0, ownership=55.0)]

        picks = build_captain_picks(scores, TUNABLES)
        by_id = {pick.element_id: pick for pick in picks}

        assert by_id[1].is_differential is True
        assert by_id[2].is_differential is False

    def test_haul_probability_is_reported(self) -> None:
        picks = build_captain_picks([a_score(1, mean=8.0, spread=5.0)], TUNABLES)

        assert 0.0 <= picks[0].haul_probability <= 1.0
        assert picks[0].haul_probability == pytest.approx(
            picks[0].score.distribution.probability_above(HAUL_THRESHOLD - 0.001), abs=0.02
        )

    def test_every_pick_carries_a_why(self) -> None:
        picks = build_captain_picks([a_score(1), a_score(2)], TUNABLES)

        for pick in picks:
            assert pick.why
            assert pick.why.endswith(".")
            assert pick.confidence in set(Confidence)

    def test_empty_input_is_handled(self) -> None:
        assert build_captain_picks([], TUNABLES) == []


class TestConfidence:
    def test_awaiting_a_press_conference_is_low(self) -> None:
        score = a_score(
            1,
            availability=AvailabilitySignal(
                element_id=1, risk=0.05, injury_condition="Currently Being Assessed"
            ),
        )

        assert assess_captain_confidence(score, TUNABLES) is Confidence.LOW

    def test_any_doubt_at_all_is_low_for_captaincy(self) -> None:
        """Stricter than the transfer equivalent, where DOUBT is MEDIUM.

        The cost of being wrong is doubled, so the bar is higher.
        """
        score = a_score(
            1, availability=AvailabilitySignal(element_id=1, risk=0.35, level=RiskLevel.DOUBT)
        )

        assert assess_captain_confidence(score, TUNABLES) is Confidence.LOW

    def test_a_weak_floor_caps_confidence(self) -> None:
        rng = np.random.default_rng(3)
        bust = np.where(rng.random(4000) < 0.4, 0.0, 11.0)
        score = a_score(1, samples=bust)

        assert score.floor < TUNABLES.captain_floor_target
        assert assess_captain_confidence(score, TUNABLES) is not Confidence.HIGH

    def test_a_nailed_reliable_player_is_high(self) -> None:
        score = a_score(1, samples=np.full(4000, 7.0))

        assert assess_captain_confidence(score, TUNABLES) is Confidence.HIGH


class TestExplanations:
    def test_leads_with_the_doubled_number(self) -> None:
        why = explain_captain(a_score(1, mean=7.0), TUNABLES, is_template=False)

        assert "armband" in why
        assert "x2" in why

    def test_names_the_template_trade_off(self) -> None:
        why = explain_captain(a_score(1), TUNABLES, is_template=True)

        assert "template" in why.lower()
        assert "rank-neutral" in why

    def test_names_the_differential_trade_off_in_both_directions(self) -> None:
        """A differential captain is a large swing - and it cuts both ways.

        Saying only the upside would be selling, not explaining.
        """
        why = explain_captain(a_score(1, ownership=3.0), TUNABLES, is_template=False)

        assert "rank swing" in why
        assert "blank" in why


class TestWarnings:
    def test_press_conference_warning_says_wait(self) -> None:
        score = a_score(
            1,
            availability=AvailabilitySignal(
                element_id=1, risk=0.05, injury_condition="Currently Being Assessed"
            ),
        )

        assert any("T-3h" in w for w in captain_warnings(score, TUNABLES))

    def test_availability_warning_names_the_stakes(self) -> None:
        score = a_score(1, availability=AvailabilitySignal(element_id=1, risk=0.2))

        warnings = captain_warnings(score, TUNABLES)

        assert any("worst outcome" in w for w in warnings)
