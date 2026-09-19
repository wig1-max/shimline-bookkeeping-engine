"""One SMTP boundary shared by synchronous fallback and background jobs."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from .settings import settings
from .telemetry import get_logger

_LOG = get_logger("email")


def _deliver(message: EmailMessage) -> bool:
    password = settings.smtp_pass.get_secret_value()
    if not (settings.smtp_host and settings.smtp_user and password):
        # This exact silence has already cost this project once: SMTP_PASS was
        # empty in production, every notification was dropped, and nothing said
        # so because the guard returned early without logging. A misconfigured
        # mailer and a working one looked identical from the outside.
        #
        # Distinct outcome from "retryable" on purpose: no number of retries
        # fixes an unset password, and an operator reading these events needs to
        # tell "the server refused us" from "we never had credentials".
        _LOG.error(
            "provider.write_failed",
            provider="smtp",
            operation="send",
            outcome="unconfigured",
        )
        return False
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
            server.starttls()
            server.login(settings.smtp_user, password)
            server.send_message(message)
    except (OSError, smtplib.SMTPException):
        _LOG.warning(
            "provider.write_failed",
            provider="smtp",
            operation="send",
            outcome="retryable",
        )
        return False
    _LOG.info("provider.write_succeeded", provider="smtp", operation="send", outcome="sent")
    return True


def send_submission_notification(
    company: str, contact_name: str, email: str, submission_id: str
) -> bool:
    if not settings.notify_to:
        # Same silence, one layer up: with no NOTIFY_TO there is nobody to tell
        # that a customer just paid and submitted, and without this line nothing
        # would record that the alert had nowhere to go.
        _LOG.error(
            "provider.write_failed",
            provider="smtp",
            operation="notify",
            outcome="unconfigured",
        )
        return False
    message = EmailMessage()
    message["Subject"] = f"New Cash-Leak Review submission — {company or 'unknown company'}"
    message["From"] = settings.effective_notify_from
    message["To"] = settings.notify_to
    message.set_content(
        f"Company: {company}\nContact: {contact_name} <{email}>\n\n"
        "Open it in the workspace: https://api.shimline.ca/admin — "
        f"submission id {submission_id}."
    )
    return _deliver(message)


def send_portal_link(email: str, token: str, portal_base_url: str | None = None) -> bool:
    link = f"{portal_base_url or settings.normalized_portal_base_url}/portal/enter?t={token}"
    message = EmailMessage()
    message["Subject"] = "Your Shimline Cash-Leak Review — next step"
    message["From"] = settings.effective_notify_from
    message["To"] = email
    message.set_content(
        "Thanks — your Cash-Leak Review is paid for.\n\n"
        "Open this link to send us what we need. You can either upload your "
        "QuickBooks exports, or connect QuickBooks directly and let us pull "
        "the reports ourselves:\n\n"
        f"{link}\n\n"
        "The link is personal to you and expires in 14 days. If it expires, "
        "reply to this email and we will send a new one.\n\n"
        "Shimline — Tejova Financial Services\n"
        "https://shimline.ca\n"
    )
    return _deliver(message)
