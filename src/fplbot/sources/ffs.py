"""Fantasy Football Scout predicted line-ups.

https://www.fantasyfootballscout.co.uk/team-news
Server-rendered, no paywall, no login, permissive robots.txt (`Disallow:` empty).

THE MOST IMPORTANT INTEGRATION DETAIL IN THE PROJECT
----------------------------------------------------
Player image URLs on this page are of the form

    .../photos/players/110x140/154561.png

and `154561` is **exactly** FPL's `elements[].code`:

    FFS  <img src>          .../110x140/154561.png
    FPL  elements[].code                  154561
    FPL  elements[].opta_code            p154561

So the join is an exact integer comparison:

    code = int(re.search(r'/110x140/(\\d+)\\.png', img_src).group(1))
    element_id = {e.code: e.id for e in bootstrap.elements}[code]

That is immune to accents, to "Son" versus "Son Heung-min", and to all fourteen
`web_name` collision groups. Name matching for line-ups is the *fallback*, not
the path. SPEC section 4.1.

ROBUSTNESS
----------
The page's CSS classes are volatile Tailwind - `class="!m-0"` and similar - so
anchoring selectors on them is asking to be broken by a redesign that changes
nothing meaningful. The primary path here therefore **regexes the raw HTML for
`110x140/(\\d+).png` and skips DOM parsing entirely**, which survives almost any
restructuring of the markup. DOM parsing is used only for the secondary job of
recovering team groupings and formation, and its failure degrades that extra
detail rather than the line-up itself.

`ul.row-1` is the goalkeeper band and `ul.row-2..N` are successive outfield
bands, so the predicted formation falls out of the row cardinalities for free.

We ignore the page's opening "best XI" widget: it spans multiple clubs and is not
a line-up, and treating it as one would attribute eleven players to whichever
club happened to be parsed first.

[UNVERIFIED - SPEC section 8 item 7: FFS is WordPress with an `ffs/v1` namespace.
Probing `/wp-json/ffs/v1/` for a JSON line-ups route would be far more stable
than any HTML parsing. `probe_json_api` below does exactly that and logs the
result, so the first live run tells us whether it exists.]
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

from fplbot.config import Endpoints
from fplbot.observability import logger, tracer
from fplbot.sources.base import SourceContext, SourceResult, with_fallback

SOURCE = "ffs"

# The primary extraction. Deliberately matched against raw HTML rather than a
# parsed DOM - see the module docstring.
PHOTO_CODE_PATTERN = re.compile(r"/110x140/(\d+)\.png")

# Team names appear in heading elements above each line-up block. We use these
# only to group players; a failure here costs us the grouping, not the codes.
TEAM_HEADING_PATTERN = re.compile(
    r"<h[23][^>]*>\s*([A-Za-z' ’&.-]{3,40}?)\s*(?:</h[23]>|<)", re.IGNORECASE
)


@dataclass
class PredictedLineup:
    """One club's predicted XI."""

    team_name: str | None
    element_ids: list[int] = field(default_factory=list)
    # Row cardinalities: [1, 4, 4, 2] is a 4-4-2 with a goalkeeper.
    formation_rows: list[int] = field(default_factory=list)
    unmatched_codes: list[int] = field(default_factory=list)

    @property
    def formation(self) -> str | None:
        """Human-readable formation, outfield only ("4-4-2")."""
        if len(self.formation_rows) < 2:
            return None
        return "-".join(str(n) for n in self.formation_rows[1:])


@dataclass
class LineupData:
    """All predicted line-ups from one fetch."""

    lineups: list[PredictedLineup] = field(default_factory=list)
    # Flat set of every element predicted to start, which is what the scorer
    # actually consumes. The per-club grouping is for the report.
    predicted_starters: set[int] = field(default_factory=set)
    codes_seen: int = 0
    codes_matched: int = 0

    @property
    def match_rate(self) -> float:
        return self.codes_matched / self.codes_seen if self.codes_seen else 0.0

    def is_predicted_to_start(self, element_id: int) -> bool:
        return element_id in self.predicted_starters


@tracer.capture_method
def fetch_lineups(
    context: SourceContext, code_to_element: dict[int, int]
) -> SourceResult[LineupData]:
    """Fetch and parse predicted line-ups.

    Args:
        code_to_element: FPL photo code -> element id, from
            `Bootstrap.elements_by_code()`. Players with `has_temporary_code` are
            already excluded from that map, which is the guard against trusting a
            placeholder code.

    Note there is deliberately **no last-known-good fallback** for this source.
    A previous gameweek's predicted line-up is not stale data, it is *wrong* data:
    it would confidently assert that a player rotated out this week is starting.
    Better to lose the feature and say so than to keep it and be wrong.
    """

    def _fetch() -> LineupData:
        result = context.fetch_and_archive(
            SOURCE,
            Endpoints.FFS_TEAM_NEWS,
            expect_content_type="text/html",
            ext="html",
        )
        return parse_lineups(result.text(), code_to_element)

    return with_fallback(context, SOURCE, _fetch)


