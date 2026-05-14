"""
Streamlit admin UI: manage provider emails and trigger credentialing notifications.
"""

import inspect
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

import streamlit as st

from service_account_json import normalize_service_account_json_string

# Must be the first Streamlit API call (Community Cloud will show "Error running app" otherwise).
st.set_page_config(
    page_title="Credentialing",
    page_icon="📋",
    layout="wide",
)


def _apply_streamlit_secrets_to_environ() -> None:
    """
    Mirror ``st.secrets`` into ``os.environ`` so ``email_service`` / ``mail_reader`` /
    ``ocr_service`` (which use ``getenv``) work on Streamlit Community Cloud.

    Supports:
    - Flat keys (optionally lower_snake_case — normalized to UPPER_SNAKE for ``os.environ``).
    - ``GOOGLE_SERVICE_ACCOUNT_JSON`` as a multi-line JSON string.
    - A nested TOML table whose body looks like a GCP service account, e.g. ``[gcp_service_account]``.
    """
    try:
        sec = st.secrets
    except (FileNotFoundError, RuntimeError, KeyError):
        return

    json_key = "GOOGLE_SERVICE_ACCOUNT_JSON"
    sa_json: str | None = None
    sa_from_table: dict | None = None

    def _env_key(name: str) -> str:
        return str(name).strip().upper()

    def _is_service_account_table(d: object) -> bool:
        if not isinstance(d, dict):
            return False
        if str(d.get("type", "")).strip() == "service_account":
            return True
        return "private_key" in d and "client_email" in d

    for key, val in sec.items():
        if str(key).startswith("_"):
            continue
        if isinstance(val, dict):
            if _is_service_account_table(val):
                sa_from_table = dict(val)
            continue
        if str(key) == json_key:
            sa_json = str(val).strip() if val is not None else None
            continue
        if val is not None and str(val).strip() != "":
            os.environ[_env_key(str(key))] = str(val).strip()

    if not sa_json and sa_from_table:
        try:
            sa_json = json.dumps(sa_from_table)
        except (TypeError, ValueError):
            sa_json = None

    if sa_from_table:
        pid = str(sa_from_table.get("processor_id") or "").strip()
        if pid and not (os.getenv("DOCUMENT_AI_PROCESSOR_ID") or "").strip():
            os.environ["DOCUMENT_AI_PROCESSOR_ID"] = pid
        proj = str(sa_from_table.get("project_id") or "").strip()
        if proj and not (os.getenv("GCP_PROJECT_ID") or "").strip():
            os.environ["GCP_PROJECT_ID"] = proj

    if sa_json:
        sa_json = normalize_service_account_json_string(sa_json)
        try:
            parsed = json.loads(sa_json)
            if isinstance(parsed, dict):
                fd, path = tempfile.mkstemp(prefix="gcp_sa_", suffix=".json")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(parsed, f)
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
                os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = sa_json
        except (json.JSONDecodeError, OSError, TypeError):
            pass


_apply_streamlit_secrets_to_environ()

import database as db
from email_service import send_credentialing_email

try:
    from email_service import send_missing_details_email
except ImportError:
    # Streamlit Cloud / forks sometimes deploy app.py without the matching email_service.py.
    send_missing_details_email = None  # type: ignore[misc, assignment]

try:
    from email_service import send_missing_submission_documents_email
except ImportError:
    send_missing_submission_documents_email = None  # type: ignore[misc, assignment]


def _missing_credentialing_labels_shim(
    category: str,
    sf: dict,
    *,
    only_keys: frozenset | set | None = None,
    **kwargs: object,
) -> list[str]:
    """
    Same rules as ``document_categorization.missing_credentialing_labels`` when that
    symbol is missing on a partial deploy (old ``document_categorization.py``).
    """
    sf = sf or {}
    c = (category or "other").strip().lower()
    if c not in ("license", "cv", "other"):
        c = "other"
    missing: list[str] = []

    def want(rule_key: str) -> bool:
        if only_keys is None or len(only_keys) == 0:
            return True
        return rule_key in only_keys

    def blank(key: str) -> bool:
        v = sf.get(key)
        return v is None or str(v).strip() == ""

    def add(label: str, condition: bool) -> None:
        if condition:
            missing.append(label)

    if c == "license":
        if want("name"):
            add("Full name", blank("name"))
        if want("license_number"):
            add("License number", blank("license_number"))
        if want("expiration"):
            exp_ok = not blank("expiry_date") or not blank("expiration_date")
            add("Expiration date", not exp_ok)
        if want("issue_date"):
            iss_ok = not blank("issue_date") or not blank("initial_license_date")
            add("Issue / initial license date", not iss_ok)
        if want("signature"):
            sig = str(sf.get("signature_present") or "").strip().lower()
            add("Signature status (not confirmed as yes/no)", sig not in ("yes", "no"))
    elif c == "cv":
        if want("name"):
            add("Name", blank("name"))
        if want("email"):
            add("Email", blank("email"))
        if want("phone"):
            add("Phone", blank("phone"))
        if want("location"):
            add("Location / address", blank("location"))
        if want("description"):
            add("Description / CV summary", blank("description"))
    else:
        add("Name", blank("name"))
        add("Email", blank("email"))
        add("Phone", blank("phone"))
        add("License number", blank("license_number"))
        exp_ok = not blank("expiry_date") or not blank("expiration_date")
        add("Expiration date", not exp_ok)
    return missing


try:
    from document_categorization import missing_credentialing_labels as _missing_from_mod
except ImportError:
    _missing_from_mod = None  # type: ignore[misc, assignment]

missing_credentialing_labels = (
    _missing_from_mod if _missing_from_mod is not None else _missing_credentialing_labels_shim
)


def _missing_labels_respecting_provider(dc: str, sf: dict, prov_row: dict) -> list[str]:
    """Apply ``missing_credentialing_labels`` limited to this provider's onboarding field rules."""
    rcf = prov_row.get("required_credentialing_fields")
    if not isinstance(rcf, dict):
        rcf = {}
    only: frozenset[str] | None = None
    if dc == "license":
        lk = rcf.get("license")
        if isinstance(lk, list) and lk:
            only = frozenset(str(x) for x in lk)
    elif dc == "cv":
        ck = rcf.get("cv")
        if isinstance(ck, list) and ck:
            only = frozenset(str(x) for x in ck)
    try:
        return missing_credentialing_labels(dc, sf, only_keys=only)
    except TypeError:
        return missing_credentialing_labels(dc, sf)

