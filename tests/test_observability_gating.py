"""Which custom metrics actually get published.

CloudWatch bills per custom metric per month beyond a free ten, and a
dimensioned metric costs one per dimension set - so this stack was paying for 19.
Only the metrics an alarm depends on are published by default; the rest are
demoted to log lines.

The risk being guarded here is specific: an alarm whose metric stops being
emitted does not fail loudly. It sits in INSUFFICIENT_DATA, or with
TreatMissingData notBreaching it simply never fires again. Trimming the wrong
metric would silently switch off the schema-drift alarm that template.yaml calls
the most important one in the stack.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fplbot.observability import ALARMED_METRICS, Metric, _should_publish

TEMPLATE = Path(__file__).resolve().parents[1] / "template.yaml"


class TestAlarmCoverage:
    def test_every_alarmed_metric_is_still_published(self) -> None:
        """The load-bearing test. Any custom metric an alarm reads must stay on
        the allowlist, or the alarm goes quiet without ever going red."""
        template = TEMPLATE.read_text(encoding="utf-8")

        # Metric names sitting under `Namespace: fplBot` in an alarm.
        alarmed = set(
            re.findall(
                r"Namespace:\s*fplBot\s*\n\s*MetricName:\s*(\w+)",
                template,
            )
        )

        assert alarmed, "expected to find alarms on the fplBot namespace"
        missing = alarmed - set(ALARMED_METRICS)
        assert not missing, (
            f"{missing} are alarmed on but not in ALARMED_METRICS, so the alarm "
            f"would sit in INSUFFICIENT_DATA rather than firing"
        )

    def test_the_allowlist_does_not_carry_metrics_nothing_alarms_on(self) -> None:
        """The allowlist is the paid-for set, so it should not drift upwards."""
        template = TEMPLATE.read_text(encoding="utf-8")
        alarmed = set(re.findall(r"Namespace:\s*fplBot\s*\n\s*MetricName:\s*(\w+)", template))

        assert set(ALARMED_METRICS) == alarmed

    def test_the_allowlist_stays_inside_the_free_tier(self) -> None:
        """Ten custom metrics are free. Undimensioned, these are one each."""
        assert len(ALARMED_METRICS) <= 10


class TestGating:
    @pytest.mark.parametrize(
        "name",
        [Metric.SCHEMA_DRIFT_DETECTED, Metric.INVARIANT_VIOLATED, Metric.ODDS_CREDITS_REMAINING],
    )
    def test_alarmed_metrics_publish(self, name: str) -> None:
        assert _should_publish(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            Metric.SOURCE_FETCH_OK,
            Metric.SOURCE_FETCH_FAILED,
            # Dimensioned by source, so this one alone was ~6 billable metrics.
            Metric.SOURCE_FETCH_DURATION,
            Metric.PLAYERS_RESOLVED_EXACT,
            Metric.NOTIFICATION_SENT,
        ],
    )
    def test_unalarmed_metrics_are_demoted(self, name: str) -> None:
        assert _should_publish(name) is False

    def test_emitting_a_demoted_metric_does_not_raise(self) -> None:
        """It has to be a no-op from the caller's point of view - every call site
        stays unchanged, which is what makes the switch reversible."""
        from fplbot.observability import count, timed

        count(Metric.SOURCE_FETCH_OK, source="fpl")
        with timed(Metric.SOURCE_FETCH_DURATION, source="fpl"):
            pass
