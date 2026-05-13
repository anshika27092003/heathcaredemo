"""
Read inbound mail via IMAP (Gmail / Google Workspace friendly).

Uses the same credentials as outbound SMTP by default (SMTP_USER / SMTP_PASSWORD).
Optional overrides: IMAP_USER, IMAP_PASSWORD, IMAP_HOST, IMAP_PORT.
"""

from __future__ import annotations

import imaplib
import os
import re
import ssl
from email import policy
from email.header import decode_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import parseaddr
from typing import Any, Optional

from dotenv import load_dotenv

from env_mail_utils import normalize_app_password, strip_env

load_dotenv()


def sender_email_from_header(from_header: str) -> str:
    """Return lowercase address from a From header (handles `Name <addr@x.com>`)."""
    _, addr = parseaddr(from_header or "")
    return addr.strip().lower()


def _decode_mime_header(value: Optional[str]) -> str:
    """Decode RFC 2047 encoded Subject / From lines."""
    if not value:
        return ""
    chunks: list[str] = []
    for part, charset in decode_header(value):
        if isinstance(part, bytes):
            chunks.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            chunks.append(part)
    return "".join(chunks).strip()


def _get_imap_settings() -> dict[str, Optional[str | int]]:
    """Mail credentials for IMAP — reuse SMTP vars unless IMAP_* overrides exist."""
    user = strip_env(os.getenv("IMAP_USER")) or strip_env(os.getenv("SMTP_USER"))
    password = normalize_app_password(
        strip_env(os.getenv("IMAP_PASSWORD")) or strip_env(os.getenv("SMTP_PASSWORD"))
    )
    host = strip_env(os.getenv("IMAP_HOST")) or "imap.gmail.com"
    port = int(os.getenv("IMAP_PORT", "993"))
    mailbox = strip_env(os.getenv("IMAP_MAILBOX")) or "INBOX"
    timeout = int(os.getenv("IMAP_TIMEOUT", "45"))
    timeout = max(10, min(timeout, 300))
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "mailbox": mailbox,
        "timeout": timeout,
    }


def _imap_connect() -> tuple[imaplib.IMAP4_SSL | None, str]:
    """Open an authenticated IMAP SSL session. Returns (client, error_message)."""
    cfg = _get_imap_settings()
    if not cfg["user"] or not cfg["password"]:
        return None, (
            "IMAP is not configured. Set SMTP_USER / SMTP_PASSWORD (shared with IMAP on Gmail) "
            "or IMAP_USER / IMAP_PASSWORD in `.env`."
        )

    ctx = ssl.create_default_context()
    try:
        client = imaplib.IMAP4_SSL(
            cfg["host"],
            int(cfg["port"]),
            ssl_context=ctx,
            timeout=int(cfg["timeout"]),
        )
        client.login(str(cfg["user"]), str(cfg["password"]))
    except imaplib.IMAP4.error as exc:
        return None, f"IMAP login failed: {exc}"
    except (OSError, TimeoutError) as exc:
        return None, (
            f"IMAP connection failed or timed out after {cfg['timeout']}s "
            f"(check network/VPN/firewall and IMAP host): {exc}"
        )

    return client, ""


