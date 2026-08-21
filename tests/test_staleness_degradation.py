"""One broken optional source must not withhold the board.

Written after GW1: ClubElo had been timing out unnoticed for three weeks, its
cached copy was 544 hours old, and the run refused to advise - discarding a board
built from six healthy sources, 2.4 hours before the deadline.

Two faults, both of which only surface when it is too late to matter:

1. `worst_age_seconds` maxed over EVERY source, so an optional one dragged the
   run over the gate even though `base.py` documents that only FPL is required
   and "everything else degrades a feature".
2. `_send_failure` kept the tier lock, so the transient outage became a
   permanently missed gameweek - the next run would find the tier claimed and
   exit `suppressed` even once the source recovered.
"""

from __future__ import annotations

from fplbot.config import HARD_STALENESS_CEILING_SECONDS
from fplbot.models.domain import DataQuality, SourceState

HOUR = 3600


class TestStalenessGate:
    def test_a_stale_optional_source_does_not_set_the_worst_age(self) -> None:
        """The GW1 failure. A source dropped for being too old reports no age,
        so it cannot drag the run over the gate."""
        quality = DataQuality()
        quality.record("fpl", SourceState.OK, age_seconds=120)
        # Discarded rather than served: FAILED, and crucially no age.
        quality.record("clubelo_fixtures", SourceState.FAILED, "cached copy discarded")

        assert quality.worst_age_seconds < HARD_STALENESS_CEILING_SECONDS

    def test_fresh_cached_data_is_still_counted(self) -> None:
        """Degrading to a recent cache is fine and must still be visible - the
        point is to drop data past the ceiling, not to stop tracking age."""
        quality = DataQuality()
        quality.record("fpl", SourceState.OK, age_seconds=60)
        quality.record("clubelo_fixtures", SourceState.DEGRADED, "cached", age_seconds=2 * HOUR)

        assert quality.worst_age_seconds == 2 * HOUR
        assert quality.worst_age_seconds < HARD_STALENESS_CEILING_SECONDS

    def test_genuinely_stale_required_data_still_blocks(self) -> None:
        """The gate must keep working. If FPL itself is ancient there is no
        fallback and refusing to advise is correct."""
        quality = DataQuality()
        quality.record("fpl", SourceState.DEGRADED, "cached", age_seconds=30 * HOUR)

        assert quality.worst_age_seconds > HARD_STALENESS_CEILING_SECONDS

    def test_a_failed_source_is_named_as_degraded(self) -> None:
        """Dropping the data must not hide it - the caveat is the only reason
        this class of silent failure is ever noticed."""
        quality = DataQuality()
        quality.record("fpl", SourceState.OK, age_seconds=60)
        quality.record("clubelo_fixtures", SourceState.FAILED, "discarded")

        assert "clubelo_fixtures" in quality.degraded_sources
        assert "clubelo_fixtures" in quality.summary_line()
