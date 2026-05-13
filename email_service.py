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

from credential_documents import REQUIRED_CREDENTIAL_DOCUMENTS, build_attachment_match_report

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


def send_missing_credentialing_documents_email(
    to_address: str,
    attachment_filenames: list[str],
    original_subject: Optional[str] = None,
    in_reply_to_message_id: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Notify a provider which credentialing attachment types are **still pending**, based on
    attachment **filenames**. Lists what we could recognize as received and what remains missing.

    Uses the same SMTP settings as ``send_credentialing_email``. Optionally sets
    ``In-Reply-To`` / ``References`` when ``in_reply_to_message_id`` is provided.
    """
    to_address = to_address.strip()
    if not to_address:
        return False, "Recipient address is missing."

    report = build_attachment_match_report(attachment_filenames)
    missing_labels = list(report.missing_categories)
    if not missing_labels:
        return True, "Nothing to send (all required documents were matched)."

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

    numbered_required = "\n".join(
        f"  {i}. {label}" for i, label in enumerate(REQUIRED_CREDENTIAL_DOCUMENTS, start=1)
    )

    received_lines: list[str] = []
    for label in REQUIRED_CREDENTIAL_DOCUMENTS:
        files = report.matched_by_category.get(label, ())
        if files:
            received_lines.append(f"  - {label}: {', '.join(files)}")
    received_block = (
        "\n".join(received_lines)
        if received_lines
        else "  (none of the five types could be matched from the current file names.)"
    )

    pending_bullets = "\n".join(f"  - {m}" for m in missing_labels)

    unc = list(report.unmatched_filenames)
    unc_block = (
        "\n".join(f"  - {u}" for u in unc)
        if unc
        else "  (none — every attachment name matched one of the five types.)"
    )

    body = f"""Dear Provider,

Thank you for your message. We reviewed the **file names** of your attachments (automated matching).

**We recognized these required documents (from file names):**
{received_block}

**These required documents are still pending — please submit them as well** (you may include them in a reply together with any updated files):
{pending_bullets}

**Attachments we could not match** to a required type (if one of these is a required document, please rename the file, e.g. dea_certificate.pdf, board_certification.pdf, insurance.pdf):
{unc_block}

Please send **all remaining items** when you can, ideally in **one** email with clear names.

Full checklist (all five are required):
{numbered_required}

Regards,
Admin Team"""

    subject = "Missing credentialing documents — please send all items together"
    if original_subject and original_subject.strip():
        subject = f"Re: {original_subject.strip()[:180]}"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings["from_addr"]
    message["To"] = to_address
    message.set_content(body)

    mid = (in_reply_to_message_id or "").strip()
    if mid:
        if not mid.startswith("<"):
            mid = f"<{mid}>" if "@" in mid else mid
        message["In-Reply-To"] = mid
        message["References"] = mid

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
        if isinstance(raw, bytes):
            detail = raw.decode(errors="replace").strip()
        else:
            detail = str(raw).strip()
        suffix = f' Provider message: "{detail}"' if detail else ""
        return False, "SMTP authentication failed while sending missing-documents notice." + suffix
    except smtplib.SMTPException as exc:
        return False, f"SMTP error: {exc}"
    except OSError as exc:
        return False, f"Network error while contacting the mail server: {exc}"

    return True, f"Missing-documents notice sent to {to_address}."
