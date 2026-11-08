"""Safe triage of customer Gmail traffic for manager-facing Telegram.

This feature never deletes an email, finalizes an order or changes order state.
Unknown and possible orders remain prominent. Only unmistakable automated
delivery reports and inquiries are routed away from the main order feed.
"""
import os
import re


_AUTOMATIC_SENDER = re.compile(r"^(?:mailer-daemon|postmaster)(?:[.+_-][^@]*)?@", re.I)
_AUTOMATIC_SUBJECT = re.compile(
    r"^(?:delivery status notification(?:\s*\([^)]*\))?|"
    r"undelivered mail returned to sender|"
    r"mail delivery failed(?:\s*[:–-].*)?|"
    r"delivery failure(?:\s*[:–-].*)?|"
    r"delivery incomplete(?:\s*[:–-].*)?|"
    r"returned mail(?:\s*[:–-].*)?|"
    r"failure notice)$",
    re.I,
)


def enabled() -> bool:
    return os.getenv("OMS_EMAIL_TRIAGE_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def is_technical_notice(sender_email: str | None, subject: str | None) -> bool:
    return bool(
        _AUTOMATIC_SENDER.match((sender_email or "").strip())
        and _AUTOMATIC_SUBJECT.match((subject or "").strip())
    )


def kind_for_draft(draft) -> str:
    """Returns 'order', 'question', or 'technical'; default is always order."""
    if getattr(draft, "source", None) != "email":
        return "order"
    details = getattr(draft, "contact_details", None) or {}
    if details.get("mail_intent") == "system" or is_technical_notice(
        getattr(draft, "sender_email", None), getattr(draft, "subject", None)
    ):
        return "technical"
    if details.get("mail_intent") == "inquiry":
        return "question"
    return "order"


def should_send_order_notification(draft) -> bool:
    return not enabled() or kind_for_draft(draft) == "order"
