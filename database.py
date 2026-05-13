"""
SQLite persistence for onboarded providers (name + email) and processed attachment text.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Database file lives next to the app (simple deployment).
DB_PATH = Path(__file__).resolve().parent / "credentialing.db"


def _get_connection() -> sqlite3.Connection:
    """Return a SQLite connection with row factory for dict-like rows."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Required so ON DELETE CASCADE removes processed docs when a provider is deleted.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_providers_table(conn: sqlite3.Connection) -> None:
    """Add `name` column when upgrading older DBs that only stored email."""
    cur = conn.execute("PRAGMA table_info(providers)")
    columns = {row[1] for row in cur.fetchall()}
    if "name" not in columns:
        conn.execute("ALTER TABLE providers ADD COLUMN name TEXT NOT NULL DEFAULT ''")


def _migrate_provider_documents_columns(conn: sqlite3.Connection) -> None:
    """Add document_category + structured_fields for OCR post-processing."""
    cur = conn.execute("PRAGMA table_info(provider_documents)")
    columns = {row[1] for row in cur.fetchall()}
    if "document_category" not in columns:
        conn.execute(
            "ALTER TABLE provider_documents ADD COLUMN document_category TEXT NOT NULL DEFAULT 'other'"
        )
    if "structured_fields" not in columns:
        conn.execute(
            "ALTER TABLE provider_documents ADD COLUMN structured_fields TEXT NOT NULL DEFAULT '{}'"
        )


def init_db() -> None:
    """Create tables if they do not exist; apply lightweight migrations."""
    with _get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """
        )
        _migrate_providers_table(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_id INTEGER NOT NULL,
                imap_uid TEXT NOT NULL,
                source_subject TEXT,
                source_from TEXT,
                filename TEXT NOT NULL,
                mime_type TEXT,
                extraction_method TEXT NOT NULL,
                ocr_text TEXT NOT NULL DEFAULT '',
                error_message TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (provider_id) REFERENCES providers(id) ON DELETE CASCADE
            )
            """
        )
        _migrate_provider_documents_columns(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_documents_provider "
            "ON provider_documents(provider_id)"
        )
        conn.commit()


def is_valid_email_format(email: str) -> bool:
    """
    Basic RFC-style check without extra dependencies.
    Good enough for MVP; tighten later if you adopt email-validator.
    """
    email = (email or "").strip()
    if not email or " " in email or email.count("@") != 1:
        return False
    local, domain = email.split("@", 1)
    if not local or not domain or "." not in domain:
        return False
    if ".." in email:
        return False
    return True


def add_provider(name: str, email: str) -> tuple[bool, str]:
    """
    Onboard a provider with display name and email. Returns (success, message).
    Prevents duplicate emails via UNIQUE constraint.
    """
    name = (name or "").strip()
    if not name:
        return False, "Please enter the provider's name."
    if len(name) > 200:
        return False, "Provider name is too long (max 200 characters)."

    email = email.strip().lower()
    if not is_valid_email_format(email):
        return False, "Invalid email format. Please enter a valid address."

    created_at = datetime.now(timezone.utc).isoformat()

    try:
        with _get_connection() as conn:
            conn.execute(
                "INSERT INTO providers (name, email, created_at) VALUES (?, ?, ?)",
                (name, email, created_at),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        return False, "This email is already onboarded."

    return True, "Provider onboarded successfully."


def list_providers() -> list[dict[str, Any]]:
    """Return all providers (newest first) with a count of stored processed documents."""
    with _get_connection() as conn:
        cur = conn.execute(
            """
            SELECT p.id, p.name, p.email, p.created_at,
                   (SELECT COUNT(*) FROM provider_documents d WHERE d.provider_id = p.id)
                   AS document_count
            FROM providers p
            ORDER BY datetime(p.created_at) DESC
            """
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows]


def provider_email_set() -> set[str]:
    """Lowercase emails of all onboarded providers (for inbox filtering)."""
    with _get_connection() as conn:
        cur = conn.execute("SELECT email FROM providers")
        rows = cur.fetchall()
    return {str(row[0]).lower() for row in rows}


def delete_provider(provider_id: int) -> tuple[bool, str]:
    """
    Remove one provider row by primary key.

    Returns (success, message). Fails if the id does not exist.
    """
    try:
        pid = int(provider_id)
    except (TypeError, ValueError):
        return False, "Invalid provider id."

    with _get_connection() as conn:
        cur = conn.execute("DELETE FROM providers WHERE id = ?", (pid,))
        conn.commit()
        if cur.rowcount == 0:
            return False, "No provider found with that ID."

    return True, "Provider removed from the roster."


def get_provider_id_by_email(email: str) -> int | None:
    """Return provider primary key for a normalized email, or None."""
    em = (email or "").strip().lower()
    if not em:
        return None
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM providers WHERE email = ? LIMIT 1", (em,)
        ).fetchone()
    return int(row[0]) if row else None