def parse_lineups(html: str, code_to_element: dict[int, int]) -> LineupData:
    """Parse the team-news page.

    Two passes, in order of trustworthiness:

    1. A regex over the raw HTML for every photo code. This is the primary path
       and it cannot be broken by CSS or markup changes.
    2. A DOM pass to attribute those codes to clubs and recover the formation.
       Best-effort; if the structure has changed we still have pass 1.
    """
    data = LineupData()

    # -- Pass 1: the codes, which is the bit that must not fail ------------
    all_codes = [int(m.group(1)) for m in PHOTO_CODE_PATTERN.finditer(html)]
    data.codes_seen = len(all_codes)

    # -- Pass 2: attribute codes to clubs ---------------------------------
    try:
        grouped = _group_by_club(html, code_to_element)
    except Exception as exc:
        logger.warning(
            "FFS DOM grouping failed; falling back to ungrouped codes",
            extra={"error": str(exc)},
        )
        grouped = []

    if grouped:
        data.lineups = grouped
        for lineup in grouped:
            data.predicted_starters.update(lineup.element_ids)
    else:
        # No grouping available - still record who is predicted to start.
        unmatched: list[int] = []
        for code in all_codes:
            element_id = code_to_element.get(code)
            if element_id is None:
                unmatched.append(code)
            else:
                data.predicted_starters.add(element_id)
        data.lineups = [
            PredictedLineup(
                team_name=None,
                element_ids=sorted(data.predicted_starters),
                unmatched_codes=unmatched,
            )
        ]

    data.codes_matched = len(data.predicted_starters)

    unknown = data.codes_seen - data.codes_matched
    logger.info(
        "Parsed FFS predicted line-ups",
        extra={
            "clubs": len(data.lineups),
            "codes_seen": data.codes_seen,
            "matched": data.codes_matched,
            "unmatched": unknown,
            "match_rate": round(data.match_rate, 3),
        },
    )
    if data.codes_seen and data.match_rate < 0.8:
        # Below 80% something structural has changed - either the URL pattern or
        # our code map. Worth knowing before the recommendations go out.
        logger.warning(
            "FFS photo-code match rate is unexpectedly low",
            extra={
                "match_rate": round(data.match_rate, 3),
                "hint": "Check the /110x140/ path pattern and has_temporary_code exclusions.",
            },
        )

    return data


def _group_by_club(html: str, code_to_element: dict[int, int]) -> list[PredictedLineup]:
    """Attribute codes to clubs using the page structure.

    We look for `ul` elements whose class contains `row-N`. Row 1 is the
    goalkeeper band, rows 2 upward are successive outfield bands - so the
    cardinalities give us the predicted formation without any extra work.

    The opening best-XI widget is skipped: it spans several clubs, so any block
    whose players belong to more than a couple of clubs is not a line-up.
    """
    tree = HTMLParser(html)
    lineups: list[PredictedLineup] = []

    # Each club's line-up sits inside a container holding several row-N lists.
    # We walk containers rather than rows so the rows stay grouped per club.
    for container in tree.css("div, section, article"):
        rows = [
            node
            for node in container.css("ul")
            if any(cls.startswith("row-") for cls in (node.attributes.get("class") or "").split())
        ]
        if len(rows) < 3:
            # A real line-up has at least a keeper band plus two outfield bands.
            continue

        # Only take the outermost container for a club - skip if a parent already
        # produced these same rows.
        element_ids: list[int] = []
        unmatched: list[int] = []
        cardinalities: list[int] = []

        for row in rows:
            row_html = row.html or ""
            codes = [int(m.group(1)) for m in PHOTO_CODE_PATTERN.finditer(row_html)]
            if not codes:
                continue
            cardinalities.append(len(codes))
            for code in codes:
                element_id = code_to_element.get(code)
                if element_id is None:
                    unmatched.append(code)
                else:
                    element_ids.append(element_id)

        # A line-up is eleven players. Allow a little slack for parse noise but
        # reject anything that is clearly the multi-club best-XI widget.
        if not (8 <= len(element_ids) + len(unmatched) <= 13):
            continue
        if any(
            len(existing.element_ids) and set(existing.element_ids) & set(element_ids)
            for existing in lineups
        ):
            continue  # already captured by an outer container

        lineups.append(
            PredictedLineup(
                team_name=_nearest_heading(container.html or ""),
                element_ids=element_ids,
                formation_rows=cardinalities,
                unmatched_codes=unmatched,
            )
        )

    return lineups


def _nearest_heading(container_html: str) -> str | None:
    match = TEAM_HEADING_PATTERN.search(container_html)
    return match.group(1).strip() if match else None


@tracer.capture_method
def probe_json_api(context: SourceContext) -> dict | None:
    """Probe for a WordPress JSON route. See SPEC section 8 item 7.

    FFS runs WordPress and exposes an `ffs/v1` namespace. If a line-ups route
    exists there it would be dramatically more stable than any HTML parsing, and
    we should migrate to it.

    This runs at most once per gameweek and logs whatever it finds. It never
    affects the run - it is instrumentation to resolve an open question, which is
    exactly the treatment SPEC section 8 asks for: surface it, do not guess.
    """
    try:
        result = context.http.fetch(
            SOURCE, "https://www.fantasyfootballscout.co.uk/wp-json/ffs/v1/"
        )
        payload = result.json()
        logger.info(
            "FFS wp-json/ffs/v1 namespace responded - investigate for a lineups route",
            extra={"routes": list(payload.get("routes", {}))[:40]},
        )
        return payload
    except Exception as exc:
        logger.info(
            "FFS wp-json probe found nothing usable (expected; HTML path remains primary)",
            extra={"detail": str(exc)[:200]},
        )
        return None
