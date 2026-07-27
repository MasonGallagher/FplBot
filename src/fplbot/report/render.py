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

Styling is inline. Email clients strip `<style>` blocks with enthusiasm and
almost no support for anything modern, so inline attributes on tables is the only
approach that survives Gmail, Outlook and Apple Mail alike.
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
# colour should mark the exceptions rather than the norm.
COLOURS = {
    "text": "#1a1a1a",
    "muted": "#666666",
    "border": "#dddddd",
    "header_bg": "#f5f5f5",
    "provisional": "#b45309",  # amber - phase 1
    "confirmed": "#15803d",  # green - phase 2, act on this
    "danger": "#b91c1c",
    "warning": "#a16207",
    "accent": "#1d4ed8",
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


def esc(value: object) -> str:
    """HTML-escape anything on its way into the document."""
    return html.escape(str(value), quote=True)


def format_deadline(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%a %d %b %Y, %H:%M UTC")


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def render_html(context: RunContext, board: Board) -> str:
    """Render the full report."""
    sections = [
        _header(context),
        _buy_board(board),
        _sell_list(board),
        _watchlist(board, context),
        _returning(board),
        _caveats(context.data_quality, context),
        _footer(context),
    ]
    body = "\n".join(sections)
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>fplBot GW{context.gameweek} - {context.phase_label}</title></head>"
        f'<body style="margin:0;padding:16px;background:#ffffff;'
        f"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
        f'color:{COLOURS["text"]};line-height:1.5;">'
        f'<div style="max-width:860px;margin:0 auto;">{body}</div>'
        "</body></html>"
    )


def _header(context: RunContext) -> str:
    """Section 1. The phase label is the most important thing on the page.

    Phase 1 output is provisional and must be labelled as such: at T-48h most
    managers' press conferences have not happened, and over half the injury table
    is still "Currently Being Assessed". Phase 2 at T-3h is the one to act on.
    """
    is_confirmed = context.is_confirmed_phase
    colour = COLOURS["confirmed"] if is_confirmed else COLOURS["provisional"]
    banner = (
        "CONFIRMED - this is the report to act on"
        if is_confirmed
        else "PROVISIONAL - team news is still moving; a confirmed report follows at T-3h"
    )

    quality = context.data_quality
    quality_colour = COLOURS["muted"] if not quality.degraded_sources else COLOURS["warning"]

    return f"""
    <h1 style="margin:0 0 4px;font-size:22px;">Gameweek {context.gameweek} transfer board</h1>
    <p style="margin:0 0 12px;color:{COLOURS["muted"]};font-size:14px;">
      Deadline {esc(format_deadline(context.deadline_epoch))} &middot;
      <strong>{context.hours_to_deadline:.1f} hours remaining</strong>
    </p>
    <div style="background:{colour};color:#ffffff;padding:10px 14px;border-radius:4px;
                font-weight:600;font-size:14px;margin-bottom:12px;">
      {esc(banner)}
    </div>
    <p style="margin:0 0 20px;font-size:13px;color:{quality_colour};">
      <strong>Data quality:</strong> {esc(quality.summary_line())}
    </p>
    """


def _buy_board(board: Board) -> str:
    """Section 2. Ranked by position."""
    blocks = [_section_heading("Buy board")]

    for position, title in POSITION_NAMES.items():
        recommendations = board.buys_by_position.get(position, [])
        if not recommendations:
            continue
        rows = "\n".join(_buy_row(rec, index + 1) for index, rec in enumerate(recommendations))
        blocks.append(
            f"""
            <h3 style="margin:18px 0 6px;font-size:15px;">{esc(title)}</h3>
            <table role="presentation" cellpadding="6" cellspacing="0" width="100%"
                   style="border-collapse:collapse;font-size:13px;">
              <thead>
                <tr style="background:{COLOURS["header_bg"]};text-align:left;">
                  <th style="width:24px;">#</th>
                  <th>Player</th>
                  <th style="text-align:right;">Price</th>
                  <th style="text-align:right;">xP</th>
                  <th style="text-align:right;" title="P10 - P90">Floor / Ceiling</th>
                  <th style="text-align:right;">Owned</th>
                  <th style="text-align:right;">Risk</th>
                  <th>Confidence</th>
                </tr>
              </thead>
              <tbody>{rows}</tbody>
            </table>
            """
        )

    if len(blocks) == 1:
        blocks.append(_empty("No buy candidates passed the filters this gameweek."))
    return "\n".join(blocks)


def _buy_row(rec: Recommendation, index: int) -> str:
    score = rec.score
    border = f"border-top:1px solid {COLOURS['border']};"
    confidence_colour = CONFIDENCE_COLOUR[rec.confidence]

    detail_bits = [f'<em style="color:{COLOURS["muted"]};">{esc(rec.why)}</em>']
    if rec.runner_up:
        detail_bits.append(
            f'<span style="color:{COLOURS["muted"]};">Runner-up: {esc(rec.runner_up)}</span>'
        )
    for warning in rec.warnings:
        detail_bits.append(
            f'<span style="color:{COLOURS["warning"]};">&#9888; {esc(warning)}</span>'
        )
    if score.availability.scout_news_link:
        # FPL hands us the actual club statement behind each injury, keyed to the
        # element id. Passing it straight through means the user can read the
        # primary source rather than taking our word for it.
        detail_bits.append(
            f'<a href="{esc(score.availability.scout_news_link)}" '
            f'style="color:{COLOURS["accent"]};">Club statement</a>'
        )

    fixture_note = ""
    if score.fixture_count >= 2:
        fixture_note = (
            f' <span style="color:{COLOURS["confirmed"]};font-weight:600;">'
            f"DGW x{score.fixture_count}</span>"
        )

    return f"""
    <tr style="{border}">
      <td style="vertical-align:top;color:{COLOURS["muted"]};">{index}</td>
      <td style="vertical-align:top;">
        <strong>{esc(score.name)}</strong>
        <span style="color:{COLOURS["muted"]};">({esc(score.team_short)})</span>{fixture_note}
        <div style="margin-top:3px;font-size:12px;">{"<br>".join(detail_bits)}</div>
      </td>
      <td style="vertical-align:top;text-align:right;">{score.price:.1f}</td>
      <td style="vertical-align:top;text-align:right;"><strong>{score.mean:.2f}</strong></td>
      <td style="vertical-align:top;text-align:right;color:{COLOURS["muted"]};">
        {score.floor:.1f} / {score.ceiling:.1f}
      </td>
      <td style="vertical-align:top;text-align:right;">{score.ownership:.1f}%</td>
      <td style="vertical-align:top;text-align:right;">{score.availability.risk:.0%}</td>
      <td style="vertical-align:top;color:{confidence_colour};font-weight:600;">
        {esc(rec.confidence.value)}
      </td>
    </tr>
    """


def _sell_list(board: Board) -> str:
    """Section 3. Each entry carries the evidence that triggered it."""
    if not board.sells:
        return _section_heading("Sell / avoid") + _empty(
            "Nothing meets the sell threshold this gameweek."
        )

    rows = "\n".join(
        f"""
        <tr style="border-top:1px solid {COLOURS["border"]};">
          <td style="vertical-align:top;">
            <strong>{esc(rec.score.name)}</strong>
            <span style="color:{COLOURS["muted"]};">({esc(rec.score.team_short)},
            {rec.score.price:.1f}m, {rec.score.ownership:.1f}% owned)</span>
            <div style="margin-top:3px;font-size:12px;color:{COLOURS["danger"]};">
              {esc(rec.why)}
            </div>
          </td>
          <td style="vertical-align:top;text-align:right;">{rec.score.mean:.2f} xP</td>
        </tr>
        """
        for rec in board.sells
    )

    return (
        _section_heading("Sell / avoid")
        + f"""<table role="presentation" cellpadding="6" cellspacing="0" width="100%"
                     style="border-collapse:collapse;font-size:13px;"><tbody>{rows}</tbody></table>"""
    )


def _watchlist(board: Board, context: RunContext) -> str:
    """Section 4. The leading-indicator section.

    Shows the z-score and the cross-validation state, because a number without
    its provenance is not evidence. Includes `scout_news_link` where FPL has one.
    """
    heading = _section_heading("Injury-signal watchlist")
    intro = (
        f'<p style="margin:0 0 8px;font-size:13px;color:{COLOURS["muted"]};">'
        "Players whose transfer outflow has spiked without a price, fixture or chip "
        "explanation, and where FPL has not yet published news. This is a leading "
        "indicator, not a confirmation.</p>"
    )

    if not board.watchlist:
        return heading + intro + _empty("No unexplained transfer anomalies detected.")

    rows = "\n".join(_watchlist_row(score) for score in board.watchlist)
    return (
        heading
        + intro
        + f"""<table role="presentation" cellpadding="6" cellspacing="0" width="100%"
                     style="border-collapse:collapse;font-size:13px;"><tbody>{rows}</tbody></table>"""
    )


def _watchlist_row(score: PlayerScore) -> str:
    availability = score.availability
    zscore = availability.flow_zscore

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
            f'style="color:{COLOURS["accent"]};">club statement</a>'
        )

    return f"""
    <tr style="border-top:1px solid {COLOURS["border"]};">
      <td style="vertical-align:top;">
        <strong>{esc(score.name)}</strong>
        <span style="color:{COLOURS["muted"]};">({esc(score.team_short)},
        {score.ownership:.1f}% owned)</span>
        <div style="margin-top:3px;font-size:12px;color:{COLOURS["muted"]};">
          z = {zscore:.2f} against own baseline &middot;
          cause: {esc(availability.flow_cause or "unclassified")} &middot;
          {esc("; ".join(validation))}{link}
        </div>
      </td>
      <td style="vertical-align:top;text-align:right;">{availability.risk:.0%} risk</td>
    </tr>
    """