# mail_reader / ocr_service are imported lazily where used so the first paint does not
# load IMAP + Google Document AI stacks until you open inbox / run OCR.


def _insert_provider_document_safe(**kwargs: object) -> tuple[bool, str]:
    """
    Call ``database.insert_provider_document`` with only arguments the deployed
    ``database.py`` supports (avoids TypeError when GitHub has new ``app.py`` but an
    older ``database.py`` on another fork/repo).
    """
    sig = inspect.signature(db.insert_provider_document)
    allowed = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return db.insert_provider_document(**allowed)


def _safe_update_document_classification(
    doc_id: int, document_category: str, structured_fields: str
) -> tuple[bool, str]:
    """Persist manual edits or re-run output."""
    _ensure_database_api_polyfills()
    upd = getattr(db, "update_document_classification", None)
    if not callable(upd):
        return (
            False,
            "Saving is not available in this installation. Ask your technical contact to update the app.",
        )
    try:
        upd(int(doc_id), document_category, structured_fields or "{}")
    except (TypeError, ValueError, sqlite3.OperationalError) as exc:
        return False, str(exc)
    return True, "Saved."


def _format_required_docs_cell(req: object) -> str:
    """Short label for roster tables (``required_document_types`` is a list on provider rows)."""
    if not isinstance(req, list) or not req:
        return "—"
    bits = []
    for x in req:
        if x == "license":
            bits.append("License")
        elif x == "cv":
            bits.append("CV")
        else:
            bits.append(str(x))
    return ", ".join(bits)


def _format_required_fields_cell(row: dict) -> str:
    """Summarize per-provider field rules from onboarding."""
    rcf = row.get("required_credentialing_fields")
    dts = row.get("required_document_types")
    if not isinstance(rcf, dict) or not rcf:
        return "All fields"
    parts: list[str] = []
    if isinstance(dts, list):
        for dt in dts:
            if dt not in rcf:
                continue
            keys = rcf[dt]
            abbrev = "Lic." if dt == "license" else "CV"
            if isinstance(keys, list) and keys:
                n = len(keys)
                parts.append(f"{abbrev} ({n} field{'s' if n != 1 else ''})")
    return ", ".join(parts) if parts else "All fields"


