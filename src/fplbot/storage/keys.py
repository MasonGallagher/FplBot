"""DynamoDB key construction, in one place.

Single-table design: one table, five item types, distinguished by the `pk`
prefix. This is standard DynamoDB practice and it is worth understanding *why*
rather than treating it as a convention.

DynamoDB charges and scales per table. Five tables means five sets of alarms,
five capacity settings, five IAM statements. More importantly, a single table
lets us fetch related items in one `Query` - all snapshots for a season, all
aliases for a source - because they share a partition key. The prefix (`SNAP#`,
`PLAYER#`, ...) is what keeps the namespaces from colliding.

    | Purpose                        | pk                          | sk           |
    |--------------------------------|-----------------------------|--------------|
    | Bootstrap snapshot (gzipped)   | SNAP#{season}               | {iso8601}    |
    | Per-player series (watchlist)  | PLAYER#{season}#{element}   | {iso8601}    |
    | Notification idempotency lock  | NOTIFY#{season}#{gw}#{tier} | LOCK         |
    | Last-known-good pointer        | LKG#{season}                | {source}     |
    | Resolved id alias cache        | ALIAS#{source}              | {source_id}  |

Sort keys are ISO-8601 timestamps for the time series, which gives us range
queries ("everything since yesterday") for free, because ISO-8601 sorts
lexicographically in the same order it sorts chronologically. That property is
the entire reason to prefer it over any other timestamp format here.
"""

from __future__ import annotations

from datetime import UTC, datetime


def iso_now() -> str:
    """UTC, second precision, always with a 'Z'.

    Second precision is deliberate: hourly snapshots do not need microseconds,
    and a fixed-width key is easier to read in the console and to reason about
    in range queries.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Keys:
    """Key builders. Never construct these strings by hand at a call site."""

    # -- bootstrap snapshots ------------------------------------------------
    @staticmethod
    def snapshot_pk(season: str) -> str:
        return f"SNAP#{season}"

    @staticmethod
    def snapshot_sk(timestamp: str | None = None) -> str:
        return timestamp or iso_now()

    # -- per-player time series --------------------------------------------
    @staticmethod
    def player_pk(season: str, element_id: int) -> str:
        return f"PLAYER#{season}#{element_id}"

    @staticmethod
    def player_sk(timestamp: str | None = None) -> str:
        return timestamp or iso_now()

    # -- notification idempotency ------------------------------------------
    @staticmethod
    def notify_pk(season: str, gameweek: int, tier: str) -> str:
        """Keyed on gameweek and tier, NEVER on wall-clock time.

        This is what makes Scheduler retries, manual re-invocations and
        at-least-once delivery all safe: whatever causes a second run for the
        same (gameweek, tier), the conditional write fails and no duplicate
        email goes out. Keying on a timestamp would defeat the entire purpose,
        because a retry has a different timestamp. SPEC section 3.
        """
        return f"NOTIFY#{season}#{gameweek}#{tier}"

    NOTIFY_SK = "LOCK"

    # -- last known good ----------------------------------------------------
    @staticmethod
    def lkg_pk(season: str) -> str:
        return f"LKG#{season}"

    @staticmethod
    def lkg_sk(source: str) -> str:
        return source

    # -- id alias cache -----------------------------------------------------
    @staticmethod
    def alias_pk(source: str) -> str:
        """Keyed by the *source's own stable id*, not by name.

        PremierInjuries gives every player a `data-id` that persists across
        pages and seasons; Understat likewise. Caching against those means each
        player is fuzzy-matched once, ever - which is what keeps the blast radius
        of the fuzzy layer small. SPEC section 4.7 step 2.
        """
        return f"ALIAS#{source}"

    @staticmethod
    def alias_sk(source_id: str) -> str:
        return str(source_id)
