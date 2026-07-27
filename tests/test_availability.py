"""Availability risk and transfer-flow inference.

The headline feature, and the one with the most ways to produce false positives.
These tests concentrate on the four pieces of care the spec calls for:
ownership normalisation, a per-player baseline, cause discrimination, and chip
contamination.
"""

from __future__ import annotations

import pytest

from fplbot.config import TUNABLES
from fplbot.domain.availability import (
    build_availability_signal,
    chip_discount_for_event,
    classify_flow_cause,
    cold_start_caveat,
    ewma_baseline,
    flow_series_from_snapshots,
    flow_zscore,
    normalise_flow,
)
from fplbot.models.domain import RiskLevel
from fplbot.models.fpl import Bootstrap, Element, Event


class TestOwnershipNormalisation:
    def test_the_same_absolute_flow_means_different_things(self) -> None:
        """NEVER use absolute counts.

        A 3%-owned and a 40%-owned player with the same absolute net-outflow are
        telling completely different stories: the first has lost a large fraction
        of his owners, the second a trivial one. Absolute counts are dominated by
        ownership and would put the same five template players at the top of the
        list every week.
        """
        total_players = 10_000_000
        low_owned, _ = normalise_flow(0, 30_000, ownership_percent=3.0, total_players=total_players)
        high_owned, _ = normalise_flow(
            0, 30_000, ownership_percent=40.0, total_players=total_players
        )

        assert abs(low_owned) > abs(high_owned) * 10

    def test_net_outflow_is_negative(self) -> None:
        net, owners = normalise_flow(1_000, 5_000, 10.0, 1_000_000)

        assert net < 0
        assert owners == 100_000

    def test_owner_count_is_floored_at_one(self) -> None:
        """No division by zero for a 0%-owned player."""
        net, owners = normalise_flow(0, 100, 0.0, 1_000_000)

        assert owners == 1
        assert net == -100.0


class TestBaseline:
    def test_ewma_weights_recent_observations_more(self) -> None:
        """Relevance decays.

        A plain rolling mean would also make the z-score jump whenever an old
        observation falls out of a hard window boundary.
        """
        steady = [0.0] * 10
        rising = [0.0] * 9 + [1.0]

        steady_mean, _ = ewma_baseline(steady)
        rising_mean, _ = ewma_baseline(rising)

        assert rising_mean > steady_mean

    def test_standard_deviation_never_reaches_zero(self) -> None:
        """A flat history must not produce an infinite z-score.

        Which is exactly the common case: a player nobody has transferred all week.
        """
        _, std = ewma_baseline([0.0] * 20)

        assert std > 0

    def test_zscore_refuses_thin_history(self) -> None:
        """An honest gap beats a confident number derived from three observations."""
        assert flow_zscore(-5.0, [0.0, 0.0, 0.0]) is None

    def test_zscore_computed_once_there_is_enough_history(self) -> None:
        history = [0.0] * TUNABLES.min_snapshots_for_zscore

        zscore = flow_zscore(-1.0, history)

        assert zscore is not None
        assert zscore < 0


class TestCauseDiscrimination:
    def _element(self, **overrides) -> Element:
        base = {"id": 1, "code": 1, "element_type": 3, "team": 1, "web_name": "Test"}
        base.update(overrides)
        return Element.model_validate(base)

    def test_net_inflow_during_a_price_run_is_a_bandwagon(self) -> None:
        """Direction alone settles this one."""
        cause = classify_flow_cause(
            self._element(cost_change_event=1),
            zscore=3.5,
            net_per_owner=0.05,
            team_mate_zscores=[],
        )

        assert cause == "price_bandwagon"

    def test_team_mates_moving_together_is_not_an_injury(self) -> None:
        """The most valuable discriminator we have.

        Injury news is idiosyncratic by nature; a fixture swing, a blank
        resolving or a manager sacking moves an entire club's roster together.
        """
        cause = classify_flow_cause(
            self._element(),
            zscore=-3.5,
            net_per_owner=-0.05,
            team_mate_zscores=[-3.2, -3.4, -2.9, -3.1],
        )

        assert cause == "team_wide_churn"

    def test_an_idiosyncratic_sharp_outflow_is_bad_news(self) -> None:
        cause = classify_flow_cause(
            self._element(),
            zscore=-4.0,
            net_per_owner=-0.08,
            team_mate_zscores=[0.1, -0.2, 0.3],
            hour_of_day=14,
        )

        assert cause == "bad_news"

    def test_out_of_hours_movement_is_the_stronger_tell(self) -> None:
        """News breaks in the evening; transfer planning happens during the day."""
        cause = classify_flow_cause(
            self._element(),
            zscore=-4.0,
            net_per_owner=-0.08,
            team_mate_zscores=[0.1],
            hour_of_day=22,
        )

        assert cause == "bad_news_out_of_hours"

    def test_quiet_movement_is_not_classified(self) -> None:
        assert (
            classify_flow_cause(
                self._element(), zscore=-0.5, net_per_owner=-0.001, team_mate_zscores=[]
            )
            is None
        )

    def test_no_zscore_means_no_classification(self) -> None:
        assert (
            classify_flow_cause(
                self._element(), zscore=None, net_per_owner=-0.9, team_mate_zscores=[]
            )
            is None
        )