def _format_added_at(iso_ts: str) -> str:
    """Turn stored UTC ISO timestamps into a short, human-readable string."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_utc = dt.astimezone(timezone.utc)
        return dt_utc.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso_ts


def _human_required_doc_label(key: str) -> str:
    k = (key or "").strip().lower()
    if k == "license":
        return "License / ID"
    if k == "cv":
        return "CV / résumé"
    return str(key)


def _follow_up_short_label(status: str) -> str:
    s = (status or "").strip().lower()
    if s == "sent_now":
        return "Sent this time"
    if s == "already_sent":
        return "Sent earlier"
    if s == "not_sent":
        return "Not sent"
    return status


def _format_latest_incomplete_cell(latest: object) -> str:
    """One-line summary for the All providers table."""
    if not isinstance(latest, dict):
        return "—"
    try:
        missing = json.loads(str(latest.get("missing_types_json") or "[]"))
    except (json.JSONDecodeError, TypeError):
        missing = []
    if not isinstance(missing, list) or not missing:
        return "—"
    miss_labels = [_human_required_doc_label(str(x)) for x in missing]
    ts = _format_added_at(str(latest.get("logged_at") or ""))
    return f"Missing: {', '.join(miss_labels)} · {ts}"


def _incomplete_event_to_row(r: dict) -> dict:
    """Flatten an incomplete_submission_events row for ``st.dataframe``."""
    try:
        missing = json.loads(str(r.get("missing_types_json") or "[]"))
    except (json.JSONDecodeError, TypeError):
        missing = []
    try:
        present = json.loads(str(r.get("present_types_json") or "[]"))
    except (json.JSONDecodeError, TypeError):
        present = []
    miss_str = ", ".join(_human_required_doc_label(str(x)) for x in missing) or "—"
    pres_str = ", ".join(_human_required_doc_label(str(x)) for x in present) if present else "—"
    return {
        "Logged": _format_added_at(str(r.get("logged_at") or "")),
        "Message ref": str(r.get("imap_uid") or ""),
        "Email subject": (str(r.get("source_subject") or "")[:100] or "—"),
        "Missing": miss_str,
        "Found in email": pres_str,
        "Follow-up email": _follow_up_short_label(str(r.get("follow_up_status") or "")),
    }


def _ensure_database_api_polyfills() -> None:
    """
    Attach APIs that older ``database.py`` forks omit (partial deploys / stale Cloud repo).

    Covers: ``update_document_classification``, ``delete_all_documents_for_provider``,
    ``delete_all_documents_all_providers``.
    """
    get_conn = getattr(db, "_get_connection", None)
    if get_conn is None:
        return

    if not callable(getattr(db, "update_document_classification", None)):

        def _migrate_cols_standalone(conn: sqlite3.Connection) -> None:
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

        def update_document_classification(
            doc_id: int, document_category: str, structured_fields: str
        ) -> None:
            migrate_fn = getattr(db, "_migrate_provider_documents_columns", None)
            with get_conn() as conn:
                if callable(migrate_fn):
                    migrate_fn(conn)
                else:
                    _migrate_cols_standalone(conn)
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

        setattr(db, "update_document_classification", update_document_classification)

    if not callable(getattr(db, "delete_all_documents_for_provider", None)):

        def delete_all_documents_for_provider(provider_id: int) -> tuple[int, str]:
            try:
                pid = int(provider_id)
            except (TypeError, ValueError):
                return 0, "Invalid provider id."
            with get_conn() as conn:
                cur = conn.execute(
                    "DELETE FROM provider_documents WHERE provider_id = ?", (pid,)
                )
                conn.commit()
                n = cur.rowcount or 0
            return n, f"Deleted {n} processed document row(s) for this provider."

        setattr(db, "delete_all_documents_for_provider", delete_all_documents_for_provider)

    if not callable(getattr(db, "delete_all_documents_all_providers", None)):

        def delete_all_documents_all_providers() -> tuple[int, str]:
            with get_conn() as conn:
                cur = conn.execute("DELETE FROM provider_documents")
                conn.commit()
                n = cur.rowcount or 0
            return n, f"Deleted {n} processed document row(s) across all providers."

        setattr(db, "delete_all_documents_all_providers", delete_all_documents_all_providers)


# Run SQLite DDL + migrations on every load so schema upgrades apply after deploy
# (``@st.cache_resource`` previously skipped ``init_db`` on reruns and could leave Cloud DB
# missing new columns → insert failures / TypeErrors).
db.init_db()
_ensure_database_api_polyfills()

# --- Lightweight styling (Streamlit-native, no custom CSS file) ---
st.markdown(
    """
    <style>
    .block-container { padding-top: 1.5rem; max-width: 1100px; }
    h1 { font-weight: 600; letter-spacing: -0.02em; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Healthcare credentialing")
st.caption(
    "Add providers, read their email replies, pull text from attachments, then review everything under **Provider records**. "
    "Reading files can take about half a minute for larger uploads."
)

# --- Session: flash messages after redirect-less reruns ---
if "flash_success" in st.session_state:
    st.success(st.session_state.pop("flash_success"))
if "flash_error" in st.session_state:
    st.error(st.session_state.pop("flash_error"))

tab_onboard, tab_mail, tab_proc, tab_records = st.tabs(
    [
        "Provider onboarding",
        "Email & inbox",
        "Read documents",
        "Provider records",
    ]
)

# --- Tab 1: Admin onboarding — provider name + email ---
with tab_onboard:
    from document_categorization import (
        CREDENTIALING_FIELD_OPTIONS_CV,
        CREDENTIALING_FIELD_OPTIONS_LICENSE,
    )

    st.subheader("Add providers")
    st.caption("Only messages **from** these addresses are listed under **Email & inbox**.")
    col_form, _ = st.columns([2, 1])
    with col_form:
        with st.form("add_provider_form", clear_on_submit=True):
            provider_name = st.text_input(
                "Provider name",
                placeholder="Dr. Jane Smith / City Clinic",
                help="Shown in your roster; used to identify the provider in the app.",
            )
            new_email = st.text_input(
                "Provider email",
                placeholder="name@clinic.example",
                help="Must match the address they send from; duplicates are rejected.",
            )
            required_doc_pick = st.multiselect(
                "Required documents for this provider",
                options=["license", "cv"],
                default=["license", "cv"],
                format_func=lambda k: (
                    "License / professional ID"
                    if k == "license"
                    else "CV / résumé"
                ),
                help=(
                    "After you read attachments on **Read documents**, we check this email for every type you list here. "
                    "If something is still missing, we can email the provider once to send the rest together with what they already sent."
                ),
            )
            license_field_pick: list[str] = []
            cv_field_pick: list[str] = []
            if "license" in required_doc_pick:
                license_field_pick = st.multiselect(
                    "License / professional ID — which details must be filled in",
                    options=[k for k, _ in CREDENTIALING_FIELD_OPTIONS_LICENSE],
                    default=[k for k, _ in CREDENTIALING_FIELD_OPTIONS_LICENSE],
                    format_func=lambda k: next(
                        lab for kk, lab in CREDENTIALING_FIELD_OPTIONS_LICENSE if kk == k
                    ),
                    help=(
                        "For license files, we only flag missing information for the items you leave checked here "
                        "(under **Provider records** and in follow-up emails)."
                    ),
                    key="onboard_license_fields",
                )
            if "cv" in required_doc_pick:
                cv_field_pick = st.multiselect(
                    "CV / résumé — which details must be filled in",
                    options=[k for k, _ in CREDENTIALING_FIELD_OPTIONS_CV],
                    default=[k for k, _ in CREDENTIALING_FIELD_OPTIONS_CV],
                    format_func=lambda k: next(
                        lab for kk, lab in CREDENTIALING_FIELD_OPTIONS_CV if kk == k
                    ),
                    help=(
                        "For résumés, we only flag missing information for the items you leave checked here "
                        "(under **Provider records** and in follow-up emails)."
                    ),
                    key="onboard_cv_fields",
                )
            submitted = st.form_submit_button("Save provider", type="primary")

    if submitted:
        rcf_payload: dict[str, list[str]] = {}
        if "license" in required_doc_pick:
            rcf_payload["license"] = list(license_field_pick)
        if "cv" in required_doc_pick:
            rcf_payload["cv"] = list(cv_field_pick)
        ok, msg = db.add_provider(provider_name, new_email, required_doc_pick, rcf_payload)
        if ok:
            st.session_state["flash_success"] = msg
        else:
            st.session_state["flash_error"] = msg
        st.rerun()

    providers = db.list_providers()

    if providers:
        display_rows = [
            {
                "id": row["id"],
                "name": (row.get("name") or "").strip() or "—",
                "email": row["email"],
                "required_docs": _format_required_docs_cell(row.get("required_document_types")),
                "required_fields": _format_required_fields_cell(row),
                "documents": int(row.get("document_count") or 0),
                "added_at": _format_added_at(row["created_at"]),
            }
            for row in providers
        ]
        st.dataframe(
            display_rows,
            hide_index=True,
            width="stretch",
            column_config={
                "id": st.column_config.NumberColumn("ID", width="small"),
                "name": st.column_config.TextColumn("Provider name"),
                "email": st.column_config.TextColumn("Email"),
                "required_docs": st.column_config.TextColumn("Required docs", width="medium"),
                "required_fields": st.column_config.TextColumn("Required fields", width="medium"),
                "documents": st.column_config.NumberColumn("Stored docs", width="small"),
                "added_at": st.column_config.TextColumn("Added (UTC)"),
            },
        )

        st.markdown("**Remove provider**")
        id_to_row = {int(p["id"]): p for p in providers}
        delete_id = st.selectbox(
            "Choose provider to delete",
            options=list(id_to_row.keys()),
            format_func=lambda i: (
                f"[{i}] {(id_to_row[i].get('name') or '').strip() or '—'} — {id_to_row[i]['email']}"
            ),
            key="delete_provider_pick",
            help="Removes this provider from your list. Their address will no longer appear in the inbox.",
        )
        confirm_delete = st.checkbox(
            "I understand this permanently removes this provider from the roster.",
            key="confirm_delete_provider",
        )
        if st.button("Delete provider", type="secondary", disabled=not confirm_delete):
            ok_del, msg_del = db.delete_provider(delete_id)
            if ok_del:
                st.session_state["flash_success"] = msg_del
                st.session_state.pop("inbox_rows", None)
                st.session_state.pop("open_uid", None)
                st.session_state.pop("delete_provider_pick", None)
                st.session_state.pop("confirm_delete_provider", None)
            else:
                st.session_state["flash_error"] = msg_del
            st.rerun()
    else:
        st.info("No providers yet. Add a name and email above to get started.")

# --- Tab 2: Send mail + IMAP inbox + load message (preview only) ---
with tab_mail:
    providers = db.list_providers()
    allowed_emails = db.provider_email_set()

    st.subheader("Send email to a provider")
    if not providers:
        st.warning("Add a provider under **Provider onboarding** before sending email.")
    else:
        name_by_email = {
            p["email"]: ((p.get("name") or "").strip() or "(no name)") for p in providers
        }
        email_list = [p["email"] for p in providers]
        selected = st.selectbox(
            "Choose provider",
            options=email_list,
            format_func=lambda e: f"{name_by_email[e]} — {e}",
            help="Pick who should receive the automated message.",
            key="send_mail_provider_pick",
        )
        if st.button("Send Email", type="secondary"):
            success, message = send_credentialing_email(selected)
            if success:
                st.success(message)
            else:
                st.error(message)

    st.divider()

    st.subheader("Inbox (replies from your providers)")
    st.caption(
        "Uses the mailbox settings for this app. Only messages **from** addresses you already added under **Provider onboarding** are listed."
    )

    col_refresh, col_limit = st.columns([1, 2])
    with col_refresh:
        fetch_clicked = st.button("Refresh inbox", type="primary")
    with col_limit:
        inbox_limit = st.number_input(
            "Max provider replies to show (newest first)",
            min_value=5,
            max_value=50,
            value=25,
            step=5,
            help="Looks through recent mail until this many matching replies from your providers are found.",
        )

    if fetch_clicked:
        if not allowed_emails:
            st.session_state["inbox_rows"] = []
            st.warning("Add at least one provider before loading the inbox.")
        else:
            from mail_reader import list_recent_messages

            with st.spinner("Connecting to your mailbox…"):
                ok, err, rows = list_recent_messages(
                    limit=int(inbox_limit),
                    allowed_sender_emails=allowed_emails,
                )
            if ok:
                st.session_state["inbox_rows"] = rows
                if rows:
                    st.success(f"Loaded {len(rows)} message(s) from your providers.")
                else:
                    st.info(
                        "No messages from your providers in this range, or the inbox is empty."
                    )
            else:
                st.session_state["inbox_rows"] = []
                st.error(err)

    inbox_rows: list = st.session_state.get("inbox_rows", [])

    if inbox_rows:
        name_by_email = {
            p["email"]: ((p.get("name") or "").strip() or "—") for p in providers
        }
        table_rows = [
            {
                "Date": r.get("date", ""),
                "Provider": name_by_email.get(r.get("sender_email", ""), r.get("sender_email", "")),
                "From": r.get("from_addr", ""),
                "Subject": r.get("subject", ""),
                "Attachments": r.get("attachment_count", 0),
                "Message ref": r.get("uid", ""),
            }
            for r in inbox_rows
        ]
        st.dataframe(
            table_rows,
            hide_index=True,
            width="stretch",
            column_config={
                "Attachments": st.column_config.NumberColumn("# files", width="small"),
                "Message ref": st.column_config.TextColumn("Ref", width="small"),
            },
        )

        labels = []
        uid_by_label: dict[str, str] = {}
        for r in inbox_rows:
            uid = str(r.get("uid", ""))
            subj = str(r.get("subject", ""))[:80]
            sender = str(r.get("from_addr", ""))[:60]
            label = f"[{uid}] {sender} — {subj}"
            labels.append(label)
            uid_by_label[label] = uid

        chosen = st.selectbox(
            "Open a message",
            options=labels,
            help="Only providers you have added appear here.",
            key="inbox_message_pick",
        )

        if st.button("Load selected message", type="secondary"):
            st.session_state["open_uid"] = uid_by_label.get(chosen)

        open_uid = st.session_state.get("open_uid")
        if open_uid:
            from mail_reader import load_message_with_attachments

            with st.spinner("Fetching message…"):
                ok_msg, err_msg, detail = load_message_with_attachments(
                    open_uid,
                    allowed_sender_emails=allowed_emails,
                )
            if ok_msg and detail:
                st.markdown(f"**From:** {detail['from_addr']}")
                st.markdown(f"**Subject:** {detail['subject']}")
                st.caption(detail.get("date") or "")
                st.text_area(
                    "Message preview",
                    value=detail.get("body_preview") or "(no plain text body)",
                    height=220,
                    disabled=True,
                    key="mail_tab_body_preview",
                )
                atts = detail.get("attachments") or []
                if atts:
                    st.markdown("**Attachments**")
                    for att in atts:
                        st.write(f"- {att.get('filename', '(unnamed)')}")
                    st.info(
                        "Go to **Read documents** next to pull text from these files into this provider’s records."
                    )
                else:
                    st.caption("No file attachments on this message.")
            elif not ok_msg:
                st.error(err_msg or "Could not load message.")
    else:
        if not allowed_emails:
            st.info("Add providers first, then click **Refresh inbox**.")
        else:
            st.info(
                "Click **Refresh inbox** to load recent replies from the providers you added."
            )

# --- Tab 3: Read attachments from the opened email ---
with tab_proc:
    st.subheader("Read documents")
    st.caption(
        "Works with the message you opened on **Email & inbox** using **Load selected message**. "
        "Each attachment is read automatically; large files may take up to a minute. "
        "Your workspace needs the usual mail and cloud credentials your team already set up for this app."
    )
    st.caption(
        "After reading files, we compare what we found with the document types you chose under **Provider onboarding**. "
        "If something is still missing, we send **one** reminder email to that provider (outbound mail must be turned on)."
    )

    providers = db.list_providers()
    allowed_emails = db.provider_email_set()
    open_uid = st.session_state.get("open_uid")

    if not open_uid:
        st.info(
            "Go to **Email & inbox**, click **Refresh inbox**, choose a row, then **Load selected message**. "
            "Come back here to read the attachments."
        )
    else:
        from mail_reader import load_message_with_attachments

        with st.spinner("Loading message and attachments…"):
            ok_msg, err_msg, detail = load_message_with_attachments(
                open_uid,
                allowed_sender_emails=allowed_emails,
            )
        if ok_msg and detail:
            st.markdown(f"**From:** {detail['from_addr']}")
            st.markdown(f"**Subject:** {detail['subject']}")
            st.caption(f"Message reference **{open_uid}** — {detail.get('date') or ''}")
            atts = detail.get("attachments") or []
            if not atts:
                st.caption("No file attachments on this message.")
            else:
                st.markdown("**Attachments**")
                for att in atts:
                    st.write(f"- {att.get('filename', '(unnamed)')}")
                if st.button(
                    "Read attachments and save to records",
                    type="primary",
                    key=f"proc_{open_uid}",
                ):
                    from mail_reader import sender_email_from_header
                    from ocr_service import extract_text_from_attachment

                    sender_addr = sender_email_from_header(detail["from_addr"])
                    pid = db.get_provider_id_by_email(sender_addr)
                    if pid is None:
                        st.error("This sender is not on your provider list. Add them under **Provider onboarding**.")
                    else:
                        lines: list[str] = []
                        with st.spinner(
                            "Reading attachments… large PDFs or several files can take up to a minute."
                        ):
                            for att in atts:
                                fname = att["filename"]
                                if db.should_skip_attachment_processing(pid, str(open_uid), fname):
                                    lines.append(f"{fname}: skipped — already saved from this message")
                                    continue
                                db.delete_failed_extractions_for_attachment(pid, str(open_uid), fname)
                                text, method, ocr_err = extract_text_from_attachment(
                                    fname,
                                    att.get("content_type") or "",
                                    att["data"],
                                )
                                from document_categorization import (
                                    categorize_and_structure,
                                    structured_fields_to_json,
                                )

                                cat, fields = categorize_and_structure(fname, text, method)
                                _insert_provider_document_safe(
                                    provider_id=pid,
                                    imap_uid=str(open_uid),
                                    source_subject=detail.get("subject") or "",
                                    source_from=detail.get("from_addr") or "",
                                    filename=fname,
                                    mime_type=att.get("content_type"),
                                    extraction_method=method,
                                    ocr_text=text,
                                    error_message=ocr_err,
                                    document_category=cat,
                                    structured_fields=structured_fields_to_json(fields),
                                )
                                if ocr_err and not text.strip():
                                    lines.append(f"{fname}: saved with a problem — {ocr_err}")
                                elif ocr_err:
                                    lines.append(f"{fname}: saved; note — {ocr_err}")
                                else:
                                    lines.append(f"{fname}: read and saved ({len(text)} characters)")

                        extra_notes: list[str] = []
                        req_fn = getattr(db, "get_provider_required_document_types", None)
                        present_fn = getattr(db, "document_categories_present_for_imap_message", None)
                        reminder_sent_fn = getattr(db, "missing_submission_reminder_was_sent", None)
                        record_rem_fn = getattr(db, "record_missing_submission_reminder_sent", None)
                        if callable(req_fn) and callable(present_fn):
                            required_list = req_fn(pid)
                            required_set = set(required_list)
                            present = present_fn(pid, str(open_uid))
                            missing = required_set - present
                            if required_set and not missing:
                                extra_notes.append(
                                    "Every document type you marked as required for this provider is present in this email."
                                )
                            elif missing:
                                recv_sorted = sorted(required_set & present)
                                miss_sorted = sorted(missing)
                                follow_up = "not_sent"
                                if not callable(reminder_sent_fn) or not callable(record_rem_fn):
                                    extra_notes.append(
                                        "Some required document types are still missing. "
                                        "Automatic reminders are not available until this app is updated—ask your technical contact."
                                    )
                                elif reminder_sent_fn(pid, str(open_uid)):
                                    follow_up = "already_sent"
                                    extra_notes.append(
                                        "Some required document types are still missing. "
                                        "A reminder was already sent for this email."
                                    )
                                else:
                                    prov_row = next(
                                        (
                                            p
                                            for p in db.list_providers()
                                            if int(p["id"]) == int(pid)
                                        ),
                                        None,
                                    )
                                    pe = str((prov_row or {}).get("email") or "").strip()
                                    pname = (
                                        (prov_row or {}).get("name") or ""
                                    ).strip() or "Provider"
                                    mail_fn = send_missing_submission_documents_email
                                    if mail_fn is None:
                                        extra_notes.append(
                                            "Some required document types are still missing, "
                                            "but reminder emails are not available in this version of the app."
                                        )
                                    elif not pe:
                                        extra_notes.append(
                                            "Some required document types are missing, but we do not have an email address on file for this provider."
                                        )
                                    else:
                                        ok_rem, msg_rem = mail_fn(
                                            pe,
                                            pname,
                                            recv_sorted,
                                            miss_sorted,
                                        )
                                        if ok_rem:
                                            record_rem_fn(pid, str(open_uid))
                                            follow_up = "sent_now"
                                            extra_notes.append(msg_rem)
                                        else:
                                            extra_notes.append(
                                                "Could not send the reminder email: " + msg_rem
                                            )

                                log_fn = getattr(db, "log_incomplete_submission_event", None)
                                if callable(log_fn):
                                    log_fn(
                                        pid,
                                        str(open_uid),
                                        miss_sorted,
                                        recv_sorted,
                                        follow_up,
                                        str(detail.get("subject") or ""),
                                    )

                        parts = ["Finished reading attachments. " + " | ".join(lines)]
                        parts.extend(extra_notes)
                        st.session_state["flash_success"] = " ".join(parts)
                        st.rerun()
        elif not ok_msg:
            st.error(err_msg or "Could not load message.")

# --- Tab 4: Roster + processed documents per provider ---
with tab_records:
    st.subheader("Provider records")
    st.caption(
        "See everyone you added, open the files we read from their email, and fix details if needed. "
        "Removing a provider also removes their saved files here."
    )

    providers = db.list_providers()
    if not providers:
        st.info("Add a provider first to see their records here.")
    else:
        inc_map: dict = {}
        lim_fn = getattr(db, "latest_incomplete_submission_map", None)
        if callable(lim_fn):
            try:
                inc_map = lim_fn()
            except (TypeError, sqlite3.OperationalError, ValueError):
                inc_map = {}
        roster_rows = [
            {
                "id": p["id"],
                "name": (p.get("name") or "").strip() or "—",
                "email": p["email"],
                "required_docs": _format_required_docs_cell(p.get("required_document_types")),
                "required_fields": _format_required_fields_cell(p),
                "stored_docs": int(p.get("document_count") or 0),
                "last_incomplete": _format_latest_incomplete_cell(
                    inc_map.get(int(p["id"]))
                ),
                "added_at": _format_added_at(p["created_at"]),
            }
            for p in providers
        ]
        st.markdown("**All providers**")
        st.dataframe(
            roster_rows,
            hide_index=True,
            width="stretch",
            column_config={
                "id": st.column_config.NumberColumn("ID", width="small"),
                "name": st.column_config.TextColumn("Provider name"),
                "email": st.column_config.TextColumn("Email"),
                "required_docs": st.column_config.TextColumn("Required docs", width="medium"),
                "required_fields": st.column_config.TextColumn("Required fields", width="medium"),
                "stored_docs": st.column_config.NumberColumn("Stored docs", width="small"),
                "last_incomplete": st.column_config.TextColumn(
                    "Last incomplete submission", width="large"
                ),
                "added_at": st.column_config.TextColumn("Added (UTC)"),
            },
        )

        st.divider()
        st.markdown("**Processed documents**")
        id_to_row = {int(p["id"]): p for p in providers}
        doc_pid = st.selectbox(
            "Provider",
            options=list(id_to_row.keys()),
            format_func=lambda i: (
                f"[{i}] {(id_to_row[i].get('name') or '').strip() or '—'} — {id_to_row[i]['email']}"
            ),
            key="doc_provider_pick",
        )
        docs = db.list_documents_for_provider(doc_pid)

        gap_fn = getattr(db, "list_incomplete_submission_events", None)
        gap_rows: list = gap_fn(doc_pid, 50) if callable(gap_fn) else []
        if gap_rows:
            st.markdown("**Incomplete email submissions**")
            st.caption(
                "Logged whenever **Read documents** finds a required document type still missing for that email. "
                "This matches the green summary message you see after reading attachments."
            )
            st.dataframe(
                [_incomplete_event_to_row(r) for r in gap_rows],
                hide_index=True,
                width="stretch",
                column_config={
                    "Logged": st.column_config.TextColumn("Logged", width="small"),
                    "Message ref": st.column_config.TextColumn("Ref", width="small"),
                    "Email subject": st.column_config.TextColumn("Subject", width="medium"),
                    "Missing": st.column_config.TextColumn("Missing", width="medium"),
                    "Found in email": st.column_config.TextColumn("Found", width="medium"),
                    "Follow-up email": st.column_config.TextColumn("Follow-up", width="small"),
                },
            )
            st.divider()

        with st.expander("Clear saved files & inbox view", expanded=False):
            st.caption(
                "Removes saved text and fields for processed files. Your provider list stays the same. "
                "Clearing the inbox only affects this browser session."
            )
            confirm_del_one = st.checkbox(
                f"I understand this removes every stored document for the selected provider (ID {doc_pid}).",
                key="confirm_delete_docs_one",
            )
            if st.button(
                "Delete all saved files for this provider",
                type="primary",
                disabled=not confirm_del_one,
            ):
                n, msg = db.delete_all_documents_for_provider(doc_pid)
                st.session_state["flash_success"] = msg
                st.session_state.pop("open_uid", None)
                st.rerun()

            st.divider()
            st.markdown("**All providers** — use with care")
            confirm_del_all = st.checkbox(
                "I understand this deletes every saved file for **every** provider.",
                key="confirm_delete_docs_all",
            )
            wipe_phrase = st.text_input(
                "Type exactly: DELETE ALL DOCUMENTS",
                key="wipe_docs_phrase",
                help="Extra guard so this is not clicked by accident.",
            )
            if st.button(
                "Delete all saved files for every provider",
                type="secondary",
                disabled=not confirm_del_all or wipe_phrase.strip() != "DELETE ALL DOCUMENTS",
            ):
                n, msg = db.delete_all_documents_all_providers()
                st.session_state["flash_success"] = msg
                st.session_state.pop("open_uid", None)
                st.rerun()

            st.divider()
            if st.button("Clear inbox view", type="secondary"):
                st.session_state.pop("inbox_rows", None)
                st.session_state.pop("open_uid", None)
                st.success("Cleared the inbox list and the open message for this session.")
                st.rerun()

        if not docs:
            st.info("No processed documents for this provider yet.")
        else:
            from document_categorization import (
                categorize_and_structure,
                structured_fields_to_json,
            )

            def _parse_structured(raw: object) -> dict:
                if raw is None:
                    return {}
                try:
                    return json.loads(str(raw))
                except json.JSONDecodeError:
                    return {}

            def _display_category_and_fields(d: dict) -> tuple[str, dict]:
                """
                Merge DB ``structured_fields`` with a fresh pass over ``ocr_text``.

                Fills table columns even when older inserts omitted JSON in SQLite (e.g. compat
                ``insert_provider_document``) or ``document_category`` stayed ``other``.
                """
                sf = dict(_parse_structured(d.get("structured_fields")))
                meth = str(d.get("extraction_method") or "")
                text = (d.get("ocr_text") or "").strip()
                cat = str(d.get("document_category") or "other").strip().lower()
                if text and meth not in ("failed", "unsupported", "skipped"):
                    c2, f2 = categorize_and_structure(d["filename"], text, meth)
                    manual = str(sf.get("_manual_saved") or "").strip().lower() in (
                        "1",
                        "true",
                        "yes",
                    )
                    if not manual:
                        for k, v in f2.items():
                            if v is None:
                                continue
                            cur = sf.get(k)
                            cur_s = ("" if cur is None else str(cur)).strip()
                            new_s = str(v).strip()
                            if new_s and not cur_s:
                                sf[k] = v
                    if cat == "other" and c2 in ("license", "cv"):
                        cat = c2
                if cat not in ("license", "cv", "other"):
                    cat = "other"
                return cat, sf

            if st.button(
                "Re-check document types for this provider",
                type="secondary",
                help="Runs the automatic type detection again on every saved file (useful after an app update).",
            ):
                upd = getattr(db, "update_document_classification", None)
                if not callable(upd):
                    st.error(
                        "Saving changes is not available in this installation. Ask your technical contact to update the app."
                    )
                else:
                    for d in docs:
                        cat, fld = categorize_and_structure(
                            d["filename"],
                            d.get("ocr_text") or "",
                            d.get("extraction_method") or "",
                        )
                        db.update_document_classification(
                            d["id"], cat, structured_fields_to_json(fld)
                        )
                    st.success(f"Re-checked {len(docs)} file(s).")
                    st.rerun()

            st.caption(
                "Tables fill in from saved data and a fresh pass over the original text. "
                "Use **Re-check document types** if columns look wrong, then save edits below if you need to correct details by hand."
            )

            _docs_tbl_rev = int(st.session_state.get("docs_table_revision", 0))

            buckets: dict[str, list] = {"license": [], "cv": [], "other": []}
            for d in docs:
                dc, _sf = _display_category_and_fields(d)
                buckets[dc if dc in buckets else "other"].append(d)

            if buckets["license"]:
                st.markdown("##### License / ID")
                lic_rows = []
                for d in buckets["license"]:
                    _, sf = _display_category_and_fields(d)
                    lic_rows.append(
                        {
                            "id": d["id"],
                            "filename": d["filename"],
                            "name": sf.get("name", ""),
                            "license_number": sf.get("license_number", ""),
                            "initial_license_date": sf.get("initial_license_date", "")
                            or sf.get("issue_date", ""),
                            "issue_date": sf.get("issue_date", ""),
                            "expiry_date": sf.get("expiry_date", "")
                            or sf.get("expiration_date", ""),
                            "expiration_date": sf.get("expiration_date", "")
                            or sf.get("expiry_date", ""),
                            "signature": sf.get("signature_present", ""),
                            "How read": d["extraction_method"],
                        }
                    )
                st.dataframe(
                    lic_rows,
                    hide_index=True,
                    width="stretch",
                    key=f"tbl_lic_{doc_pid}_{_docs_tbl_rev}",
                    column_config={
                        "How read": st.column_config.TextColumn("How read", width="small"),
                    },
                )

            if buckets["cv"]:
                st.markdown("##### CV / résumé")
                cv_rows = []
                for d in buckets["cv"]:
                    _, sf = _display_category_and_fields(d)
                    desc = sf.get("description") or ""
                    if len(desc) > 300:
                        desc = desc[:300].rsplit(" ", 1)[0] + "…"
                    cv_rows.append(
                        {
                            "id": d["id"],
                            "filename": d["filename"],
                            "name": sf.get("name", ""),
                            "email": sf.get("email", ""),
                            "phone": sf.get("phone", ""),
                            "location": sf.get("location", ""),
                            "description": desc,
                            "How read": d["extraction_method"],
                        }
                    )
                st.dataframe(
                    cv_rows,
                    hide_index=True,
                    width="stretch",
                    key=f"tbl_cv_{doc_pid}_{_docs_tbl_rev}",
                    column_config={
                        "How read": st.column_config.TextColumn("How read", width="small"),
                    },
                )

            if buckets["other"]:
                st.markdown("##### Other")
                oth = []
                for d in buckets["other"]:
                    _, sf = _display_category_and_fields(d)
                    desc = (sf.get("description") or "")[:120]
                    if len(sf.get("description") or "") > 120:
                        desc += "…"
                    oth.append(
                        {
                            "id": d["id"],
                            "filename": d["filename"],
                            "How read": d["extraction_method"],
                            "name": sf.get("name", ""),
                            "email": sf.get("email", ""),
                            "phone": sf.get("phone", ""),
                            "location": sf.get("location", ""),
                            "license_number": sf.get("license_number", ""),
                            "expiry": sf.get("expiry_date", "") or sf.get("expiration_date", ""),
                            "issue_date": sf.get("issue_date", "") or sf.get("initial_license_date", ""),
                            "signature": sf.get("signature_present", ""),
                            "summary": desc,
                            "saved": _format_added_at(d["created_at"]),
                            "preview": (d.get("ocr_text") or "")[:120].replace("\n", " "),
                        }
                    )
                st.dataframe(
                    oth,
                    hide_index=True,
                    width="stretch",
                    key=f"tbl_oth_{doc_pid}_{_docs_tbl_rev}",
                    column_config={
                        "How read": st.column_config.TextColumn("How read", width="small"),
                    },
                )

            st.divider()
            st.markdown("**All documents**")

            def _all_docs_flat_row(d: dict) -> dict:
                dcat, sf = _display_category_and_fields(d)
                summ = (sf.get("description") or "")[:100]
                if len(sf.get("description") or "") > 100:
                    summ += "…"
                return {
                    "id": d["id"],
                    "Document type": {
                        "license": "License / ID",
                        "cv": "CV / résumé",
                        "other": "Other",
                    }.get(dcat, dcat),
                    "filename": d["filename"],
                    "How read": d["extraction_method"],
                    "name": sf.get("name", ""),
                    "email": sf.get("email", ""),
                    "phone": sf.get("phone", ""),
                    "location": sf.get("location", ""),
                    "summary": summ,
                    "license_number": sf.get("license_number", ""),
                    "expiry": sf.get("expiry_date", "") or sf.get("expiration_date", ""),
                    "issue_date": sf.get("issue_date", "") or sf.get("initial_license_date", ""),
                    "signature": sf.get("signature_present", ""),
                    "saved": _format_added_at(d["created_at"]),
                    "preview": (d.get("ocr_text") or "")[:100].replace("\n", " "),
                }

            preview_rows = [_all_docs_flat_row(d) for d in docs]
            st.dataframe(
                preview_rows,
                hide_index=True,
                width="stretch",
                key=f"tbl_all_{doc_pid}_{_docs_tbl_rev}",
                column_config={
                    "id": st.column_config.NumberColumn("Doc ID", width="small"),
                    "summary": st.column_config.TextColumn("Summary"),
                    "preview": st.column_config.TextColumn("Text preview"),
                    "How read": st.column_config.TextColumn("How read", width="small"),
                },
            )
            doc_ids = [d["id"] for d in docs]
            pick_doc = st.selectbox(
                "View full text from file",
                options=doc_ids,
                format_func=lambda did: next(
                    (f"{d['filename']} (#{d['id']})" for d in docs if d["id"] == did),
                    str(did),
                ),
                key="doc_full_pick",
            )
            full_row = next(d for d in docs if d["id"] == pick_doc)
            st.text_area(
                "Full text from file",
                value=full_row.get("ocr_text") or "(empty)",
                height=300,
                disabled=True,
                key=f"fulltext_{pick_doc}",
            )
            if full_row.get("error_message"):
                st.warning(full_row["error_message"])

            with st.expander("Fix details by hand", expanded=False):
                st.caption(
                    "Choose a file, pick its **Document type**, change any fields, then **Save changes**. "
                    "Updates show in the tables above after the page refreshes."
                )
                edit_pick = st.selectbox(
                    "Document to edit",
                    options=[d["id"] for d in docs],
                    format_func=lambda i: next(
                        (f"{x['filename']} (#{x['id']})" for x in docs if x["id"] == i),
                        str(i),
                    ),
                    key="manual_edit_doc_pick",
                )
                erow = next(d for d in docs if d["id"] == edit_pick)
                dcat, merged = _display_category_and_fields(erow)
                cats = ("license", "cv", "other")
                cat_index = cats.index(dcat) if dcat in cats else 2
                _type_labels = {"license": "License / ID", "cv": "CV / résumé", "other": "Other"}
                with st.form("manual_edit_document_form"):
                    new_cat = st.selectbox(
                        "Document type",
                        cats,
                        index=cat_index,
                        format_func=lambda c: _type_labels.get(str(c), str(c)),
                    )
                    st.markdown("**Person & contact**")
                    f_name = st.text_input(
                        "Name / full name",
                        value=str(merged.get("name") or ""),
                        key=f"ed_name_{edit_pick}",
                    )
                    f_email = st.text_input(
                        "Email",
                        value=str(merged.get("email") or ""),
                        key=f"ed_email_{edit_pick}",
                    )
                    f_phone = st.text_input(
                        "Phone",
                        value=str(merged.get("phone") or ""),
                        key=f"ed_phone_{edit_pick}",
                    )
                    f_location = st.text_input(
                        "Location",
                        value=str(merged.get("location") or ""),
                        key=f"ed_loc_{edit_pick}",
                    )
                    f_desc = st.text_area(
                        "Description / CV summary",
                        value=str(merged.get("description") or ""),
                        height=100,
                        key=f"ed_desc_{edit_pick}",
                    )
                    st.markdown("**License / ID**")
                    f_lic = st.text_input(
                        "License number",
                        value=str(merged.get("license_number") or ""),
                        key=f"ed_lic_{edit_pick}",
                    )
                    f_iss = st.text_input(
                        "Issue date",
                        value=str(merged.get("issue_date") or ""),
                        key=f"ed_iss_{edit_pick}",
                    )
                    f_init = st.text_input(
                        "Initial license date",
                        value=str(merged.get("initial_license_date") or ""),
                        key=f"ed_init_{edit_pick}",
                    )
                    f_exp = st.text_input(
                        "Expiry date",
                        value=str(merged.get("expiry_date") or ""),
                        key=f"ed_exp_{edit_pick}",
                    )
                    f_exp2 = st.text_input(
                        "Expiration date (alt)",
                        value=str(merged.get("expiration_date") or ""),
                        key=f"ed_exp2_{edit_pick}",
                    )
                    f_sig = st.text_input(
                        "Signature present (yes / no / unknown)",
                        value=str(merged.get("signature_present") or ""),
                        key=f"ed_sig_{edit_pick}",
                    )
                    save_edits = st.form_submit_button("Save changes", type="primary")
                if save_edits:
                    pack = {
                        "name": f_name,
                        "email": f_email,
                        "phone": f_phone,
                        "location": f_location,
                        "description": f_desc,
                        "license_number": f_lic,
                        "issue_date": f_iss,
                        "initial_license_date": f_init,
                        "expiry_date": f_exp,
                        "expiration_date": f_exp2,
                        "signature_present": f_sig,
                    }
                    if new_cat == "license":
                        keys = {
                            "name",
                            "license_number",
                            "issue_date",
                            "initial_license_date",
                            "expiry_date",
                            "expiration_date",
                            "signature_present",
                        }
                    elif new_cat == "cv":
                        keys = {"name", "email", "phone", "location", "description"}
                    else:
                        keys = set(pack.keys())
                    updated = dict(merged)
                    for k in keys:
                        updated[k] = pack.get(k, "")
                    updated["_manual_saved"] = "1"
                    ok_u, msg_u = _safe_update_document_classification(
                        edit_pick,
                        new_cat,
                        structured_fields_to_json(updated),
                    )
                    if ok_u:
                        st.session_state["docs_table_revision"] = (
                            int(st.session_state.get("docs_table_revision", 0)) + 1
                        )
                        st.success(msg_u)
                        st.rerun()
                    else:
                        st.error(msg_u)

            with st.expander("Missing information — email the provider", expanded=False):
                if send_missing_details_email is None:
                    st.warning(
                        "Sending follow-up email from this screen is not available in this installation. "
                        "Ask your technical contact to update the app."
                    )
                else:
                    prov_row = id_to_row[doc_pid]
                    prov_email = str(prov_row.get("email") or "").strip()
                    prov_name = (prov_row.get("name") or "").strip() or "Provider"
                    st.caption(
                        "Uses the same checks as the tables above, limited to the fields you chose when you added this provider. "
                        "The email goes to the address saved for them under **Provider onboarding**, not an address taken from a résumé."
                    )
                    gap_list: list[tuple[str, list[str]]] = []
                    for d in docs:
                        dc, sf = _display_category_and_fields(d)
                        labels = _missing_labels_respecting_provider(dc, sf, id_to_row[doc_pid])
                        if labels:
                            gap_list.append((str(d.get("filename") or "document"), labels))
                    if not gap_list:
                        st.success("No gaps found for the items you marked as required.")
                    else:
                        for fn, labels in gap_list:
                            st.markdown(f"**{fn}**")
                            for lab in labels:
                                st.markdown(f"- {lab}")
                        if st.button(
                            "Email provider about missing information",
                            type="primary",
                            key="btn_send_missing_email",
                        ):
                            ok_m, msg_m = send_missing_details_email(
                                prov_email,
                                prov_name,
                                gap_list,
                            )
                            if ok_m:
                                st.success(msg_m)
                            else:
                                st.error(msg_m)

st.divider()
with st.expander("How this app works", expanded=False):
    st.markdown(
        """
        - **Provider onboarding** is your approved list of people and the document types (and fields) you care about.
        - **Email & inbox** reads the mailbox you configured for this workspace and shows replies from people on that list.
        - **Read documents** pulls readable text from attachments and groups them as license, CV, or other.
        - **Provider records** is where you review what was read and send gentle follow-up emails if something is incomplete.
        - If you use Gmail or Google Workspace, turn on two-step verification and create an [app password](https://support.google.com/accounts/answer/185833) for the mailbox you connect here.
        """
    )
