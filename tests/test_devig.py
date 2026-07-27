"""Devigging.

The properties that matter:

* probabilities sum to 1 for a genuine partition;
* Shin and power both remove margin *without* the longshot bias that
  multiplicative normalisation introduces;
* anytime-goalscorer prices are devigged per player, never normalised across a
  squad.

That last one is the serious error the spec calls out, and there is a test for it.
"""

from __future__ import annotations

import pytest

from fplbot.domain.devig import (
    DevigMethod,
    clean_sheet_probability_from_goals,
    devig_1x2,
    devig_goalscorers,
    devig_multiplicative,
    devig_power,
    devig_shin,
    implied_probabilities,
    overround,
    poisson_cdf,
    poisson_pmf,
    raw_implied,
)


class TestRawImplied:
    def test_inverts_decimal_odds(self) -> None:
        assert raw_implied([2.0])[0] == pytest.approx(0.5)
        assert raw_implied([4.0])[0] == pytest.approx(0.25)

    def test_rejects_impossible_odds(self) -> None:
        with pytest.raises(ValueError, match=r"must exceed 1\.0"):
            raw_implied([0.95])

    def test_overround_is_above_one_for_a_real_book(self) -> None:
        # A typical EPL 1X2 line with roughly 5% margin.
        assert overround([2.10, 3.50, 3.60]) > 1.0


class TestShin:
    def test_probabilities_sum_to_one(self) -> None:
        result = devig_shin([2.10, 3.50, 3.60])

        assert sum(result.probabilities) == pytest.approx(1.0, abs=1e-9)
        assert result.method is DevigMethod.SHIN

    def test_preserves_ordering(self) -> None:
        """Devigging changes magnitudes, never the ranking."""
        result = devig_shin([1.50, 4.50, 7.00])

        assert result.probabilities[0] > result.probabilities[1] > result.probabilities[2]

    def test_estimates_a_plausible_insider_proportion(self) -> None:
        result = devig_shin([2.10, 3.50, 3.60])

        assert result.parameter is not None
        assert 0 <= result.parameter < 0.2, "z should be small in a liquid market"

    def test_handles_a_book_with_no_margin(self) -> None:
        """A fair book, or an arbitrage. Must not divide by zero or diverge."""
        result = devig_shin([3.0, 3.0, 3.0])

        assert sum(result.probabilities) == pytest.approx(1.0)


class TestPower:
    def test_probabilities_sum_to_one(self) -> None:
        result = devig_power([1.60, 15.0])

        assert sum(result.probabilities) == pytest.approx(1.0, abs=1e-9)

    def test_exponent_is_at_least_one(self) -> None:
        result = devig_power([2.10, 3.50, 3.60])

        assert result.parameter is not None
        assert result.parameter >= 1.0


class TestLongshotBias:
    def test_multiplicative_overestimates_longshots(self) -> None:
        """THE reason the method choice matters.

        Bookmakers load margin disproportionately onto longshots, because that is
        where recreational money goes. Multiplicative normalisation removes it
        evenly and therefore leaves longshots overpriced.

        For us this is concrete: anytime-goalscorer markets are exactly the
        lopsided case (Haaland ~1.6 against a full-back ~15.0), and
        overestimating longshot scorers means over-recommending cheap
        differential forwards - the very mistake a rank-attacking objective is
        most exposed to.
        """
        # A heavy favourite, a middling outcome and a genuine longshot. The raw
        # implied probabilities sum to ~1.04, so there is real margin to remove.
        odds = [1.20, 7.00, 15.0]

        multiplicative = devig_multiplicative(odds).probabilities
        power = devig_power(odds).probabilities

        longshot_index = 2
        assert multiplicative[longshot_index] > power[longshot_index], (
            "multiplicative should assign the longshot a HIGHER probability than "
            "power - that gap is the bias we are avoiding"
        )

    def test_methods_agree_on_a_balanced_line(self) -> None:
        """The method choice is immaterial where the book is symmetric."""
        odds = [2.05, 2.05]

        multiplicative = devig_multiplicative(odds).probabilities
        shin = devig_shin(odds).probabilities

        assert multiplicative[0] == pytest.approx(shin[0], abs=0.01)


class TestGoalscorers:
    def test_each_player_is_devigged_independently(self) -> None:
        """Anytime goalscorer is NOT a partition.

        Outcomes are Yes/No per player, several players can score in the same
        match, and the probabilities across a squad do not sum to anything in
        particular. Normalising them to 1.0 would compress every striker towards
        the mean and destroy the signal.
        """
        prices = {
            1: (1.60, 2.20),  # a prolific striker
            2: (1.80, 2.00),  # the other striker
            3: (2.50, 1.55),  # an attacking midfielder
            4: (3.50, 1.28),  # a midfielder
            5: (15.0, 1.02),  # a full-back
        }

        probabilities = devig_goalscorers(prices)

        assert sum(probabilities.values()) > 1.0, (
            "these must NOT sum to 1 - they are independent Yes/No markets, "
            "and a sum of 1 would prove we had wrongly normalised across players"
        )
        assert probabilities[1] > probabilities[3] > probabilities[5]
        for probability in probabilities.values():
            assert 0.0 < probability < 1.0

    def test_falls_back_when_only_one_side_is_priced(self) -> None:
        probabilities = devig_goalscorers({1: (2.00, 0.0)})

        # Raw implied is 0.50; a flat margin assumption should pull it below that.
        assert 0.40 < probabilities[1] < 0.50


class TestHelpers:
    def test_devig_1x2_returns_three_probabilities(self) -> None:
        home, draw, away = devig_1x2(2.10, 3.50, 3.60)

        assert home + draw + away == pytest.approx(1.0)
        # 3.50 (draw) is a shorter price than 3.60 (away), so draw ranks above it.
        assert home > draw > away

    def test_dispatcher_selects_the_method(self) -> None:
        assert implied_probabilities([2.0, 2.0], DevigMethod.SHIN).method is DevigMethod.SHIN
        assert implied_probabilities([2.0, 2.0], DevigMethod.POWER).method is DevigMethod.POWER


class TestPoisson:
    def test_pmf_matches_known_values(self) -> None:
        # P(X=0 | lambda=1) = e^-1
        assert poisson_pmf(0, 1.0) == pytest.approx(0.3678794, abs=1e-6)
        assert poisson_pmf(2, 2.0) == pytest.approx(0.2706706, abs=1e-6)

    def test_pmf_sums_to_one(self) -> None:
        assert sum(poisson_pmf(k, 1.5) for k in range(30)) == pytest.approx(1.0, abs=1e-9)

    def test_cdf_is_monotonic(self) -> None:
        values = [poisson_cdf(k, 1.4) for k in range(8)]

        assert values == sorted(values)
        assert values[-1] == pytest.approx(1.0, abs=1e-3)

    def test_clean_sheet_from_expected_goals(self) -> None:
        """Sanity: a mean of zero conceded means a certain clean sheet."""
        assert clean_sheet_probability_from_goals(0.0) == pytest.approx(1.0)
        assert clean_sheet_probability_from_goals(1.4) == pytest.approx(0.2466, abs=1e-3)