def should_skip_attachment_processing(provider_id: int, imap_uid: str, filename: str) -> bool:
    """
    True only when the latest saved row for this attachment succeeded.

    Rows with ``failed`` / ``unsupported`` / ``skipped`` allow **Process** again
    (e.g. after fixing Document AI credentials), so old error rows can be replaced.
    """
    with _get_connection() as conn:
        row = conn.execute(
            """
            SELECT extraction_method
            FROM provider_documents
            WHERE provider_id = ? AND imap_uid = ? AND filename = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(provider_id), str(imap_uid), str(filename)),
        ).fetchone()
    if not row:
        return False
    method = row[0]
    if method in ("failed", "unsupported", "skipped"):
        return False
    return method in (
        "document_ai",
        "pdf_text_layer",
        "pdf_text_layer_partial",
        "pdf_ocr",
        "image_ocr",
    )


def delete_failed_extractions_for_attachment(
    provider_id: int,
    imap_uid: str,
    filename: str,
) -> None:
    """Remove prior failed rows so a new **Process** attempt replaces stale errors in the UI."""
    with _get_connection() as conn:
        conn.execute(
            """
            DELETE FROM provider_documents
            WHERE provider_id = ? AND imap_uid = ? AND filename = ?
              AND extraction_method IN ('failed', 'unsupported', 'skipped')
            """,
            (int(provider_id), str(imap_uid), str(filename)),
        )
        conn.commit()


def insert_provider_document(
    provider_id: int,
    imap_uid: str,
    source_subject: str,
    source_from: str,
    filename: str,
    mime_type: str | None,
    extraction_method: str,
    ocr_text: str,
    error_message: str | None = None,
    document_category: str = "other",
    structured_fields: str = "{}",
) -> tuple[bool, str]:
    """Persist one processed attachment row for a provider."""
    # Coerce types so SQLite / json never see surprise objects (avoids TypeError on Cloud).
    pid = int(provider_id)
    method = str(extraction_method or "failed")
    cat = str(document_category or "other").strip() or "other"
    sf_raw = structured_fields if isinstance(structured_fields, str) else "{}"
    if not (sf_raw or "").strip():
        sf_raw = "{}"
    err = None if error_message is None else str(error_message)

    created_at = datetime.now(timezone.utc).isoformat()
    with _get_connection() as conn:
        _migrate_provider_documents_columns(conn)
        conn.execute(
            """
            INSERT INTO provider_documents (
                provider_id, imap_uid, source_subject, source_from,
                filename, mime_type, extraction_method, ocr_text, error_message,
                document_category, structured_fields, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pid,
                str(imap_uid),
                str(source_subject or ""),
                str(source_from or ""),
                str(filename),
                str(mime_type or ""),
                method,
                str(ocr_text or ""),
                err,
                cat,
                sf_raw,
                created_at,
            ),
        )
        conn.commit()
    return True, "Saved processed document."


def update_document_classification(
    doc_id: int,
    document_category: str,
    structured_fields: str,
) -> None:
    """Update category + JSON field blob (used when re-running heuristics on old rows)."""
    with _get_connection() as conn:
        conn.execute(
            """
            UPDATE provider_documents
            SET document_category = ?, structured_fields = ?
            WHERE id = ?
            """,
            (
                (document_category or "other").strip() or "other",
                structured_fields or "{}",
                int(doc_id),
            ),
        )
        conn.commit()


def list_documents_for_provider(provider_id: int) -> list[dict[str, Any]]:
    """All processed attachments for one provider, newest first."""
    with _get_connection() as conn:
        cur = conn.execute(
            """
            SELECT id, imap_uid, source_subject, filename, mime_type,
                   extraction_method, ocr_text, error_message, created_at,
                   document_category, structured_fields
            FROM provider_documents
            WHERE provider_id = ?
            ORDER BY datetime(created_at) DESC
            """,
            (int(provider_id),),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def delete_all_documents_for_provider(provider_id: int) -> tuple[int, str]:
    """Remove every ``provider_documents`` row for one provider. Returns (count deleted, message)."""
    try:
        pid = int(provider_id)
    except (TypeError, ValueError):
        return 0, "Invalid provider id."
    with _get_connection() as conn:
        cur = conn.execute("DELETE FROM provider_documents WHERE provider_id = ?", (pid,))
        conn.commit()
        n = cur.rowcount or 0
    return n, f"Deleted {n} processed document row(s) for this provider."


def delete_all_documents_all_providers() -> tuple[int, str]:
    """Remove every row in ``provider_documents`` (all providers)."""
    with _get_connection() as conn:
        cur = conn.execute("DELETE FROM provider_documents")
        conn.commit()
        n = cur.rowcount or 0
    return n, f"Deleted {n} processed document row(s) across all providers."
