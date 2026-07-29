"""Rendering the board into HTML and plain text.

SPEC section 6.1 fixes the section order, and each section earns its place:

    1. Header          gameweek, deadline, hours left, PHASE, data quality
    2. Buy board       ranked by position, with xP / ceiling / floor / price /
                       ownership / risk / confidence / why / runner-up
    3. Sell or avoid   with the evidence that triggered each entry
    4. Injury watchlist transfer-flow anomalies not yet in FPL's news
    5. Returning       the buy-low window
    6. Caveats         unresolved assessments, stale sources, unmatched players

The governing principle: **every recommendation must be legible enough to argue
with.** The user intends to compete against this bot. A pick with no "why" is a
pick they cannot reason about, which defeats the entire purpose of sending it.

No templating engine. Jinja2 would be another dependency in a 250 MB budget for
one document whose structure never changes, and f-strings with a couple of helper
functions are perfectly readable at this size. Everything user-facing goes
through `esc()` - the data comes from third-party HTML pages, and a player's
`news` field containing a stray angle bracket should not be able to break the
layout.

---------------------------------------------------------------------------
ON THE EMAIL HTML
---------------------------------------------------------------------------
Four constraints shape every styling decision here, and none of them are
preferences:

* **Inline styles on tables.** Email clients strip `<style>` blocks with
  enthusiasm. The `<style>` block below is progressive enhancement only - it
  carries the mobile breakpoint and nothing the layout depends on. Every rule
  that matters is also inline.

* **Tables for layout, not divs.** Outlook renders through Word's HTML engine,
  which has no meaningful float or flexbox support. A centred card is a table
  with `align="center"`, and that is simply the way it is done.

* **`color-scheme: light only`.** Gmail and Outlook.com auto-invert dark-mode
  messages, and their inversion is naive: it flips backgrounds but mangles the
  subtle greys this report leans on for hierarchy, so the muted secondary text
  ends up nearly the same value as the primary. Pinning to light renders this
  document identically everywhere, which for a data-dense table is worth more
  than honouring a theme preference.

* **A preheader.** The hidden span after `<body>` is what the inbox list shows
  next to the subject. Without one, clients scrape the first visible text and
  the preview reads as a fragment of the header. It is the second thing read
  after the subject and the cheapest professional detail available.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

from fplbot.domain.ranking import Board
from fplbot.models.domain import (
    Confidence,
    DataQuality,
    PlayerScore,
    Recommendation,
    RunContext,
)

# Palette. Muted deliberately - a board full of red and green is unreadable, and
# colour should mark the exceptions rather than the norm. The original keys are
# retained; the rest extend the same restraint to surfaces and soft fills.
COLOURS = {
    # Original keys.
    "text": "#0f172a",
    "muted": "#64748b",
    "border": "#e2e8f0",
    "header_bg": "#f8fafc",
    "provisional": "#b45309",  # amber - phase 1
    "confirmed": "#15803d",  # green - phase 2, act on this
    "danger": "#b91c1c",
    "warning": "#a16207",
    "accent": "#4f46e5",
    # Extensions.
    "body": "#334155",
    "faint": "#94a3b8",
    "line_soft": "#eef2f7",
    "canvas": "#eef1f6",
    "surface": "#ffffff",
    "masthead": "#111827",
    "masthead_muted": "#9ca3af",
    "accent_soft": "#eef2ff",
    "confirmed_soft": "#dcfce7",
    "provisional_soft": "#fef3c7",
    "danger_soft": "#fee2e2",
}

CONFIDENCE_COLOUR = {
    Confidence.HIGH: "#15803d",
    Confidence.MEDIUM: "#a16207",
    Confidence.LOW: "#b91c1c",
}

POSITION_NAMES = {
    "GKP": "Goalkeepers",
    "DEF": "Defenders",
    "MID": "Midfielders",
    "FWD": "Forwards",
}

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"

# `tabular-nums` keeps decimal points in a column of xP figures vertically
# aligned. Unsupported clients ignore it, so it costs nothing.
NUM = f"font-family:{FONT};font-variant-numeric:tabular-nums;"

CARD_WIDTH = 680


def esc(value: object) -> str:
    """HTML-escape anything on its way into the document."""
    return html.escape(str(value), quote=True)


def format_deadline(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%a %d %b %Y, %H:%M UTC")


# ---------------------------------------------------------------------------
# Small presentational helpers
# ---------------------------------------------------------------------------
def _pill(text: str, *, fg: str, bg: str) -> str:
    """A rounded badge. Used for TEMPLATE / DIFFERENTIAL / captain markers."""
    return (
        f'<span style="display:inline-block;background:{bg};color:{fg};'
        f"padding:2px 7px;border-radius:10px;font-size:10px;font-weight:700;"
        f'letter-spacing:0.04em;white-space:nowrap;">{text}</span>'
    )


def _th(label: str, *, align: str = "left", width: str = "", title: str = "") -> str:
    """A table header cell: small, uppercase, muted - it should recede."""
    width_rule = f"width:{width};" if width else ""
    title_attr = f' title="{esc(title)}"' if title else ""
    return (
        f'<th{title_attr} style="{width_rule}text-align:{align};padding:0 10px 8px;'
        f"font-size:10px;font-weight:700;letter-spacing:0.07em;text-transform:uppercase;"
        f'color:{COLOURS["faint"]};border-bottom:1px solid {COLOURS["border"]};">{label}</th>'
    )


def _table(head: str, body: str) -> str:
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"'
        f' style="border-collapse:collapse;width:100%;font-family:{FONT};">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


def _section_heading(title: str) -> str:
    """Heading plus a short accent rule - an editorial cue that a section starts."""
    return (
        f'<h2 style="margin:34px 0 0;font-family:{FONT};font-size:17px;font-weight:700;'
        f'color:{COLOURS["text"]};letter-spacing:-0.01em;">{esc(title)}</h2>'
        f'<div style="width:34px;height:3px;background:{COLOURS["accent"]};'
        f'border-radius:2px;margin:7px 0 14px;"></div>'
    )


def _note(message: str) -> str:
    """Explanatory copy under a heading."""
    return (
        f'<p style="margin:0 0 12px;font-family:{FONT};font-size:13px;line-height:1.55;'
        f'color:{COLOURS["muted"]};">{message}</p>'
    )


def _empty(message: str) -> str:
    """The 'nothing here' state. Bordered so it reads as a deliberate answer
    rather than a section that failed to render."""
    return (
        f'<p style="margin:0;padding:14px 16px;font-family:{FONT};font-size:13px;'
        f'color:{COLOURS["muted"]};background:{COLOURS["header_bg"]};'
        f'border:1px solid {COLOURS["border"]};border-radius:8px;">{esc(message)}</p>'
    )


def _detail_line(bits: list[str]) -> str:
    """The stacked 'why / runner-up / warning' block under a player's name."""
    return (
        f'<div style="margin-top:5px;font-size:12px;line-height:1.6;'
        f'color:{COLOURS["muted"]};">{"<br>".join(bits)}</div>'
    )


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def render_html(context: RunContext, board: Board) -> str:
    """Render the full report."""
    # Captaincy sits directly under the buy board because it is a decision for
    # this gameweek and the reader acts on it in the same sitting. The wildcard
    # squad goes last before the caveats: it is a much rarer decision, and
    # burying it slightly is the honest reflection of how often it applies.
    sections = [
        _buy_board(board),
        _captain_picks(board),
        _sell_list(board),
        _watchlist(board, context),
        _returning(board),
        _wildcard_squad(board),
        _caveats(context.data_quality, context),
    ]
    body = "\n".join(sections)

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light only">
<title>fplBot GW{context.gameweek} - {esc(context.phase_label)}</title>
<style>
  /* Progressive enhancement only - the inline styles carry the layout. */
  @media only screen and (max-width:620px) {{
    .fb-pad {{ padding-left:18px !important; padding-right:18px !important; }}
    .fb-hide-sm {{ display:none !important; }}
    .fb-h1 {{ font-size:21px !important; }}
  }}
  a {{ color:{COLOURS["accent"]}; }}