def _returning(board: Board) -> str:
    """Section 5. The buy-low window."""
    heading = _section_heading("Returning from injury")
    if not board.returning:
        return heading + _empty("No return signals detected.")

    rows = "\n".join(
        f"""
        <tr style="border-top:1px solid {COLOURS["border"]};">
          <td style="vertical-align:top;">
            <strong>{esc(score.name)}</strong>
            <span style="color:{COLOURS["muted"]};">({esc(score.team_short)},
            {score.price:.1f}m, {score.ownership:.1f}% owned)</span>
            <div style="margin-top:3px;font-size:12px;color:{COLOURS["muted"]};">{esc(reason)}</div>
          </td>
          <td style="vertical-align:top;text-align:right;">{score.mean:.2f} xP</td>
        </tr>
        """
        for score, reason in board.returning
    )
    return (
        heading + f'<p style="margin:0 0 8px;font-size:13px;color:{COLOURS["muted"]};">'
        "Availability improving before the price does - the cheapest window to buy in.</p>"
        + f"""<table role="presentation" cellpadding="6" cellspacing="0" width="100%"
                     style="border-collapse:collapse;font-size:13px;"><tbody>{rows}</tbody></table>"""
    )


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

    bullets = "\n".join(f"<li style='margin-bottom:5px;'>{esc(item)}</li>" for item in items)
    return (
        _section_heading("Caveats")
        + f'<ul style="margin:0;padding-left:20px;font-size:13px;color:{COLOURS["muted"]};">'
        f"{bullets}</ul>"
    )


