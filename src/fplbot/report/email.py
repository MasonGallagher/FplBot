"""Sending the report via Amazon SES.

Why SES rather than SNS: SNS email is plain text only and prefixes every message
with subscription boilerplate. SES sends real multipart HTML mail from a verified
address, which for a report full of tables is not a cosmetic difference.

Two operational notes that will bite on first deploy:

* **A new SES account is in the sandbox.** In sandbox mode you may only send *to*
  verified addresses, and there is a low daily cap. For a personal bot emailing
  its own owner that is genuinely fine - verify the one recipient and never leave
  the sandbox. `deploy.sh` verifies the addresses for you and explains this.

* **The From address must be verified** (or its domain must be). The identity is
  created by the SAM template and confirmed by clicking the link AWS emails you.

We send a multipart/alternative message with both text and HTML parts. Beyond
client compatibility, a message with a real text alternative scores better with
spam filters than an HTML-only one - which matters for something that arrives on
a schedule and must not end up in Junk three hours before a deadline.

---------------------------------------------------------------------------
ON DELIVERABILITY, AND THE LIMIT OF WHAT THIS FILE CAN FIX
---------------------------------------------------------------------------
`build_message` sets the headers that legitimate automated mail is expected to
carry: a display name, `Reply-To`, `Date`, `Message-ID`, `List-Id`,
`Auto-Submitted`, and one-click `List-Unsubscribe`. Gmail's bulk-sender guidance
names the unsubscribe pair explicitly, and their absence is a mild negative
signal on its own.

None of that addresses the actual problem, so it is written down here rather
than discovered later:

**If EMAIL_FROM is an @gmail.com (or @outlook.com, or any other provider's)
address, this mail cannot authenticate.** SPF checks the envelope sender, which
belongs to `amazonses.com`, and gmail.com's SPF record does not authorise SES.
DKIM would have to be signed by a key published in gmail.com's DNS, which is
Google's zone and not ours. Both alignment checks therefore fail, and what
arrives at Gmail is a message claiming to be from one of its own users, sent
from infrastructure Google has never authorised - which is indistinguishable
from spoofing, because structurally it *is* spoofing. Junk is the correct
verdict, and no header or stylesheet changes it.

The fix is a domain you control: verify it as an SES domain identity, enable
Easy DKIM (three CNAMEs), set a custom MAIL FROM subdomain so SPF aligns too,
and send as `fplbot@yourdomain`. Until then the practical workaround is a Gmail
filter on this From address with "Never send it to Spam".
"""

from __future__ import annotations

from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from typing import Any

import boto3
from botocore.exceptions import ClientError

from fplbot.config import get_settings
from fplbot.observability import Metric, count, logger

# The display name shown in the inbox list instead of a bare address.
SENDER_NAME = "fplBot"


@dataclass
class SendResult:
    sent: bool
    message_id: str | None = None
    error: str | None = None


def build_message(
    subject: str, html_body: str, text_body: str, sender: str, recipients: list[str]
) -> MIMEMultipart:
    """Assemble a multipart/alternative message.

    Part order matters and is not arbitrary: in multipart/alternative the *last*
    part is the one a capable client prefers, so text must come first and HTML
    second. Getting this backwards makes every client show the plain-text version.

    The headers beyond From/To/Subject are deliverability hygiene - see the module
    docstring for what they do and, more importantly, what they cannot do.
    """
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    # A display name is what turns a bare "bot@example.com" in the inbox list
    # into "fplBot". formataddr quotes and encodes it correctly.
    message["From"] = formataddr((SENDER_NAME, sender))
    message["To"] = ", ".join(recipients)
    message["Reply-To"] = sender

    # RFC 5322 requires Date, and Message-ID is what clients thread on. SES will
    # supply both if absent, but a message that arrives already well-formed is
    # not relying on that.
    message["Date"] = formatdate(localtime=False)
    domain = sender.rpartition("@")[2] or None
    message["Message-ID"] = make_msgid(domain=domain)

    # Marks this as machine-generated so well-behaved auto-responders stay quiet
    # rather than bouncing an out-of-office back at a Lambda.
    message["Auto-Submitted"] = "auto-generated"
    message["List-Id"] = f"fplBot transfer board <fplbot.{domain}>"

    # Gmail's bulk-sender rules call for one-click unsubscribe. A mailto: target
    # is a valid List-Unsubscribe and needs no endpoint to host - the recipient
    # here is the operator, who can simply stop the schedule.
    message["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
    message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))
    return message


