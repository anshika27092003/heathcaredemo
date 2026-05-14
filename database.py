"""
SQLite persistence for onboarded providers (name + email) and processed attachment text.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Document types the admin can mark as required at onboarding (matches ``document_categorization``).
ALLOWED_REQUIRED_DOC_TYPES = frozenset({"license", "cv"})

_OK_EXTRACTION_METHODS = (
    "document_ai",
    "pdf_text_layer",
    "pdf_text_layer_partial",
    "pdf_ocr",
    "image_ocr",
)

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


def _migrate_providers_required_documents(conn: sqlite3.Connection) -> None:
    """Per-provider list of required submission types (license / cv), stored as JSON array."""
    cur = conn.execute("PRAGMA table_info(providers)")
    columns = {row[1] for row in cur.fetchall()}
    if "required_document_types" not in columns:
        # SQLite does not allow bound parameters in ALTER TABLE … DEFAULT (see sqlite.org/lang_altertable.html).
        default_json = json.dumps(["license", "cv"])
        escaped = default_json.replace("'", "''")
        conn.execute(
            "ALTER TABLE providers ADD COLUMN required_document_types TEXT NOT NULL "
            f"DEFAULT '{escaped}'"
        )


def _migrate_missing_submission_reminders(conn: sqlite3.Connection) -> None:
    """Track one outbound reminder per (provider, IMAP message) so re-clicks do not spam."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS missing_submission_reminders (
            provider_id INTEGER NOT NULL,
            imap_uid TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (provider_id, imap_uid),
            FOREIGN KEY (provider_id) REFERENCES providers(id) ON DELETE CASCADE
        )
        """
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
        _migrate_providers_required_documents(conn)
        _migrate_missing_submission_reminders(conn)
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


def normalize_required_document_types(raw: list[str] | tuple[str, ...] | None) -> list[str]:
    """Return a stable list of ``license`` / ``cv`` keys (subset of ``ALLOWED_REQUIRED_DOC_TYPES``)."""
    out: list[str] = []
    for x in raw or ():
        key = str(x).strip().lower()
        if key in ALLOWED_REQUIRED_DOC_TYPES and key not in out:
            out.append(key)
    return out


def add_provider(
    name: str,
    email: str,
    required_document_types: list[str] | tuple[str, ...] | None = None,
) -> tuple[bool, str]:
    """
    Onboard a provider with display name and email. Returns (success, message).
    Prevents duplicate emails via UNIQUE constraint.

    ``required_document_types`` must list at least one of: ``license``, ``cv``.
    """
    name = (name or "").strip()
    if not name:
        return False, "Please enter the provider's name."
    if len(name) > 200:
        return False, "Provider name is too long (max 200 characters)."

    email = email.strip().lower()
    if not is_valid_email_format(email):
        return False, "Invalid email format. Please enter a valid address."

    req = normalize_required_document_types(
        list(required_document_types) if required_document_types is not None else None
    )
    if not req:
        return (
            False,
            "Select at least one required document type (License / ID and/or CV / résumé).",
        )

    created_at = datetime.now(timezone.utc).isoformat()
    req_json = json.dumps(req)

    try:
        with _get_connection() as conn:
            _migrate_providers_required_documents(conn)
            conn.execute(
                "INSERT INTO providers (name, email, created_at, required_document_types) "
                "VALUES (?, ?, ?, ?)",
                (name, email, created_at, req_json),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        return False, "This email is already onboarded."

    return True, "Provider onboarded successfully."


def list_providers() -> list[dict[str, Any]]:
    """Return all providers (newest first) with a count of stored processed documents."""
    with _get_connection() as conn:
        _migrate_providers_required_documents(conn)
        cur = conn.execute(
            """
            SELECT p.id, p.name, p.email, p.created_at, p.required_document_types,
                   (SELECT COUNT(*) FROM provider_documents d WHERE d.provider_id = p.id)
                   AS document_count
            FROM providers p
            ORDER BY datetime(p.created_at) DESC
            """
        )
        rows = cur.fetchall()
    out = [dict(row) for row in rows]
    for r in out:
        r["required_document_types"] = parse_required_document_types_column(
            r.get("required_document_types")
        )
    return out


def parse_required_document_types_column(value: object) -> list[str]:
    """Parse JSON array from DB; fall back to both types if missing or invalid."""
    default = ["license", "cv"]
    if value is None:
        return list(default)
    s = str(value).strip()
    if not s:
        return list(default)
    try:
        data = json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return list(default)
    if not isinstance(data, list):
        return list(default)
    norm = normalize_required_document_types([str(x) for x in data])
    return norm if norm else list(default)


def get_provider_required_document_types(provider_id: int) -> list[str]:
    """Ordered list of required types for preprocessing (license / cv)."""
    try:
        pid = int(provider_id)
    except (TypeError, ValueError):
        return list(parse_required_document_types_column(None))
    with _get_connection() as conn:
        _migrate_providers_required_documents(conn)
        row = conn.execute(
            "SELECT required_document_types FROM providers WHERE id = ? LIMIT 1",
            (pid,),
        ).fetchone()
    if not row:
        return []
    return parse_required_document_types_column(row[0])


def document_categories_present_for_imap_message(provider_id: int, imap_uid: str) -> set[str]:
    """
    Distinct ``license`` / ``cv`` categories among successfully extracted rows
    for one inbound message (same IMAP UID).
    """
    pid = int(provider_id)
    uid = str(imap_uid)
    placeholders = ",".join("?" * len(_OK_EXTRACTION_METHODS))
    with _get_connection() as conn:
        cur = conn.execute(
            f"""
            SELECT DISTINCT document_category
            FROM provider_documents
            WHERE provider_id = ? AND imap_uid = ?
              AND extraction_method IN ({placeholders})
            """,
            (pid, uid, *_OK_EXTRACTION_METHODS),
        )
        raw = {str(r[0]).strip().lower() for r in cur.fetchall()}
    return raw & ALLOWED_REQUIRED_DOC_TYPES


def missing_submission_reminder_was_sent(provider_id: int, imap_uid: str) -> bool:
    with _get_connection() as conn:
        _migrate_missing_submission_reminders(conn)
        row = conn.execute(
            """
            SELECT 1 FROM missing_submission_reminders
            WHERE provider_id = ? AND imap_uid = ?
            LIMIT 1
            """,
            (int(provider_id), str(imap_uid)),
        ).fetchone()
    return row is not None


def record_missing_submission_reminder_sent(provider_id: int, imap_uid: str) -> None:
    sent_at = datetime.now(timezone.utc).isoformat()
    with _get_connection() as conn:
        _migrate_missing_submission_reminders(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO missing_submission_reminders (provider_id, imap_uid, sent_at)
            VALUES (?, ?, ?)
            """,
            (int(provider_id), str(imap_uid), sent_at),
        )
        conn.commit()


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
