"""Grading our own predictions against what actually happened.

The metrics here are the feedback loop the model did not previously have:
`_log_benchmark` compares us against FPL's `ep_next`, which is another estimate
rather than truth, and cannot say whether the distribution is honest.
"""

from __future__ import annotations

import numpy as np
import pytest

from fplbot.domain.calibration import (
    CalibrationReport,
    PredictionRecord,
    rank_average,
    score_predictions,
    spearman,
    summarise,
)
from fplbot.models.domain import AvailabilitySignal, Distribution, FixtureKind, PlayerScore


def a_prediction(element_id: int, mean: float, floor: float, ceiling: float, **over):
    defaults = {
        "element_id": element_id,
        "mean": mean,
        "floor": floor,
        "ceiling": ceiling,
        "haul_probability": 0.1,
        "expected_minutes": 85.0,
        "ownership": 10.0,
    }
    defaults.update(over)
    return PredictionRecord(**defaults)


class TestRanking:
    def test_ties_get_the_average_rank(self) -> None:
        """Not an edge case: in pre-season hundreds of players share the same
        prior, and ordinal ranking would invent an order from array position."""
        ranks = rank_average(np.array([5.0, 1.0, 5.0, 3.0]))

        assert list(ranks) == [3.5, 1.0, 3.5, 2.0]

    def test_perfect_agreement_is_one(self) -> None:
        predicted = np.array([1.0, 2.0, 3.0, 4.0])

        assert spearman(predicted, np.array([10.0, 20.0, 30.0, 40.0])) == pytest.approx(1.0)

    def test_perfect_disagreement_is_minus_one(self) -> None:
        predicted = np.array([1.0, 2.0, 3.0, 4.0])

        assert spearman(predicted, np.array([40.0, 30.0, 20.0, 10.0])) == pytest.approx(-1.0)

    def test_monotonic_but_nonlinear_still_scores_one(self) -> None:
        """The point of using rank correlation. A model can be badly biased in
        level and still order players perfectly, and that model is still useful
        for a board - which is a ranking, not a set of point estimates."""
        predicted = np.array([1.0, 2.0, 3.0, 4.0])

        assert spearman(predicted, np.array([1.0, 4.0, 9.0, 16.0])) == pytest.approx(1.0)

    def test_constant_predictions_return_zero_not_nan(self) -> None:
        """Reachable in pre-season, when everyone falls back to one prior. A NaN
        would propagate into the log line and the report."""
        result = spearman(np.array([3.0, 3.0, 3.0]), np.array([1.0, 5.0, 9.0]))

        assert result == 0.0


