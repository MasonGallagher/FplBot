"""Rendering the board into an email, and sending it."""

from fplbot.report.email import send_report
from fplbot.report.render import render_failure_html, render_html, render_text

__all__ = ["render_failure_html", "render_html", "render_text", "send_report"]