def _summarize_message(uid: bytes, raw: bytes) -> dict[str, Any]:
    """Parse a full RFC822 blob into summary fields + attachment metadata."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    subject = _decode_mime_header(msg.get("Subject"))
    from_ = _decode_mime_header(msg.get("From"))
    date_ = _decode_mime_header(msg.get("Date"))

    attachment_meta: list[dict[str, Any]] = []
    if msg.is_multipart():
        for part in msg.walk():
            fn = part.get_filename()
            disp = (part.get_content_disposition() or "").lower()
            ctype = part.get_content_type()
            if not fn:
                continue
            # Attachments and inline files with a name (common for credentialing PDFs).
            if disp in ("attachment", "inline") or (
                ctype not in ("text/plain", "text/html") and fn
            ):
                attachment_meta.append({"filename": fn, "content_type": ctype})

    return {
        "uid": uid.decode(),
        "from_addr": from_ or "(unknown sender)",
        "sender_email": sender_email_from_header(from_),
        "subject": subject or "(no subject)",
        "date": date_ or "",
        "attachment_names": [a["filename"] for a in attachment_meta],
        "attachment_count": len(attachment_meta),
    }


def _peek_header_sender_email(client: imaplib.IMAP4_SSL, uid: bytes) -> Optional[str]:
    """Lightweight UID FETCH — From header only (avoids downloading full bodies when filtering)."""
    try:
        typ, data = client.uid(
            "fetch",
            uid,
            "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])",
        )
    except Exception:
        return None
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        return None
    raw = data[0][1]
    if not isinstance(raw, (bytes, bytearray)):
        return None
    try:
        msg = BytesParser(policy=policy.default).parsebytes(bytes(raw))
        return sender_email_from_header(_decode_mime_header(msg.get("From")))
    except Exception:
        return None


def list_recent_messages(
    limit: int = 30,
    allowed_sender_emails: Optional[set[str]] = None,
) -> tuple[bool, str, list[dict[str, Any]]]:
    """
    Fetch messages from the configured mailbox.

    If ``allowed_sender_emails`` is set, only messages whose From address matches one of
    those emails are returned (newest first), scanning recent mail until ``limit`` matches
    or a scan cap is reached.

    If ``allowed_sender_emails`` is None, returns the newest ``limit`` messages (any sender).

    Returns (ok, error_or_empty, rows). Each row includes uid + summary fields.
    """
    limit = max(1, min(int(limit), 100))
    client, err = _imap_connect()
    if client is None:
        return False, err, []

    cfg = _get_imap_settings()
    try:
        typ, _ = client.select(cfg["mailbox"])
        if typ != "OK":
            client.logout()
            return False, f"Could not open mailbox {cfg['mailbox']}", []

        typ, data = client.uid("search", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            client.logout()
            return True, "", []

        uids = data[0].split()
        rows: list[dict[str, Any]] = []

        if allowed_sender_emails is None:
            pick = uids[-limit:] if len(uids) > limit else uids
            for uid in pick:
                typ, msg_data = client.uid("fetch", uid, "(BODY.PEEK[])")
                if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                raw = msg_data[0][1]
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                rows.append(_summarize_message(uid, bytes(raw)))
            rows.reverse()
        else:
            # Phase 1: scan headers only (fast), then download full messages for matches only.
            max_scan = min(len(uids), 200)
            to_scan = uids[-max_scan:]
            allowed = {e.strip().lower() for e in allowed_sender_emails if e.strip()}
            matched_uids: list[bytes] = []
            for uid in reversed(to_scan):
                s_email = _peek_header_sender_email(client, uid)
                if s_email and s_email in allowed:
                    matched_uids.append(uid)
                if len(matched_uids) >= limit:
                    break

            rows = []
            for uid in matched_uids:
                typ, msg_data = client.uid("fetch", uid, "(BODY.PEEK[])")
                if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                raw = msg_data[0][1]
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                rows.append(_summarize_message(uid, bytes(raw)))

        client.logout()
        return True, "", rows
    except imaplib.IMAP4.error as exc:
        try:
            client.logout()
        except Exception:
            pass
        return False, f"IMAP error: {exc}", []
    except (OSError, TimeoutError) as exc:
        try:
            client.logout()
        except Exception:
            pass
        return False, f"IMAP network/timeout error: {exc}", []


def load_message_with_attachments(
    uid: str,
    allowed_sender_emails: Optional[set[str]] = None,
) -> tuple[bool, str, Optional[dict[str, Any]]]:
    """
    Load one message by IMAP UID and return plain/HTML snippet plus attachment payloads.

    If ``allowed_sender_emails`` is set, refuse to load when the sender is not in the set.

    Returns dict with keys: from_addr, subject, date, message_id, body_preview, attachments.
    Each attachment: {filename, content_type, data (bytes)}.
    """
    client, err = _imap_connect()
    if client is None:
        return False, err, None

    cfg = _get_imap_settings()
    try:
        typ, _ = client.select(cfg["mailbox"])
        if typ != "OK":
            client.logout()
            return False, f"Could not open mailbox {cfg['mailbox']}", None

        typ, msg_data = client.uid("fetch", uid.encode(), "(BODY.PEEK[])")
        if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
            client.logout()
            return False, "Message not found or could not be downloaded.", None

        raw = msg_data[0][1]
        if not isinstance(raw, (bytes, bytearray)):
            client.logout()
            return False, "Unexpected message format.", None

        msg = BytesParser(policy=policy.default).parsebytes(bytes(raw))
        from_addr = _decode_mime_header(msg.get("From"))
        subject = _decode_mime_header(msg.get("Subject"))
        date_ = _decode_mime_header(msg.get("Date"))

        sender = sender_email_from_header(from_addr)
        if allowed_sender_emails is not None:
            allowed = {e.strip().lower() for e in allowed_sender_emails if e.strip()}
            if sender not in allowed:
                client.logout()
                return False, "This message is not from an onboarded provider.", None

        body_preview = _extract_body_preview(msg)
        attachments = _extract_attachments(msg)

        message_id = _decode_mime_header(msg.get("Message-ID"))

        client.logout()
        return True, "", {
            "from_addr": from_addr,
            "subject": subject,
            "date": date_,
            "message_id": message_id,
            "body_preview": body_preview,
            "attachments": attachments,
        }
    except imaplib.IMAP4.error as exc:
        try:
            client.logout()
        except Exception:
            pass
        return False, f"IMAP error: {exc}", None
    except (OSError, TimeoutError) as exc:
        try:
            client.logout()
        except Exception:
            pass
        return False, f"IMAP network/timeout error: {exc}", None


def _extract_body_preview(msg: Message, max_chars: int = 8000) -> str:
    """Prefer plain text; fall back to stripped HTML."""
    if msg.is_multipart():
        plain = None
        html = None
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get_content_disposition() or "").lower()
            if disp == "attachment":
                continue
            if ctype == "text/plain" and plain is None:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    plain = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            elif ctype == "text/html" and html is None:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        text = plain or _strip_html_simple(html) if html else ""
    else:
        if msg.get_content_type() == "text/plain":
            payload = msg.get_payload(decode=True)
            text = (
                payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
                if isinstance(payload, bytes)
                else str(payload or "")
            )
        elif msg.get_content_type() == "text/html":
            payload = msg.get_payload(decode=True)
            html = (
                payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
                if isinstance(payload, bytes)
                else str(payload or "")
            )
            text = _strip_html_simple(html)
        else:
            text = ""

    text = (text or "").strip()
    if len(text) > max_chars:
        return text[:max_chars] + "\n\n… (truncated)"
    return text


def _strip_html_simple(html: str) -> str:
    """Very small HTML-to-text helper for preview only."""
    t = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return " ".join(t.split())


def _extract_attachments(msg: EmailMessage | Message) -> list[dict[str, Any]]:
    """Collect attachment parts as raw bytes with filenames."""
    out: list[dict[str, Any]] = []
    if not msg.is_multipart():
        return out

    for part in msg.walk():
        fn = part.get_filename()
        disp = (part.get_content_disposition() or "").lower()
        ctype = part.get_content_type()
        if not fn:
            continue
        if disp not in ("attachment", "inline") and ctype in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        out.append(
            {
                "filename": fn,
                "content_type": ctype,
                "data": payload,
                "size": len(payload),
            }
        )
    return out