def send_report(
    subject: str,
    html_body: str,
    text_body: str,
    *,
    client: Any = None,
    dry_run: bool | None = None,
) -> SendResult:
    """Send the report.

    `dry_run` renders and logs everything without calling SES. It is the default
    for local invocation and it is how you check the output of a new season's
    first run without emailing yourself a broken board.
    """
    settings = get_settings()
    is_dry_run = settings.dry_run if dry_run is None else dry_run

    if not settings.email_to:
        logger.error("No EMAIL_TO recipients configured - cannot send")
        return SendResult(sent=False, error="no recipients configured")

    if is_dry_run:
        logger.info(
            "DRY RUN - report rendered but not sent",
            extra={
                "subject": subject,
                "recipients": list(settings.email_to),
                "html_bytes": len(html_body),
                "text_preview": text_body[:600],
            },
        )
        return SendResult(sent=False, message_id="dry-run")

    ses = client or boto3.client("sesv2", region_name=settings.aws_region)
    message = build_message(
        subject, html_body, text_body, settings.email_from, list(settings.email_to)
    )

    try:
        # SendEmail with a raw MIME blob rather than the structured Simple format:
        # it keeps the message construction in standard library code we can unit
        # test, and it is the same call shape we would need for attachments later.
        response = ses.send_email(
            FromEmailAddress=settings.email_from,
            Destination={"ToAddresses": list(settings.email_to)},
            Content={"Raw": {"Data": message.as_bytes()}},
        )
        message_id = response.get("MessageId")
        logger.info(
            "Report sent",
            extra={"message_id": message_id, "recipients": len(settings.email_to)},
        )
        count(Metric.NOTIFICATION_SENT)
        return SendResult(sent=True, message_id=message_id)

    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        hint = _ses_error_hint(code)
        logger.error(
            "SES send failed",
            extra={"code": code, "ses_message": exc.response["Error"]["Message"], "hint": hint},
        )
        return SendResult(sent=False, error=f"{code}: {hint}")


def _ses_error_hint(code: str) -> str:
    """Turn an SES error code into the thing you actually need to do."""
    return {
        "MessageRejected": (
            "The From or To address is not verified. A new SES account is in the sandbox, "
            "where every recipient must be verified individually. Run "
            "`aws sesv2 create-email-identity --email-identity <address>` and click the link."
        ),
        "AccountSuspendedException": "The SES account is suspended - check the SES console.",
        "SendingPausedException": (
            "Sending is paused for this account, usually after a bounce or complaint spike."
        ),
        "LimitExceededException": "Daily sending quota exceeded (sandbox is 200/day).",
        "NotFoundException": "The sending identity does not exist in this region.",
    }.get(code, "See the SES console for details.")


def build_subject(gameweek: int, tier: str, is_confirmed: bool, top_pick: str | None) -> str:
    """Compose the subject line.

    The phase marker leads, because it is the thing that determines whether the
    user should act now or wait. A subject line is often all that gets read on a
    phone, and "FINAL" versus "provisional" is the single most decision-relevant
    bit of information we have.
    """
    marker = "FINAL" if is_confirmed else "provisional"
    head = f"fplBot GW{gameweek} ({marker}, T-{tier})"
    return f"{head}: {top_pick}" if top_pick else head