def _footer(context: RunContext) -> str:
    generated = datetime.fromtimestamp(context.now_epoch, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"""
    <hr style="margin:24px 0 12px;border:0;border-top:1px solid {COLOURS["border"]};">
    <p style="font-size:11px;color:{COLOURS["muted"]};margin:0;">
      fplBot &middot; generated {esc(generated)} &middot; season {esc(context.season)} &middot;
      tier {esc(context.tier)}<br>
      Recommendations are expected-rank-gain oriented, not expected-points oriented:
      ownership is penalised and ceiling is rewarded. The bot does not know your squad,
      so these are candidates, not swaps.<br>
      Data: Fantasy Premier League, ClubElo, Fantasy Football Scout, PremierInjuries,
      Understat. Personal, non-commercial use.
    </p>
    """


def _section_heading(title: str) -> str:
    return (
        f'<h2 style="margin:26px 0 8px;font-size:17px;padding-bottom:4px;'
        f'border-bottom:2px solid {COLOURS["border"]};">{esc(title)}</h2>'
    )


def _empty(message: str) -> str:
    return f'<p style="font-size:13px;color:{COLOURS["muted"]};margin:0;">{esc(message)}</p>'


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
        f"<li>{esc(name)}: {esc(status.state.value)}"
        + (f" - {esc(status.detail)}" if status.detail else "")
        + "</li>"
        for name, status in sorted(context.data_quality.sources.items())
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"></head>
    <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
                 padding:16px;color:{COLOURS["text"]};">
      <h1 style="font-size:20px;color:{COLOURS["danger"]};margin:0 0 8px;">
        fplBot could not produce a reliable board for GW{context.gameweek}
      </h1>
      <p style="font-size:14px;">
        Deadline {esc(format_deadline(context.deadline_epoch))}
        ({context.hours_to_deadline:.1f} hours away).
      </p>
      <p style="font-size:14px;"><strong>Reason:</strong> {esc(reason)}</p>
      <p style="font-size:13px;color:{COLOURS["muted"]};">
        Recommendations were withheld rather than sent from data past the
        {esc(int(context.data_quality.worst_age_seconds / 3600))}-hour staleness ceiling.
        This message exists so the silence is not mistaken for "nothing to report".
      </p>
      <h2 style="font-size:15px;margin:18px 0 6px;">Source status</h2>
      <ul style="font-size:13px;">{quality_lines}</ul>
    </body></html>
    """