class TestScoring:
    def test_returns_none_when_nothing_overlaps(self) -> None:
        """A non-answer must not look like a bad score. No overlap means the
        predictions are for another gameweek, or the actuals have not settled."""
        report = score_predictions(
            [a_prediction(1, 5.0, 2.0, 9.0)], {999: 6}, gameweek=3, tier="3h"
        )

        assert report is None

    def test_a_perfect_model_scores_perfectly(self) -> None:
        predictions = [a_prediction(i, float(i), 0.0, 20.0) for i in range(1, 6)]
        actuals = {i: i for i in range(1, 6)}

        report = score_predictions(predictions, actuals, gameweek=3, tier="3h")

        assert report is not None
        assert report.rmse == 0.0
        assert report.mae == 0.0
        assert report.spearman == pytest.approx(1.0)
        assert report.players == 5

    def test_coverage_counts_outcomes_inside_the_stated_interval(self) -> None:
        """P10-P90 should contain roughly 80% of outcomes. This is the sharpest
        check available, because `mu * ceiling` is a term in the ranking
        objective - a miscalibrated tail reorders the board."""
        predictions = [a_prediction(i, 5.0, 2.0, 9.0) for i in range(1, 5)]
        # Three inside [2, 9], one far outside.
        actuals = {1: 3, 2: 5, 3: 8, 4: 25}

        report = score_predictions(predictions, actuals, gameweek=3, tier="3h")

        assert report is not None
        assert report.coverage == 0.75

    def test_a_too_narrow_distribution_is_called_out(self) -> None:
        """The failure mode that matters: an overstated ceiling silently
        reorders the board and nothing else in the run would notice."""
        predictions = [a_prediction(i, 5.0, 4.9, 5.1) for i in range(1, 11)]
        actuals = {i: i for i in range(1, 11)}

        report = score_predictions(predictions, actuals, gameweek=3, tier="3h")

        assert report is not None
        assert report.coverage < 0.65
        assert any("too narrow" in note for note in report.notes)

    def test_a_systematic_bias_is_called_out(self) -> None:
        predictions = [a_prediction(i, 8.0, 0.0, 20.0) for i in range(1, 6)]
        actuals = dict.fromkeys(range(1, 6), 2)

        report = score_predictions(predictions, actuals, gameweek=3, tier="3h")

        assert report is not None
        assert any("over-predicted" in note for note in report.notes)

    def test_brier_rewards_honest_haul_probabilities(self) -> None:
        """A confident wrong forecast must score worse than a hedged one, even
        when both have the same mean."""
        confident = [a_prediction(i, 5.0, 0.0, 20.0, haul_probability=0.99) for i in range(1, 5)]
        hedged = [a_prediction(i, 5.0, 0.0, 20.0, haul_probability=0.25) for i in range(1, 5)]
        # Nobody hauled.
        actuals = dict.fromkeys(range(1, 5), 2)

        confident_report = score_predictions(confident, actuals, gameweek=1, tier="3h")
        hedged_report = score_predictions(hedged, actuals, gameweek=1, tier="3h")

        assert confident_report is not None and hedged_report is not None
        assert confident_report.brier > hedged_report.brier

    def test_starters_are_scored_separately(self) -> None:
        """The full player list is dominated by squad filler who were always
        going to score zero, and predicting that correctly flatters every
        metric. The board is only ever read for players expected to play."""
        predictions = [
            a_prediction(1, 6.0, 2.0, 12.0, expected_minutes=88.0),
            a_prediction(2, 5.0, 1.0, 11.0, expected_minutes=80.0),
            a_prediction(3, 0.2, 0.0, 1.0, expected_minutes=2.0),
            a_prediction(4, 0.1, 0.0, 1.0, expected_minutes=0.0),
        ]
        actuals = {1: 7, 2: 4, 3: 0, 4: 0}

        report = score_predictions(predictions, actuals, gameweek=3, tier="3h")

        assert report is not None
        assert report.players == 4
        assert report.starters == 2

    def test_tiers_are_graded_independently(self) -> None:
        """Storing both tiers is only worth doing if they are compared. If the
        team news at T-3h is worth anything, it shows up as a difference here."""
        predictions = [a_prediction(1, 5.0, 0.0, 10.0)]

        early = score_predictions(predictions, {1: 6}, gameweek=3, tier="24h")
        late = score_predictions(predictions, {1: 6}, gameweek=3, tier="3h")

        assert early is not None and late is not None
        assert early.tier == "24h"
        assert late.tier == "3h"


class TestSummarise:
    def test_a_scored_player_reduces_to_a_storable_record(self) -> None:
        """We store the distribution summary, not the 4000 samples: the samples
        are ~32 KB per player and reproducible from the seed anyway."""
        rng = np.random.default_rng(7)
        score = PlayerScore(
            element_id=42,
            name="Test",
            team_short="ARS",
            team_id=1,
            position="MID",
            price=8.0,
            ownership=13.5,
            distribution=Distribution(samples=np.clip(rng.normal(6, 3, 2000), 0, None)),
            availability=AvailabilitySignal(element_id=42, risk=0.05),
            fixture_kind=FixtureKind.NORMAL,
            fixture_count=1,
            components={"expected_minutes": 84.0},
        )

        records = summarise([score])

        assert len(records) == 1
        assert records[0].element_id == 42
        assert records[0].expected_minutes == 84.0
        assert records[0].ownership == 13.5
        assert records[0].floor < records[0].mean < records[0].ceiling

    def test_a_record_survives_a_storage_round_trip(self) -> None:
        """It is written to DynamoDB as JSON and read back weeks later."""
        original = a_prediction(9, 5.5, 1.25, 12.75, haul_probability=0.33)

        restored = PredictionRecord.from_dict(original.as_dict())

        assert restored == original


class TestReport:
    def test_the_summary_line_carries_the_four_headline_numbers(self) -> None:
        report = CalibrationReport(
            gameweek=7,
            tier="3h",
            players=580,
            rmse=2.41,
            mae=1.62,
            spearman=0.512,
            brier=0.0731,
            coverage=0.79,
            mean_predicted=1.88,
            mean_actual=1.94,
        )

        line = report.summary_line()

        assert "GW7" in line
        assert "rmse=2.41" in line
        assert "spearman=0.512" in line
        assert "coverage=79.0%" in line
