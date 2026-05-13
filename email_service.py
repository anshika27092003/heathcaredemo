"""
SMTP helper for sending templated credentialing emails.
Credentials are read from environment variables (never hard-coded).
"""

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

from dotenv import load_dotenv

# Load .env once when this module is imported (Streamlit reloads modules often).
load_dotenv()

# Default template for the credentialing request (can be overridden via env if needed).
DEFAULT_SUBJECT = "Document Submission Request"
DEFAULT_BODY = """Dear Provider,

Please submit your updated credentialing documents for verification.

Regards,
Admin Team"""


def _strip_env(value: Optional[str]) -> Optional[str]:
    """Trim accidental spaces/newlines often pasted into .env values."""
    if value is None:
        return None
    s = value.strip()
    return s if s else None


def _normalize_app_password(password: Optional[str]) -> Optional[str]:
    """
    Gmail / Google Workspace App Passwords are often pasted as four groups of four letters.
    SMTP expects one continuous 16-character password with no spaces.
    """
    if not password:
        return password
    collapsed = "".join(password.split())
    if len(collapsed) == 16 and collapsed.isalnum():
        return collapsed
    return password


def _get_smtp_settings() -> dict[str, Optional[str]]:
    """Collect SMTP configuration from the environment."""
    user = _strip_env(os.getenv("SMTP_USER"))
    password = _normalize_app_password(_strip_env(os.getenv("SMTP_PASSWORD")))
    from_addr = _strip_env(os.getenv("SMTP_FROM")) or user
    return {
        "host": _strip_env(os.getenv("SMTP_HOST")) or "smtp.gmail.com",
        "port": int(os.getenv("SMTP_PORT", "587")),
        "user": user,
        "password": password,
        "from_addr": from_addr,
        "subject": os.getenv("EMAIL_SUBJECT", DEFAULT_SUBJECT),
        "body": os.getenv("EMAIL_BODY", DEFAULT_BODY),
    }


def send_credentialing_email(to_address: str) -> tuple[bool, str]:
    """
    Send the credentialing document request email.

    Returns (success, message) for UI feedback.
    """
    to_address = to_address.strip()
    if not to_address:
        return False, "Recipient address is missing."

    settings = _get_smtp_settings()
    missing = [
        name
        for name, key in [
            ("SMTP_USER", settings["user"]),
            ("SMTP_PASSWORD", settings["password"]),
            ("SMTP_FROM or SMTP_USER", settings["from_addr"]),
        ]
        if not key
    ]
    if missing:
        return False, (
            "Email is not configured. Set the following in your .env file: "
            + ", ".join(missing)
        )

    message = EmailMessage()
    message["Subject"] = settings["subject"] or DEFAULT_SUBJECT
    message["From"] = settings["from_addr"]
    message["To"] = to_address
    message.set_content(settings["body"] or DEFAULT_BODY)

    context = ssl.create_default_context()

    try:
        with smtplib.SMTP(settings["host"], settings["port"], timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(settings["user"], settings["password"])
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        # Surface the provider's reply (e.g. invalid credentials vs wrong host).
        raw = getattr(exc, "smtp_error", b"") or b""
        if isinstance(raw, bytes):
            detail = raw.decode(errors="replace").strip()
        else:
            detail = str(raw).strip()
        hint = (
            "If this mailbox is Google Workspace / Gmail: enable 2FA on the Google Account, "
            "create an App Password (16 letters), put it in SMTP_PASSWORD with no spaces. "
            "If email is hosted by Microsoft 365 / Outlook, set SMTP_HOST and port from your "
            "org's SMTP docs (smtp.office365.com is common); Gmail SMTP will not accept "
            "those credentials."
        )
        suffix = f' Provider message: "{detail}"' if detail else ""
        return False, (
            "SMTP authentication failed. Check SMTP_USER / SMTP_PASSWORD." + suffix + " " + hint
        )
    except smtplib.SMTPException as exc:
        return False, f"SMTP error: {exc}"
    except OSError as exc:
        return False, f"Network error while contacting the mail server: {exc}"

    return True, f"Email sent successfully to {to_address}."


def send_missing_details_email(
    to_address: str,
    provider_display_name: str,
    document_gaps: list[tuple[str, list[str]]],
) -> tuple[bool, str]:
    """
    Notify a provider that some extracted credentialing fields are missing or unclear.

    ``document_gaps`` is a list of ``(filename, [human-readable label, ...])`` with at least
    one label per tuple (callers should skip documents with no gaps).
    """
    to_address = (to_address or "").strip()
    if not to_address:
        return False, "Recipient address is missing."
    if not document_gaps:
        return False, "No missing fields to report."

    settings = _get_smtp_settings()
    missing_cfg = [
        name
        for name, key in [
            ("SMTP_USER", settings["user"]),
            ("SMTP_PASSWORD", settings["password"]),
            ("SMTP_FROM or SMTP_USER", settings["from_addr"]),
        ]
        if not key
    ]
    if missing_cfg:
        return False, (
            "Email is not configured. Set the following in your .env file: "
            + ", ".join(missing_cfg)
        )

    who = (provider_display_name or "").strip() or "Provider"
    lines: list[str] = [
        f"Dear {who},",
        "",
        "During automated review of your submitted documents, the following details were "
        "missing or could not be read clearly. Please reply with the information or upload "
        "clearer copies when you can.",
        "",
    ]
    for filename, labels in document_gaps:
        lines.append(f"• {filename}")
        for lab in labels:
            lines.append(f"    – {lab}")
        lines.append("")
    lines.append("Thank you,")
    lines.append("Credentialing team")

    subject = _strip_env(os.getenv("EMAIL_MISSING_SUBJECT")) or "Action needed: missing credentialing details"
    body = "\n".join(lines)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings["from_addr"]
    message["To"] = to_address
    message.set_content(body)

    context = ssl.create_default_context()

    try:
        with smtplib.SMTP(settings["host"], settings["port"], timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(settings["user"], settings["password"])
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raw = getattr(exc, "smtp_error", b"") or b""
        detail = raw.decode(errors="replace").strip() if isinstance(raw, bytes) else str(raw).strip()
        suffix = f' Provider message: "{detail}"' if detail else ""
        return False, "SMTP authentication failed. Check SMTP_USER / SMTP_PASSWORD." + suffix
    except smtplib.SMTPException as exc:
        return False, f"SMTP error: {exc}"
    except OSError as exc:
        return False, f"Network error while contacting the mail server: {exc}"

    return True, f"Missing-details email sent to {to_address}."