</style>
</head>
<body style="margin:0;padding:0;background:{COLOURS["canvas"]};
             font-family:{FONT};color:{COLOURS["body"]};
             -webkit-font-smoothing:antialiased;">
{_preheader(context)}
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="background:{COLOURS["canvas"]};width:100%;">
  <tr><td align="center" style="padding:24px 12px;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0"
           width="{CARD_WIDTH}"
           style="width:100%;max-width:{CARD_WIDTH}px;background:{COLOURS["surface"]};
                  border:1px solid {COLOURS["border"]};border-radius:14px;overflow:hidden;">
      {_masthead(context)}
      {_status_strip(context)}
      <tr><td class="fb-pad" style="padding:4px 32px 32px;">{body}</td></tr>
      {_footer(context)}
    </table>
  </td></tr>
</table>
</body></html>"""


def _preheader(context: RunContext) -> str:
    """Hidden inbox preview text.

    The trailing entities are the standard whitespace hack: without them the
    client keeps scraping past the preheader and appends the masthead copy.
    """
    marker = "Confirmed" if context.is_confirmed_phase else "Provisional"
    text = (
        f"{marker} board for GW{context.gameweek} - "
        f"{context.hours_to_deadline:.0f}h to the deadline."
    )
    return (
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;'
        f'mso-hide:all;font-size:1px;line-height:1px;color:{COLOURS["surface"]};">'
        f"{esc(text)}" + ("&#847;&zwnj;&nbsp;" * 60) + "</div>"
    )


def _masthead(context: RunContext) -> str:
    """The dark header band: identity, gameweek, and the deadline countdown.

    Hours remaining is the single most decision-relevant number in the document,
    so it is set large and given its own column rather than being buried in a
    sentence.
    """
    hours = context.hours_to_deadline
    return f"""
    <tr><td class="fb-pad" style="background:{COLOURS["masthead"]};padding:26px 32px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
        <tr>
          <td style="vertical-align:middle;">
            <div style="font-size:10px;font-weight:700;letter-spacing:0.16em;
                        text-transform:uppercase;color:{COLOURS["masthead_muted"]};">
              fplBot &middot; Season {esc(context.season)}
            </div>
            <h1 class="fb-h1" style="margin:9px 0 0;font-size:25px;line-height:1.2;
                       font-weight:700;color:#ffffff;letter-spacing:-0.02em;">
              Gameweek {context.gameweek} transfer board
            </h1>
          </td>
          <td class="fb-hide-sm" style="vertical-align:middle;text-align:right;
                     white-space:nowrap;padding-left:16px;">
            <div style="{NUM}font-size:30px;font-weight:700;color:#ffffff;line-height:1;">
              {hours:.1f}h
            </div>
            <div style="font-size:10px;font-weight:700;letter-spacing:0.1em;
                        text-transform:uppercase;color:{COLOURS["masthead_muted"]};
                        margin-top:5px;">
              to deadline
            </div>
          </td>
        </tr>
      </table>
    </td></tr>
    """


def _status_strip(context: RunContext) -> str:
    """Phase banner and data-quality line.

    Phase 1 output is provisional and must be labelled as such: at T-48h most
    managers' press conferences have not happened, and over half the injury table
    is still "Currently Being Assessed". Phase 2 at T-3h is the one to act on.
    """
    is_confirmed = context.is_confirmed_phase
    colour = COLOURS["confirmed"] if is_confirmed else COLOURS["provisional"]
    soft = COLOURS["confirmed_soft"] if is_confirmed else COLOURS["provisional_soft"]
    label = "CONFIRMED" if is_confirmed else "PROVISIONAL"
    banner = (
        "this is the report to act on"
        if is_confirmed
        else "team news is still moving; a confirmed report follows at T-3h"
    )

    quality = context.data_quality
    degraded = bool(quality.degraded_sources)
    quality_colour = COLOURS["warning"] if degraded else COLOURS["muted"]

    return f"""
    <tr><td class="fb-pad" style="padding:18px 32px 0;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
             style="background:{soft};border-left:3px solid {colour};border-radius:6px;">
        <tr><td style="padding:11px 14px;font-size:13px;line-height:1.5;color:{colour};">
          <strong style="font-weight:700;letter-spacing:0.03em;">{label}</strong>
          <span style="color:{COLOURS["body"]};"> &mdash; {esc(banner)}</span>
        </td></tr>
      </table>
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
             style="margin-top:14px;border-top:1px solid {COLOURS["line_soft"]};">
        <tr>
          <td style="padding:12px 0 0;font-size:12px;line-height:1.6;color:{COLOURS["muted"]};">
            <strong style="color:{COLOURS["body"]};">Deadline</strong>
            {esc(format_deadline(context.deadline_epoch))}
            <span style="color:{COLOURS["faint"]};"> &middot; </span>
            <strong style="color:{quality_colour};">Data</strong>
            <span style="color:{quality_colour};">{esc(quality.summary_line())}</span>
          </td>
        </tr>
      </table>
    </td></tr>
    """


def _buy_board(board: Board) -> str:
    """Section 2. Ranked by position."""
    blocks = [_section_heading("Buy board")]

    any_rows = False
    for position, title in POSITION_NAMES.items():
        recommendations = board.buys_by_position.get(position, [])
        if not recommendations:
            continue
        any_rows = True
        rows = "\n".join(_buy_row(rec, index + 1) for index, rec in enumerate(recommendations))
        head = (
            _th("#", width="22px")
            + _th("Player")
            + _th("Price", align="right")
            + _th("xP", align="right")
            + _th("Floor / Ceiling", align="right", title="P10 - P90")
            + _th("Owned", align="right")
            + _th("Risk", align="right")
            + _th("Conf.", align="right")
        )
        blocks.append(
            f'<h3 style="margin:22px 0 10px;font-size:11px;font-weight:700;'
            f"letter-spacing:0.09em;text-transform:uppercase;"
            f'color:{COLOURS["accent"]};">{esc(title)}</h3>{_table(head, rows)}'
        )

    if not any_rows:
        blocks.append(_empty("No buy candidates passed the filters this gameweek."))
    return "\n".join(blocks)


def _buy_row(rec: Recommendation, index: int) -> str:
    score = rec.score
    cell = (
        f"padding:11px 10px;border-bottom:1px solid {COLOURS['line_soft']};vertical-align:top;"
    )
    confidence_colour = CONFIDENCE_COLOUR[rec.confidence]

    detail_bits = [f'<em style="font-style:normal;">{esc(rec.why)}</em>']
    if rec.runner_up:
        detail_bits.append(
            f'<span style="color:{COLOURS["faint"]};">Runner-up: {esc(rec.runner_up)}</span>'
        )
    for warning in rec.warnings:
        detail_bits.append(
            f'<span style="color:{COLOURS["warning"]};">&#9888;&#65039; {esc(warning)}</span>'
        )
    if score.availability.scout_news_link:
        # FPL hands us the actual club statement behind each injury, keyed to the
        # element id. Passing it straight through means the user can read the
        # primary source rather than taking our word for it.
        detail_bits.append(
            f'<a href="{esc(score.availability.scout_news_link)}" '
            f'style="color:{COLOURS["accent"]};text-decoration:none;'
            f'border-bottom:1px solid {COLOURS["accent_soft"]};">Club statement &rarr;</a>'
        )

    fixture_note = ""
    if score.fixture_count >= 2:
        fixture_note = " " + _pill(
            f"DGW &times;{score.fixture_count}",
            fg=COLOURS["confirmed"],
            bg=COLOURS["confirmed_soft"],
        )

    return f"""
    <tr>
      <td style="{cell}{NUM}color:{COLOURS["faint"]};font-size:12px;">{index}</td>
      <td style="{cell}font-size:13px;color:{COLOURS["body"]};">
        <strong style="color:{COLOURS["text"]};font-size:14px;">{esc(score.name)}</strong>
        <span style="color:{COLOURS["faint"]};font-size:12px;">
          {esc(score.team_short)}</span>{fixture_note}
        {_detail_line(detail_bits)}
      </td>
      <td style="{cell}{NUM}text-align:right;font-size:13px;">{score.price:.1f}</td>
      <td style="{cell}{NUM}text-align:right;font-size:15px;font-weight:700;
                 color:{COLOURS["text"]};">{score.mean:.2f}</td>
      <td style="{cell}{NUM}text-align:right;font-size:12px;color:{COLOURS["muted"]};
                 white-space:nowrap;">{score.floor:.1f} &ndash; {score.ceiling:.1f}</td>
      <td style="{cell}{NUM}text-align:right;font-size:13px;">{score.ownership:.1f}%</td>
      <td style="{cell}{NUM}text-align:right;font-size:13px;">
        {score.availability.risk:.0%}</td>
      <td style="{cell}text-align:right;">
        <span style="color:{confidence_colour};font-weight:700;font-size:11px;
                     letter-spacing:0.04em;text-transform:uppercase;">
          {esc(rec.confidence.value)}</span>
      </td>
    </tr>
    """


def _captain_picks(board: Board) -> str:
    """Section 3. The armband.

    Deliberately reports the *captained* numbers - doubled points, doubled floor,
    doubled ceiling - because that is what actually lands in your score. Showing
    the raw single-score figures here would make the reader do the multiplication
    themselves, and the whole point of the section is the doubling.
    """
    heading = _section_heading("Captain picks")
    intro = _note(
        "Ranked on a captaincy-specific objective, not the transfer one: the armband "
        "doubles the mean <em>and</em> the variance, so a weak floor is penalised hard "
        "(a captain blank is a double zero) and ownership is discounted far more gently "
        "than for a transfer. <strong>The bot does not know your squad, so these are the "
        "players worth having the armband on &ndash; you can only captain someone you "
        "already own.</strong>"
    )

    if not board.captains:
        return heading + intro + _empty("No captain candidates passed the availability filter.")

    rows = "\n".join(_captain_row(pick, index + 1) for index, pick in enumerate(board.captains))
    head = (
        _th("#", width="22px")
        + _th("Player")
        + _th("Captained xP", align="right")
        + _th("Floor / Ceiling", align="right", title="P10 - P90, doubled")
        + _th("Haul", align="right", title="P(20+ captained)")
        + _th("Owned", align="right")
        + _th("Conf.", align="right")
    )
    return heading + intro + _table(head, rows)


def _captain_row(pick, index: int) -> str:
    score = pick.score
    cell = (
        f"padding:11px 10px;border-bottom:1px solid {COLOURS['line_soft']};vertical-align:top;"
    )
    confidence_colour = CONFIDENCE_COLOUR[pick.confidence]

    badges = ""
    if pick.is_template:
        badges += " " + _pill("TEMPLATE", fg=COLOURS["muted"], bg=COLOURS["header_bg"])
    if pick.is_differential:
        badges += " " + _pill("DIFFERENTIAL", fg=COLOURS["accent"], bg=COLOURS["accent_soft"])
    if score.fixture_count >= 2:
        badges += " " + _pill(
            f"DGW &times;{score.fixture_count}",
            fg=COLOURS["confirmed"],
            bg=COLOURS["confirmed_soft"],
        )

    details = [f'<em style="font-style:normal;">{esc(pick.why)}</em>']
    for warning in pick.warnings:
        details.append(
            f'<span style="color:{COLOURS["warning"]};">&#9888;&#65039; {esc(warning)}</span>'
        )

    return f"""
    <tr>
      <td style="{cell}{NUM}color:{COLOURS["faint"]};font-size:12px;">{index}</td>
      <td style="{cell}font-size:13px;color:{COLOURS["body"]};">
        <strong style="color:{COLOURS["text"]};font-size:14px;">{esc(score.name)}</strong>
        <span style="color:{COLOURS["faint"]};font-size:12px;">
          {esc(score.team_short)}</span>{badges}
        {_detail_line(details)}
      </td>
      <td style="{cell}{NUM}text-align:right;font-size:15px;font-weight:700;
                 color:{COLOURS["text"]};">{pick.expected_points:.2f}</td>
      <td style="{cell}{NUM}text-align:right;font-size:12px;color:{COLOURS["muted"]};
                 white-space:nowrap;">
        {pick.captained_floor:.1f} &ndash; {pick.captained_ceiling:.1f}</td>
      <td style="{cell}{NUM}text-align:right;font-size:13px;">
        {pick.haul_probability:.0%}</td>
      <td style="{cell}{NUM}text-align:right;font-size:13px;">{score.ownership:.1f}%</td>
      <td style="{cell}text-align:right;">
        <span style="color:{confidence_colour};font-weight:700;font-size:11px;
                     letter-spacing:0.04em;text-transform:uppercase;">
          {esc(pick.confidence.value)}</span>
      </td>
    </tr>
    """


def _wildcard_squad(board: Board) -> str:
    """Section 7. The best GBP 100.0m squad for the rest of the season."""
    heading = _section_heading("Best wildcard squad")
    squad = board.wildcard

    if squad is None:
        return heading + _empty(
            "No wildcard squad could be built - projections are unavailable "
            "(expected before the season starts)."
        )

    # The three headline numbers get a strip of their own. Formation, spend and
    # projected points are what the reader compares against their own squad.
    summary = f"""
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
           style="background:{COLOURS["header_bg"]};border:1px solid {COLOURS["border"]};
                  border-radius:8px;margin-bottom:14px;">
      <tr>
        {_metric("Formation", esc(squad.formation))}
        {_metric("Spent", f"&pound;{squad.total_price:.1f}m")}
        {_metric("In the bank", f"&pound;{squad.money_left:.1f}m")}
        {_metric("Projected XI", f"{squad.starting_xp:.0f} pts", emphasis=True)}
      </tr>
    </table>
    {_note(esc(board.horizon_note) + " " + esc(squad.optimality_note))}
    """

    starters = "\n".join(
        _squad_row(
            player,
            is_captain=squad.captain is not None and player.element_id == squad.captain.element_id,
        )
        for player in squad.starters
    )
    bench = "\n".join(_squad_row(player, is_captain=False) for player in squad.bench)
    divider = (
        f'<tr><td colspan="5" style="padding:16px 10px 8px;font-size:10px;'
        f"font-weight:700;color:{COLOURS['faint']};text-transform:uppercase;"
        f'letter-spacing:0.09em;">Bench (in autosub order)</td></tr>'
    )
    head = (
        _th("Player")
        + _th("Price", align="right")
        + _th("This GW", align="right")
        + _th("Season xP", align="right")
        + _th("Owned", align="right")
    )

    return (
        heading
        + summary
        + _table(head, starters + divider + bench)
        + _note(
            "Squads are optimised on the best legal starting XI plus a light weight on the "
            "bench, because only eleven players score. That is why the bench is cheap &ndash; "
            "it is there to satisfy the squad rules, not to earn points."
        )
    )


def _metric(label: str, value: str, *, emphasis: bool = False) -> str:
    """One cell of the wildcard summary strip."""
    colour = COLOURS["accent"] if emphasis else COLOURS["text"]
    return f"""
    <td style="padding:12px 14px;vertical-align:top;">
      <div style="font-size:9px;font-weight:700;letter-spacing:0.09em;
                  text-transform:uppercase;color:{COLOURS["faint"]};">{label}</div>
      <div style="{NUM}margin-top:4px;font-size:16px;font-weight:700;color:{colour};">
        {value}</div>
    </td>
    """


def _squad_row(player, *, is_captain: bool) -> str:
    cell = f"padding:9px 10px;border-bottom:1px solid {COLOURS['line_soft']};"
    captain_badge = (
        " " + _pill("C", fg="#ffffff", bg=COLOURS["confirmed"]) if is_captain else ""
    )
    return f"""
    <tr>
      <td style="{cell}font-size:13px;">
        <span style="display:inline-block;min-width:30px;font-size:10px;font-weight:700;
                     color:{COLOURS["faint"]};letter-spacing:0.05em;">
          {esc(player.position)}</span>
        <strong style="color:{COLOURS["text"]};">{esc(player.name)}</strong>
        <span style="color:{COLOURS["faint"]};font-size:12px;">
          {esc(player.team_short)}</span>{captain_badge}
      </td>
      <td style="{cell}{NUM}text-align:right;font-size:13px;">{player.price:.1f}</td>
      <td style="{cell}{NUM}text-align:right;font-size:13px;color:{COLOURS["muted"]};">
        {player.gameweek_xp:.1f}</td>
      <td style="{cell}{NUM}text-align:right;font-size:13px;font-weight:700;
                 color:{COLOURS["text"]};">{player.season_xp:.0f}</td>
      <td style="{cell}{NUM}text-align:right;font-size:13px;color:{COLOURS["muted"]};">
        {player.ownership:.1f}%</td>
    </tr>
    """


def _sell_list(board: Board) -> str:
    """Section 3. Each entry carries the evidence that triggered it."""
    heading = _section_heading("Sell / avoid")
    if not board.sells:
        return heading + _empty("Nothing meets the sell threshold this gameweek.")

    cell = f"padding:11px 10px;border-bottom:1px solid {COLOURS['line_soft']};vertical-align:top;"
    rows = "\n".join(
        f"""
        <tr>
          <td style="{cell}font-size:13px;">
            <strong style="color:{COLOURS["text"]};font-size:14px;">
              {esc(rec.score.name)}</strong>
            <span style="color:{COLOURS["faint"]};font-size:12px;">
              {esc(rec.score.team_short)} &middot; {rec.score.price:.1f}m &middot;
              {rec.score.ownership:.1f}% owned</span>
            <div style="margin-top:5px;font-size:12px;line-height:1.6;
                        color:{COLOURS["danger"]};">{esc(rec.why)}</div>
          </td>
          <td style="{cell}{NUM}text-align:right;font-size:13px;white-space:nowrap;
                     color:{COLOURS["muted"]};">{rec.score.mean:.2f} xP</td>
        </tr>
        """
        for rec in board.sells
    )
    return heading + _table(_th("Player") + _th("xP", align="right"), rows)


def _watchlist(board: Board, context: RunContext) -> str:
    """Section 4. The leading-indicator section.

    Shows the z-score and the cross-validation state, because a number without
    its provenance is not evidence. Includes `scout_news_link` where FPL has one.
    """
    heading = _section_heading("Injury-signal watchlist")
    intro = _note(
        "Players whose transfer outflow has spiked without a price, fixture or chip "
        "explanation, and where FPL has not yet published news. This is a leading "
        "indicator, not a confirmation."
    )

    if not board.watchlist:
        return heading + intro + _empty("No unexplained transfer anomalies detected.")

    rows = "\n".join(_watchlist_row(score) for score in board.watchlist)
    return heading + intro + _table(_th("Player") + _th("Risk", align="right"), rows)


def _watchlist_row(score: PlayerScore) -> str:
    availability = score.availability
    zscore = availability.flow_zscore
    cell = f"padding:11px 10px;border-bottom:1px solid {COLOURS['line_soft']};vertical-align:top;"

    validation: list[str] = []
    if availability.corroborating_sources:
        validation.append("corroborated by " + ", ".join(availability.corroborating_sources))
    if availability.conflicting_sources:
        validation.append("CONFLICTS with " + ", ".join(availability.conflicting_sources))
    if not validation:
        validation.append("no corroborating source yet - single-signal only")

    link = ""
    if availability.scout_news_link:
        link = (
            f' &middot; <a href="{esc(availability.scout_news_link)}" '
            f'style="color:{COLOURS["accent"]};text-decoration:none;">club statement &rarr;</a>'
        )

    return f"""
    <tr>
      <td style="{cell}font-size:13px;">
        <strong style="color:{COLOURS["text"]};font-size:14px;">{esc(score.name)}</strong>
        <span style="color:{COLOURS["faint"]};font-size:12px;">
          {esc(score.team_short)} &middot; {score.ownership:.1f}% owned</span>
        <div style="margin-top:5px;font-size:12px;line-height:1.6;color:{COLOURS["muted"]};">
          z = {zscore:.2f} against own baseline &middot;
          cause: {esc(availability.flow_cause or "unclassified")} &middot;
          {esc("; ".join(validation))}{link}
        </div>
      </td>
      <td style="{cell}{NUM}text-align:right;font-size:13px;white-space:nowrap;
                 color:{COLOURS["warning"]};font-weight:700;">
        {availability.risk:.0%}</td>
    </tr>
    """


def _returning(board: Board) -> str:
    """Section 5. The buy-low window."""
    heading = _section_heading("Returning from injury")
    if not board.returning:
        return heading + _empty("No return signals detected.")

    intro = _note("Availability improving before the price does - the cheapest window to buy in.")
    cell = f"padding:11px 10px;border-bottom:1px solid {COLOURS['line_soft']};vertical-align:top;"
    rows = "\n".join(
        f"""
        <tr>
          <td style="{cell}font-size:13px;">
            <strong style="color:{COLOURS["text"]};font-size:14px;">{esc(score.name)}</strong>
            <span style="color:{COLOURS["faint"]};font-size:12px;">
              {esc(score.team_short)} &middot; {score.price:.1f}m &middot;
              {score.ownership:.1f}% owned</span>
            <div style="margin-top:5px;font-size:12px;line-height:1.6;
                        color:{COLOURS["muted"]};">{esc(reason)}</div>
          </td>
          <td style="{cell}{NUM}text-align:right;font-size:13px;white-space:nowrap;
                     color:{COLOURS["muted"]};">{score.mean:.2f} xP</td>
        </tr>
        """
        for score, reason in board.returning
    )
    return heading + intro + _table(_th("Player") + _th("xP", align="right"), rows)


def _caveats(quality: DataQuality, context: RunContext) -> str:
    """Section 6. Everything we are not sure about.

    This section is not an apology, it is part of the product. SPEC section 6.2
    sets the policy that we always produce output and label what was degraded;
    the caveats are what make that labelling honest.
    """
    items: list[str] = list(quality.caveats)

    if not context.is_confirmed_phase:
        items.append(
            "This is a provisional report. Managers' press conferences for a weekend "
            "fixture typically land Thursday and Friday afternoon, after this run. "
            "The T-3h report will resolve most of the outstanding fitness questions."
        )

    if not context.season_has_started:
        items.append(
            "The season has not started. FPL's season aggregates currently hold LAST "
            "season's values while form and transfer counters are reset to zero, so "
            "all attacking rates fall back to positional priors."
        )

    for failure in quality.invariant_failures:
        items.append(f"Data integrity: {failure}")

    if quality.unresolved_players:
        shown = ", ".join(quality.unresolved_players[:12])
        more = (
            f" and {len(quality.unresolved_players) - 12} more"
            if len(quality.unresolved_players) > 12
            else ""
        )
        items.append(
            f"Could not match {len(quality.unresolved_players)} third-party player "
            f"name(s) to FPL ids: {shown}{more}. Their data was excluded rather than guessed."
        )

    if not items:
        items.append("None. All sources responded and every invariant held.")

    bullets = "\n".join(
        f'<li style="margin-bottom:7px;padding-left:2px;">{esc(item)}</li>' for item in items
    )
    return (
        _section_heading("Caveats")
        + f'<ul style="margin:0;padding:0 0 0 18px;font-size:12px;line-height:1.65;'
        f'color:{COLOURS["muted"]};">{bullets}</ul>'
    )


def _footer(context: RunContext) -> str:
    generated = datetime.fromtimestamp(context.now_epoch, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"""
    <tr><td class="fb-pad" style="padding:22px 32px 26px;background:{COLOURS["header_bg"]};
               border-top:1px solid {COLOURS["border"]};">
      <p style="margin:0 0 8px;font-size:11px;font-weight:700;letter-spacing:0.09em;
                text-transform:uppercase;color:{COLOURS["faint"]};">
        fplBot &middot; GW{context.gameweek} &middot; tier {esc(context.tier)}
      </p>
      <p style="margin:0;font-size:11px;line-height:1.7;color:{COLOURS["muted"]};">
        Generated {esc(generated)} &middot; season {esc(context.season)}.<br>
        Recommendations are expected-rank-gain oriented, not expected-points oriented:
        ownership is penalised and ceiling is rewarded. The bot does not know your squad,
        so these are candidates, not swaps.<br>
        Data: Fantasy Premier League, ClubElo, Fantasy Football Scout, PremierInjuries,
        Understat. Personal, non-commercial use.
      </p>
    </td></tr>
    """


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------
def render_text(context: RunContext, board: Board) -> str:
    """Plain-text alternative part.

    Worth the effort: some clients prefer it, some people read mail in a terminal,
    and a multipart message with a real text part is markedly less likely to be
    treated as spam than an HTML-only one.
    """
    lines: list[str] = [
        f"GAMEWEEK {context.gameweek} TRANSFER BOARD",
        "=" * 60,
        f"Deadline: {format_deadline(context.deadline_epoch)}",
        f"Remaining: {context.hours_to_deadline:.1f} hours",
        f"Phase: {context.phase_label}"
        + ("" if context.is_confirmed_phase else " (team news still moving)"),
        f"Data quality: {context.data_quality.summary_line()}",
        "",
        "BUY BOARD",
        "-" * 60,
    ]

    for position, title in POSITION_NAMES.items():
        recommendations = board.buys_by_position.get(position, [])
        if not recommendations:
            continue
        lines.append(f"\n{title.upper()}")
        for index, rec in enumerate(recommendations, start=1):
            score = rec.score
            lines.append(
                f"  {index}. {score.name} ({score.team_short}) {score.price:.1f}m  "
                f"xP {score.mean:.2f}  [{score.floor:.1f}-{score.ceiling:.1f}]  "
                f"{score.ownership:.1f}% owned  risk {score.availability.risk:.0%}  "
                f"confidence {rec.confidence.value}"
            )
            lines.append(f"     Why: {rec.why}")
            if rec.runner_up:
                lines.append(f"     Runner-up: {rec.runner_up}")
            for warning in rec.warnings:
                lines.append(f"     ! {warning}")

    lines += ["", "CAPTAIN PICKS", "-" * 60]
    if board.captains:
        lines.append("  (you can only captain someone you already own)")
        for index, pick in enumerate(board.captains, start=1):
            score = pick.score
            flags = []
            if pick.is_template:
                flags.append("TEMPLATE")
            if pick.is_differential:
                flags.append("DIFFERENTIAL")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(
                f"  {index}. {score.name} ({score.team_short})  "
                f"{pick.expected_points:.2f} captained  "
                f"[{pick.captained_floor:.1f}-{pick.captained_ceiling:.1f}]  "
                f"haul {pick.haul_probability:.0%}  "
                f"{score.ownership:.1f}% owned  "
                f"confidence {pick.confidence.value}{suffix}"
            )
            lines.append(f"     Why: {pick.why}")
            for warning in pick.warnings:
                lines.append(f"     ! {warning}")
    else:
        lines.append("  No captain candidates passed the availability filter.")

    lines += ["", "SELL / AVOID", "-" * 60]
    if board.sells:
        for rec in board.sells:
            lines.append(
                f"  {rec.score.name} ({rec.score.team_short}, {rec.score.ownership:.1f}% owned) "
                f"- {rec.why}"
            )
    else:
        lines.append("  Nothing meets the sell threshold.")

    lines += ["", "INJURY-SIGNAL WATCHLIST", "-" * 60]
    if board.watchlist:
        for score in board.watchlist:
            lines.append(
                f"  {score.name} ({score.team_short}) z={score.availability.flow_zscore:.2f} "
                f"cause={score.availability.flow_cause}"
            )
    else:
        lines.append("  No unexplained transfer anomalies.")

    lines += ["", "RETURNING FROM INJURY", "-" * 60]
    if board.returning:
        for score, reason in board.returning:
            lines.append(f"  {score.name} ({score.team_short}) - {reason}")
    else:
        lines.append("  None detected.")

    lines += ["", "BEST WILDCARD SQUAD", "-" * 60]
    squad = board.wildcard
    if squad is None:
        lines.append("  Unavailable - no season projections (expected before the season starts).")
    else:
        lines.append(
            f"  {squad.formation}  "
            f"GBP {squad.total_price:.1f}m spent, GBP {squad.money_left:.1f}m left  "
            f"{squad.starting_xp:.0f} projected pts from the XI"
        )
        lines.append(f"  {board.horizon_note}")
        lines.append(f"  {squad.optimality_note}")
        lines.append("")
        for player in squad.starters:
            marker = (
                " (C)"
                if squad.captain is not None and player.element_id == squad.captain.element_id
                else ""
            )
            lines.append(
                f"    {player.position:<4}{player.name} ({player.team_short}){marker}"
                f"  {player.price:.1f}m  GW {player.gameweek_xp:.1f}  "
                f"season {player.season_xp:.0f}"
            )
        lines.append("    -- bench (autosub order) --")
        for player in squad.bench:
            lines.append(
                f"    {player.position:<4}{player.name} ({player.team_short})"
                f"  {player.price:.1f}m  season {player.season_xp:.0f}"
            )

    lines += ["", "CAVEATS", "-" * 60]
    caveats = context.data_quality.caveats or ["None."]
    lines.extend(f"  - {caveat}" for caveat in caveats)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Failure email
# ---------------------------------------------------------------------------
def render_failure_html(context: RunContext, reason: str) -> str:
    """What we send instead of advice when the data is too stale to trust.

    SPEC section 6.2: we refuse only above the hard staleness ceiling, and then we
    email about the *failure*, not about transfers. Silence would be worse - the
    user would assume the bot ran and found nothing worth saying.
    """
    quality_lines = "\n".join(
        f'<li style="margin-bottom:6px;">{esc(name)}: '
        f'<strong style="color:{COLOURS["text"]};">{esc(status.state.value)}</strong>'
        + (f" &ndash; {esc(status.detail)}" if status.detail else "")
        + "</li>"
        for name, status in sorted(context.data_quality.sources.items())
    )
    hours = int(context.data_quality.worst_age_seconds / 3600)

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light only">
<title>fplBot GW{context.gameweek} - no reliable board</title>
</head>
<body style="margin:0;padding:0;background:{COLOURS["canvas"]};font-family:{FONT};
             color:{COLOURS["body"]};">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="background:{COLOURS["canvas"]};">
  <tr><td align="center" style="padding:24px 12px;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="{CARD_WIDTH}"
           style="width:100%;max-width:{CARD_WIDTH}px;background:{COLOURS["surface"]};
                  border:1px solid {COLOURS["border"]};border-radius:14px;overflow:hidden;">
      <tr><td style="background:{COLOURS["danger"]};padding:22px 30px;">
        <div style="font-size:10px;font-weight:700;letter-spacing:0.16em;
                    text-transform:uppercase;color:#fecaca;">fplBot &middot; run aborted</div>
        <h1 style="margin:9px 0 0;font-size:21px;line-height:1.3;color:#ffffff;font-weight:700;">
          fplBot could not produce a reliable board for GW{context.gameweek}
        </h1>
      </td></tr>
      <tr><td style="padding:24px 30px 28px;">
        <p style="margin:0 0 14px;font-size:14px;line-height:1.6;">
          Deadline {esc(format_deadline(context.deadline_epoch))}
          ({context.hours_to_deadline:.1f} hours away).
        </p>
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
               style="background:{COLOURS["danger_soft"]};border-left:3px solid
                      {COLOURS["danger"]};border-radius:6px;margin-bottom:16px;">
          <tr><td style="padding:12px 14px;font-size:13px;line-height:1.6;
                     color:{COLOURS["text"]};">
            <strong>Reason:</strong> {esc(reason)}
          </td></tr>
        </table>
        <p style="margin:0 0 18px;font-size:12px;line-height:1.65;color:{COLOURS["muted"]};">
          Recommendations were withheld rather than sent from data past the
          {esc(hours)}-hour staleness ceiling. This message exists so the silence is not
          mistaken for &ldquo;nothing to report&rdquo;.
        </p>
        <h2 style="font-size:13px;font-weight:700;margin:0 0 4px;color:{COLOURS["text"]};">
          Source status</h2>
        <div style="width:34px;height:3px;background:{COLOURS["danger"]};border-radius:2px;
                    margin-bottom:12px;"></div>
        <ul style="margin:0;padding:0 0 0 18px;font-size:12px;line-height:1.6;
                   color:{COLOURS["muted"]};">{quality_lines}</ul>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""