class TestChipContamination:
    def test_wildcard_weeks_are_discounted(self) -> None:
        """Wildcards inflate raw counts ~29% while contributing ~1.4% of real pressure.

        Without this, GW1-2, GW20-21 and every post-blank week read as alarming.
        """
        heavy = Event.model_validate(
            {
                "id": 1,
                "deadline_time_epoch": 0,
                "chip_plays": [{"chip_name": "wildcard", "num_played": 900_000}],
            }
        )
        quiet = Event.model_validate(
            {
                "id": 2,
                "deadline_time_epoch": 0,
                "chip_plays": [{"chip_name": "wildcard", "num_played": 5_000}],
            }
        )

        heavy_discount = chip_discount_for_event(heavy, 10_000_000)
        quiet_discount = chip_discount_for_event(quiet, 10_000_000)

        assert heavy_discount > quiet_discount
        assert heavy_discount <= TUNABLES.chip_contamination_discount

    def test_no_event_means_no_discount(self) -> None:
        assert chip_discount_for_event(None, 10_000_000) == 0.0


class TestCompositeSignal:
    def test_fpl_chance_drives_the_base_risk(self, bootstrap: Bootstrap) -> None:
        doubtful = next(e for e in bootstrap.elements if e.id == 6)  # 25%

        signal = build_availability_signal(doubtful)

        assert signal.risk == pytest.approx(0.75)
        assert signal.level is RiskLevel.SERIOUS

    def test_agreement_between_sources_is_recorded(self, bootstrap: Bootstrap) -> None:
        doubtful = next(e for e in bootstrap.elements if e.id == 6)

        signal = build_availability_signal(doubtful, injury_status="25%")

        assert "premierinjuries" in signal.corroborating_sources
        assert not signal.conflicting_sources

    def test_disagreement_is_surfaced_not_resolved(self, bootstrap: Bootstrap) -> None:
        """Disagreement means we know LESS, not more.

        We sit between the two readings and let the confidence downgrade reflect
        it, rather than silently picking a winner.
        """
        fit_per_fpl = next(e for e in bootstrap.elements if e.id == 1)

        signal = build_availability_signal(fit_per_fpl, injury_status="Ruled Out")

        assert "premierinjuries" in signal.conflicting_sources
        assert any("disagree" in note for note in signal.notes)
        assert 0.0 < signal.risk < 1.0, "should sit between the two views"

    def test_suspension_is_deterministic(self, bootstrap: Bootstrap) -> None:
        element = bootstrap.elements[0]

        signal = build_availability_signal(element, injury_reason="Suspended")

        assert signal.risk == 1.0
        assert signal.is_suspension is True
        assert any("deterministic" in note for note in signal.notes)

    def test_transfer_flow_only_adds_risk_never_removes_it(self, bootstrap: Bootstrap) -> None:
        """A managers' stampede is evidence, not proof.

        It must not be able to rule a fit player out on its own.
        """
        element = bootstrap.elements[0]

        baseline = build_availability_signal(element)
        flagged = build_availability_signal(element, zscore=-4.5, flow_cause="bad_news")

        assert flagged.risk > baseline.risk
        assert flagged.risk < 1.0

    def test_awaiting_press_conference_is_flagged(self, bootstrap: Bootstrap) -> None:
        """This defines the Phase 2 re-check set."""
        element = bootstrap.elements[0]

        signal = build_availability_signal(element, injury_condition="Currently Being Assessed")

        assert signal.awaiting_press_conference is True
        assert any("press conference" in note for note in signal.notes)

    def test_risk_is_always_bounded(self, bootstrap: Bootstrap) -> None:
        element = next(e for e in bootstrap.elements if e.id == 6)

        signal = build_availability_signal(
            element, injury_status="Ruled Out", zscore=-10.0, flow_cause="bad_news"
        )

        assert 0.0 <= signal.risk <= 1.0


class TestColdStart:
    def test_caveat_appears_when_history_is_thin(self) -> None:
        """SPEC 5.3 requires this sentence in the email.

        Presenting a z-score built from four observations as though it were sharp
        is exactly the confident wrongness the whole spec is written to avoid.
        """
        caveat = cold_start_caveat(3)

        assert caveat is not None
        assert "snapshots" in caveat

    def test_no_caveat_once_there_is_enough(self) -> None:
        assert cold_start_caveat(TUNABLES.min_snapshots_for_zscore) is None


class TestSeriesConstruction:
    def test_snapshots_become_a_per_player_series(self) -> None:
        snapshots = [
            # DynamoDB returns newest first; the function must reverse so the
            # EWMA runs forwards in time.
            {
                "total_players": 1_000_000,
                "players": [
                    {
                        "id": 1,
                        "selected_by_percent": "10.0",
                        "transfers_in_event": 0,
                        "transfers_out_event": 500,
                    }
                ],
            },
            {
                "total_players": 1_000_000,
                "players": [
                    {
                        "id": 1,
                        "selected_by_percent": "10.0",
                        "transfers_in_event": 0,
                        "transfers_out_event": 100,
                    }
                ],
            },
        ]

        series = flow_series_from_snapshots(snapshots, 1_000_000)

        assert len(series[1]) == 2
        # Oldest first after the reversal: the smaller outflow should come first.
        assert series[1][0] > series[1][1]
