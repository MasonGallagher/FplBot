"""Base model with the drift policy baked in.

SPEC section 6.3 is emphatic, and it is worth restating the reasoning because it
runs against the usual instinct to validate strictly.

FPL adds fields mid-season without notice. `can_transact`, `price_change_percent`
and `scout_news_link` are all recent additions. Under `extra="forbid"` - the
setting a careful engineer reaches for by default - the first of those additions
would have hard-failed every parse and taken the bot off the air three hours
before a deadline, because someone at FPL added a harmless field.

So: `extra="allow"`, and require only the fields whose absence genuinely breaks
scoring. Everything else is optional with a default.

But "tolerate" is not "ignore". We record every unexpected field as a metric,
once per field per container. That metric is the early-warning system: a new
field is very often the visible edge of a semantic change to an existing one,
and knowing about it a week early is the difference between noticing and being
quietly wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from fplbot.observability import report_schema_drift


class DriftTolerantModel(BaseModel):
    """Accepts unknown fields, reports them, and never fails because of them."""

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        # Upstream sends strings where we want numbers all over the place.
        # Letting pydantic coerce is not laziness - it is the *point*. Hand-rolled
        # float() calls are where the NoneType/'' crashes come from.
        coerce_numbers_to_str=False,
        str_strip_whitespace=True,
        frozen=False,
    )

    @model_validator(mode="after")
    def _report_extras(self) -> DriftTolerantModel:
        extras = self.__pydantic_extra__ or {}
        if extras:
            for name, value in extras.items():
                report_schema_drift(type(self).__name__, name, value)
        return self


# ---------------------------------------------------------------------------
# Shared coercion helpers
# ---------------------------------------------------------------------------
# These exist because FPL's typing is inconsistent in three specific, documented
# ways, and each of them has a wrong-looking-but-correct handling.


def parse_optional_float(value: Any) -> float | None:
    """Coerce FPL's string-encoded floats. `''` and None both become None.

    `expected_goals`, `expected_assists`, `selected_by_percent`, `form`,
    `points_per_game`, `ict_index`, `ep_this` and `ep_next` all arrive as
    **strings**. Their `*_per_90` counterparts arrive as **floats**. Same payload,
    same concept, different types.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_chance_of_playing(value: Any) -> int:
    """Convert `chance_of_playing_*` to a percentage.

    The trap: FPL uses `''` (an empty string) for a fully fit player, **not
    null**. Naively doing `value or 0` therefore marks every healthy player as
    0% likely to play - a bug that would invert the entire recommendation set
    while looking like reasonable defensive coding.

    None is also treated as 100: `chance_of_playing_this_round` is null for every
    player outside a live gameweek, which is most of the time we run.
    """
    if value is None or value == "":
        return 100
    try:
        return int(value)
    except (TypeError, ValueError):
        return 100


# FPL is not internally consistent about timestamp precision: `news_added` has
# microseconds, `deadline_time` does not. A shared parser has to accept both, so
# we try each format in turn rather than assuming one.
_TIMESTAMP_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%fZ",  # news_added
    "%Y-%m-%dT%H:%M:%SZ",  # deadline_time, kickoff_time
    "%Y-%m-%dT%H:%M:%S%z",
)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an FPL timestamp in any of the formats FPL actually emits.

    Used for *display and recency*, never for deadline arithmetic. All deadline
    maths goes through `deadline_time_epoch`, which is an integer, which means
    no timezone handling and therefore no DST bugs. SPEC section 3.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value)
    for fmt in _TIMESTAMP_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    # ISO-8601 with an offset, as a last resort.
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None
