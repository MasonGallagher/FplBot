"""PremierInjuries - injuries and suspensions.

https://www.premierinjuries.com/injury-table.php
robots.txt is explicitly permissive (`Disallow:` empty). Verified current for the
2026/27 season, unlike PhysioRoom whose live table still lists last season's
clubs and is therefore excluded entirely.

FOUR PARSING TRAPS, ALL LIVE
----------------------------
1. **Rows are flat siblings, not nested.** A `tr.heading[data-team-id]` opens a
   team and `tr.player-row.team_{id}` rows follow it. We key off the
   `team_{id}` class rather than document order, so each row is self-describing
   and a stray element between them cannot silently reassign a player to the
   wrong club.

2. **Every `<td>` is prefixed by `<div class="mob-title">Label</div>`** - a
   mobile-layout label. Taking `.text()` naively yields `"PlayerRyan Christie"`
   for every field. The label node must be removed first. This is the single
   most likely thing to go wrong here, and it fails *quietly*: you get plausible
   strings that are subtly wrong.

3. **`Potential Return` is DD/MM/YYYY - day first.** Parsing it as US month-first
   silently corrupts every date with a day of 12 or lower, which is roughly 40%
   of them, and the corrupted dates are still valid dates. Nothing will throw.

4. **`Reason` includes "Suspended".** Bans ride in the same table as injuries.
   A suspension has a deterministic end date; modelling its "return probability"
   as though it were a fitness question is simply the wrong model.

CONTROLLED VOCABULARIES
-----------------------
    Status    -> "Ruled Out" | "25%" | "50%" | "75%"
    Condition -> "Currently Being Assessed" | "Not Available"

Both are closed sets and both are asserted. An unrecognised value is a semantic
change, not a parse error, and we would much rather map it by hand than let it
fall through to a default of "fit".

`Currently Being Assessed` is the load-bearing one: it is the "a press conference
will resolve this" flag, and it defines the Phase 2 re-check set. On the live
table 23 of 44 listed players carry it - more than half. That is why a single
48-hour run is not enough. SPEC sections 3 and 4.3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from selectolax.parser import HTMLParser

from fplbot.config import Endpoints
from fplbot.domain.invariants import check_injury_vocabulary
from fplbot.observability import logger, tracer
from fplbot.sources.base import SourceContext, SourceResult, with_fallback

SOURCE = "premierinjuries"

TEAM_CLASS_PATTERN = re.compile(r"\bteam_(\d+)\b")


@dataclass
class InjuryRecord:
    """One row of the injury table."""

    # PremierInjuries' own stable player id, from `a.track[data-id]`. This is the
    # anchor for the alias cache: resolve a player to an FPL element id once,
    # store it against this id, and never fuzzy-match them again.
    source_id: str
    name: str  # from data-name, which is clean and unpolluted
    team_id: str | None  # PremierInjuries' team id, not FPL's
    team_name: str | None
    status: str | None  # Ruled Out | 25% | 50% | 75%
    condition: str | None  # Currently Being Assessed | Not Available
    reason: str | None  # includes "Suspended"
    potential_return: date | None
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def is_suspension(self) -> bool:
        return (self.reason or "").strip().lower() == "suspended"

    @property
    def awaiting_press_conference(self) -> bool:
        """The Phase 2 re-check flag."""
        return self.condition == "Currently Being Assessed"

    @property
    def chance_of_playing(self) -> int:
        """Map Status onto FPL's 0-100 scale.

        The mapping is close to 1:1 with `chance_of_playing_next_round`, which
        makes cross-validation between the two sources cheap and meaningful:
        agreement should raise confidence sharply, disagreement should lower it
        and be surfaced rather than silently resolved. SPEC section 5.3 step 4.
        """
        return {
            "Ruled Out": 0,
            "25%": 25,
            "50%": 50,
            "75%": 75,
        }.get(self.status or "", 100)


@dataclass
class InjuryData:
    records: list[InjuryRecord] = field(default_factory=list)
    statuses_seen: set[str] = field(default_factory=set)
    conditions_seen: set[str] = field(default_factory=set)

    @property
    def awaiting_press_conference(self) -> list[InjuryRecord]:
        """The set Phase 2 exists to re-check."""
        return [r for r in self.records if r.awaiting_press_conference]

    def by_source_id(self) -> dict[str, InjuryRecord]:
        return {r.source_id: r for r in self.records}


@tracer.capture_method
def fetch_injuries(context: SourceContext) -> SourceResult[InjuryData]:
    """Fetch and parse the injury table.

    Last-known-good *is* enabled here, unlike predicted line-ups. An injury
    listing from six hours ago is still largely true - injuries resolve over days,
    not minutes - so stale data plus a clear label beats losing the feature.
    """

    def _fetch() -> InjuryData:
        result = context.fetch_and_archive(
            SOURCE,
            Endpoints.PREMIER_INJURIES,
            expect_content_type="text/html",
            ext="html",
        )
        data = parse_injury_table(result.text())
        check_injury_vocabulary(data.statuses_seen, data.conditions_seen, context.quality)
        return data

    return with_fallback(
        context,
        SOURCE,
        _fetch,
        serialise=_serialise,
        deserialise=_deserialise,
    )


def parse_injury_table(html: str) -> InjuryData:
    """Parse the flat sibling-row structure into records."""
    tree = HTMLParser(html)
    data = InjuryData()

    # Team id -> name, from the heading rows.
    team_names: dict[str, str] = {}
    for heading in tree.css("tr.heading"):
        team_id = heading.attributes.get("data-team-id")
        if team_id:
            # Read the name cell, NOT the whole row. The heading also carries a
            # "track this team" control:
            #
            #   <div class="injury-table-th">
            #     <div class="injury-team">AFC Bournemouth</div>
            #     <div class="table-actions">... TRACK ...</div>
            #
            # so the row's text is "AFC BournemouthTRACK1". That resolved to no
            # FPL team for ANY club, which silently dropped the team hint from
            # every injury row - and the team hint is what keeps player matching
            # from having to guess between similar names at different clubs.
            #
            # `.injury-team` first, with the generic cleaner as a fallback that
            # now also strips `.table-actions`, so a markup change costs accuracy
            # rather than correctness.
            name_node = heading.css_first("div.injury-team") or heading
            team_names[team_id] = _clean_text(name_node)

    # The column order is defined by the repeated `tr.sub-head` row. We build a
    # label -> index map from it rather than hardcoding indices, so a new column
    # inserted upstream shifts our reads automatically instead of silently
    # putting the Reason into the Status field.
    column_index = _build_column_map(tree)
    if not column_index:
        logger.warning(
            "Could not find the PremierInjuries sub-head row; falling back to "
            "positional column indices"
        )
        column_index = {
            "player": 0,
            "status": 1,
            "condition": 2,
            "reason": 3,
            "potential return": 4,
        }

    for row in tree.css("tr.player-row"):
        record = _parse_row(row, column_index, team_names)
        if record is not None:
            data.records.append(record)
            if record.status:
                data.statuses_seen.add(record.status)
            if record.condition:
                data.conditions_seen.add(record.condition)

    logger.info(
        "Parsed injury table",
        extra={
            "records": len(data.records),
            "awaiting_press_conference": len(data.awaiting_press_conference),
            "suspensions": sum(1 for r in data.records if r.is_suspension),
        },
    )
    return data


def _build_column_map(tree: HTMLParser) -> dict[str, int]:
    """Read the schema off the `tr.sub-head` row.

    Parsing the header rather than hardcoding indices is the difference between
    "PremierInjuries added a column and our Reason field became the return date"
    and "nothing happened".
    """
    sub_head = tree.css_first("tr.sub-head")
    if sub_head is None:
        return {}
    mapping: dict[str, int] = {}
    for index, cell in enumerate(sub_head.css("td, th")):
        label = _clean_text(cell).lower().strip()
        if label:
            mapping[label] = index
    return mapping


def _parse_row(
    row, column_index: dict[str, int], team_names: dict[str, str]
) -> InjuryRecord | None:
    classes = row.attributes.get("class") or ""
    team_match = TEAM_CLASS_PATTERN.search(classes)
    team_id = team_match.group(1) if team_match else None

    # The anchor carries both the stable id and a clean name. Both are far more
    # trustworthy than the cell text, which is polluted by the mobile labels.
    anchor = row.css_first('a.track[data-type="player"]') or row.css_first("a.track")
    if anchor is None:
        return None

    source_id = anchor.attributes.get("data-id")
    if not source_id:
        return None
    name = (anchor.attributes.get("data-name") or _clean_text(anchor)).strip()

    cells = row.css("td")
    values = {label: _cell_value(cells, index) for label, index in column_index.items()}

    return InjuryRecord(
        source_id=source_id,
        name=name,
        team_id=team_id,
        team_name=team_names.get(team_id or ""),
        status=values.get("status") or None,
        condition=values.get("condition") or None,
        reason=values.get("reason") or None,
        potential_return=parse_uk_date(values.get("potential return", "")),
        raw={k: v for k, v in values.items() if v},
    )


def _cell_value(cells: list, index: int) -> str:
    if index >= len(cells):
        return ""
    return _clean_text(cells[index])


def _clean_text(node) -> str:
    """Text of a node with the `mob-title` label stripped.

    THE trap in this source. Every `<td>` is prefixed with
    `<div class="mob-title">Label</div>` for the mobile layout, so a naive
    `.text()` returns `"PlayerRyan Christie"` and `"StatusRuled Out"`. Removing
    the node first is the whole fix, and forgetting it produces plausible-looking
    but wrong values rather than an error.
    """
    if node is None:
        return ""
    for label in node.css("div.mob-title"):
        label.decompose()
    # Interactive controls carry visible text ("TRACK") that is chrome, not data.
    for actions in node.css("div.table-actions"):
        actions.decompose()
    return " ".join((node.text() or "").split())


def parse_uk_date(value: str) -> date | None:
    """Parse a DD/MM/YYYY date. **Day first.**

    Parsing this as month-first corrupts every date whose day is 12 or lower -
    roughly 40% of them - and produces another perfectly valid date, so nothing
    throws and nothing looks wrong. A player due back on 05/09 becomes due back
    on 09/05, and the bot recommends buying him four months early.

    We try day-first formats only. If a value does not match, we return None
    rather than guessing, because a missing return date is far less harmful than
    a confidently wrong one.
    """
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    logger.debug("Unparseable potential-return date", extra={"value": text})
    return None


# ---------------------------------------------------------------------------
# Last-known-good round trip
# ---------------------------------------------------------------------------
def _serialise(data: InjuryData) -> dict:
    return {
        "records": [
            {
                **vars(record),
                "potential_return": (
                    record.potential_return.isoformat() if record.potential_return else None
                ),
            }
            for record in data.records
        ],
        "statuses_seen": sorted(data.statuses_seen),
        "conditions_seen": sorted(data.conditions_seen),
    }


def _deserialise(payload: dict) -> InjuryData:
    records = []
    for row in payload.get("records", []):
        row = dict(row)
        raw_date = row.pop("potential_return", None)
        records.append(
            InjuryRecord(**row, potential_return=date.fromisoformat(raw_date) if raw_date else None)
        )
    return InjuryData(
        records=records,
        statuses_seen=set(payload.get("statuses_seen", [])),
        conditions_seen=set(payload.get("conditions_seen", [])),
    )
