"""The scoring model.

Distribution-level tests rather than exact-value tests. Asserting that a player's
xP equals 5.37 would be testing the current coefficients, which SPEC section 5
says explicitly are placeholders awaiting a fit. What we test instead are the
*properties* that must hold whatever the coefficients become:

* a blank is zero, with certainty;
* a double is worth more than a single;
* an unavailable player scores nothing;
* shrinkage pulls small samples towards the prior;
* the distribution has genuine spread, because the spread is the deliverable.
"""

from __future__ import annotations

import numpy as np
import pytest

from fplbot.config import TUNABLES
from fplbot.domain.minutes import MinutesDistribution, estimate_minutes
from fplbot.domain.scoring import ScoringContext, score_player, shrink_per_90
from fplbot.models.domain import AvailabilitySignal, FixtureContext, TeamGameweek
from fplbot.models.fpl import Bootstrap, ScoringRules


@pytest.fixture
def scoring_context(bootstrap: Bootstrap) -> ScoringContext:
    return ScoringContext(
        element_types={1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"},
        scoring_rules=bootstrap.game_config.scoring,
        season_has_started=True,
        tunables=TUNABLES,
        # A fixed seed makes every assertion below reproducible. Re-running for
        # the same gameweek and tier must produce the same board, so that a
        # difference between two runs means the data changed, not the dice.
        rng=np.random.default_rng(42),
    )


def a_fixture(**overrides) -> FixtureContext:
    defaults = {
        "fixture_id": 1,
        "team_id": 1,
        "opponent_id": 2,
        "is_home": True,
        "difficulty": 3,
        "clean_sheet_probability": 0.30,
        "expected_team_goals": 1.6,
        "expected_goals_conceded": 1.2,
    }
    defaults.update(overrides)
    return FixtureContext(**defaults)


NAILED = MinutesDistribution(0.90, 0.06, 0.03, 0.01)
CLEAR = AvailabilitySignal(element_id=1, risk=0.0)


class TestShrinkage:
    def test_small_samples_are_pulled_towards_the_prior(self) -> None:
        """A player with 90 minutes and 1.0 xG is not a 1.0 xG/90 player.

        He is a player about whom we know almost nothing, and the honest estimate
        is barely distinguishable from the positional average.
        """
        estimate = shrink_per_90(raw_per_90=1.0, minutes_played=90, prior=0.35, prior_minutes=450)

        assert estimate < 0.5, "one hot afternoon must not become a season-long rate"
        assert estimate > 0.35, "but it should move somewhat"

    def test_large_samples_dominate_the_prior(self) -> None:
        estimate = shrink_per_90(raw_per_90=1.0, minutes_played=3000, prior=0.35, prior_minutes=450)

        assert estimate > 0.85

    def test_the_halfway_point_is_the_prior_minutes(self) -> None:
        estimate = shrink_per_90(raw_per_90=1.0, minutes_played=450, prior=0.0, prior_minutes=450)

        assert estimate == pytest.approx(0.5)

    def test_no_data_returns_the_prior_exactly(self) -> None:
        assert shrink_per_90(None, 0, prior=0.35, prior_minutes=450) == 0.35
        assert shrink_per_90(1.0, 0, prior=0.35, prior_minutes=450) == 0.35


class TestBlanksAndDoubles:
    def test_a_blank_is_certainly_zero(
        self, bootstrap: Bootstrap, scoring_context: ScoringContext
    ) -> None:
        """A player whose team does not play scores nothing. Not an expectation."""
        element = bootstrap.elements[0]
        blank = TeamGameweek(team_id=1, gameweek=1, fixtures=())

        distribution, components = score_player(element, blank, NAILED, CLEAR, scoring_context)

        assert distribution.mean == 0.0
        assert distribution.std == 0.0
        assert distribution.ceiling == 0.0
        assert components == {"blank": 1.0}

    def test_a_double_beats_a_single(
        self, bootstrap: Bootstrap, scoring_context: ScoringContext
    ) -> None:
        element = next(e for e in bootstrap.elements if e.id == 1)

        single = TeamGameweek(1, 1, (a_fixture(),))
        double = TeamGameweek(1, 1, (a_fixture(), a_fixture(fixture_id=2, is_home=False)))

        single_dist, _ = score_player(element, single, NAILED, CLEAR, scoring_context)
        double_dist, _ = score_player(element, double, NAILED, CLEAR, scoring_context)

        assert double_dist.mean > single_dist.mean
        # Not exactly double: the second fixture carries extra rotation risk,
        # because managers rotate across a congested week.
        assert double_dist.mean < single_dist.mean * 2.0


class TestAvailability:
    def test_certain_absence_scores_zero(
        self, bootstrap: Bootstrap, scoring_context: ScoringContext
    ) -> None:
        element = bootstrap.elements[0]
        out = AvailabilitySignal(element_id=element.id, risk=1.0)

        distribution, _ = score_player(
            element, TeamGameweek(1, 1, (a_fixture(),)), NAILED, out, scoring_context
        )

        assert distribution.mean == 0.0

    def test_risk_reduces_the_mean(
        self, bootstrap: Bootstrap, scoring_context: ScoringContext
    ) -> None:
        element = next(e for e in bootstrap.elements if e.id == 1)
        gameweek = TeamGameweek(1, 1, (a_fixture(),))

        clear, _ = score_player(element, gameweek, NAILED, CLEAR, scoring_context)
        doubtful, _ = score_player(
            element,
            gameweek,
            NAILED,
            AvailabilitySignal(element_id=element.id, risk=0.5),
            scoring_context,
        )

        assert doubtful.mean < clear.mean

    def test_risk_is_a_gate_not_a_scaling(
        self, bootstrap: Bootstrap, scoring_context: ScoringContext
    ) -> None:
        """Applied as a Bernoulli gate on the whole gameweek.

        Scaling the mean would quietly shrink the ceiling too - and the ceiling
        is precisely what a rank-attacking objective is buying.
        """
        element = next(e for e in bootstrap.elements if e.id == 1)
        gameweek = TeamGameweek(1, 1, (a_fixture(),))

        clear, _ = score_player(element, gameweek, NAILED, CLEAR, scoring_context)
        risky, _ = score_player(
            element,
            gameweek,
            NAILED,
            AvailabilitySignal(element_id=element.id, risk=0.4),
            scoring_context,
        )

        assert risky.ceiling == pytest.approx(clear.ceiling, rel=0.35), (
            "the upside when he plays should be broadly unchanged"
        )


class TestDistributionShape:
    def test_there_is_real_spread(
        self, bootstrap: Bootstrap, scoring_context: ScoringContext
    ) -> None:
        """The spread IS the deliverable under a rank-attacking objective."""
        element = next(e for e in bootstrap.elements if e.id == 1)

        distribution, _ = score_player(
            element, TeamGameweek(1, 1, (a_fixture(),)), NAILED, CLEAR, scoring_context
        )

        assert distribution.std > 1.0
        assert distribution.ceiling > distribution.mean > distribution.floor

    def test_haul_probability_is_plausible(
        self, bootstrap: Bootstrap, scoring_context: ScoringContext
    ) -> None:
        element = next(e for e in bootstrap.elements if e.id == 1)  # a premium forward

        distribution, _ = score_player(
            element, TeamGameweek(1, 1, (a_fixture(),)), NAILED, CLEAR, scoring_context
        )

        assert 0.0 < distribution.haul_probability < 0.5

    def test_components_are_reported_for_explainability(
        self, bootstrap: Bootstrap, scoring_context: ScoringContext
    ) -> None:
        """ "5.1 xP of which 2.3 is clean-sheet equity" is an argument.

        "5.1 xP" is an assertion. The report has to be legible enough to disagree
        with, which means the breakdown has to exist.
        """
        defender = next(e for e in bootstrap.elements if e.id == 5)

        _, components = score_player(
            defender, TeamGameweek(1, 1, (a_fixture(),)), NAILED, CLEAR, scoring_context
        )

        assert "clean_sheet" in components
        assert "appearance" in components
        assert components["clean_sheet"] > 0

    def test_a_keeper_gets_save_points(
        self, bootstrap: Bootstrap, scoring_context: ScoringContext
    ) -> None:
        keeper = next(e for e in bootstrap.elements if e.id == 8)

        _, components = score_player(
            keeper, TeamGameweek(1, 1, (a_fixture(),)), NAILED, CLEAR, scoring_context
        )

        assert components["saves"] > 0


class TestPurity:
    def test_the_same_inputs_produce_the_same_output(self, bootstrap: Bootstrap) -> None:
        """`score_player` is pure - the v2 seam depends on it.

        A future squad-aware ILP optimiser consumes these distributions unchanged,
        which only works if scoring is deterministic given its inputs.
        """
        element = bootstrap.elements[0]
        gameweek = TeamGameweek(1, 1, (a_fixture(),))

        def build_context() -> ScoringContext:
            return ScoringContext(
                element_types={1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"},
                scoring_rules=ScoringRules(),
                season_has_started=True,
                tunables=TUNABLES,
                rng=np.random.default_rng(7),
            )

        first, _ = score_player(element, gameweek, NAILED, CLEAR, build_context())
        second, _ = score_player(element, gameweek, NAILED, CLEAR, build_context())

        assert first.mean == second.mean
        assert first.ceiling == second.ceiling


class TestMinutesModel:
    def test_suspension_is_deterministic(self, bootstrap: Bootstrap) -> None:
        """A ban is not a fitness question - there is no probability to model."""
        element = bootstrap.elements[0]
        suspended = AvailabilitySignal(element_id=element.id, risk=1.0, injury_reason="Suspended")

        distribution = estimate_minutes(element, suspended, season_has_started=True)

        assert distribution.out == 1.0
        assert distribution.probability_of_playing == 0.0

    def test_predicted_to_start_raises_the_starter_probability(self, bootstrap: Bootstrap) -> None:
        """The freshest signal we have - it reflects press conferences."""
        element = next(e for e in bootstrap.elements if e.id == 100)  # a filler, no history

        without = estimate_minutes(element, CLEAR, season_has_started=True)
        with_lineup = estimate_minutes(
            element, CLEAR, season_has_started=True, predicted_to_start=True
        )

        assert with_lineup.starter > without.starter
        assert with_lineup.starter >= 0.85

    def test_availability_caps_the_starter_probability(self, bootstrap: Bootstrap) -> None:
        """A 25% player cannot be an 85% starter, whatever a stale lineup said."""
        element = next(e for e in bootstrap.elements if e.id == 6)  # 25% chance
        doubtful = AvailabilitySignal(element_id=6, risk=0.75, fpl_chance_pct=25)

        distribution = estimate_minutes(
            element, doubtful, season_has_started=True, predicted_to_start=True
        )

        assert distribution.probability_of_playing <= 0.26

    def test_probabilities_always_sum_to_one(self, bootstrap: Bootstrap) -> None:
        for element in bootstrap.elements[:10]:
            distribution = estimate_minutes(element, CLEAR, season_has_started=False)
            total = sum(distribution.as_tuple())
            assert total == pytest.approx(1.0)

    def test_pre_season_ignores_carried_over_minutes(self, bootstrap: Bootstrap) -> None:
        """The season gate.

        In pre-season `minutes` and `starts` hold LAST season's values while
        everything around them is reset. Using them silently mixes two seasons.
        """
        element = next(e for e in bootstrap.elements if e.id == 1)

        pre_season = estimate_minutes(element, CLEAR, season_has_started=False, games_played=0)
        in_season = estimate_minutes(element, CLEAR, season_has_started=True, games_played=30)

        assert pre_season.as_tuple() != in_season.as_tuple()
