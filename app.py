"""
Streamlit admin UI: manage provider emails and trigger credentialing notifications.
"""

import inspect
import json
import os
import tempfile
from datetime import datetime, timezone

import streamlit as st

from service_account_json import normalize_service_account_json_string

# Must be the first Streamlit API call (Community Cloud will show "Error running app" otherwise).
st.set_page_config(
    page_title="Credentialing Admin",
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
from email_service import send_credentialing_email, send_missing_details_email

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
    """Persist manual edits or re-run output; no-op message if the DB module is outdated."""
    upd = getattr(db, "update_document_classification", None)
    if not callable(upd):
        return (
            False,
            "This deployment’s **database.py** is missing `update_document_classification`. "
            "Sync **database.py** from the repo.",
        )
    try:
        upd(int(doc_id), document_category, structured_fields or "{}")
    except (TypeError, ValueError) as exc:
        return False, str(exc)
    return True, "Saved to database."


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


# Run SQLite DDL + migrations on every load so schema upgrades apply after deploy
# (``@st.cache_resource`` previously skipped ``init_db`` on reruns and could leave Cloud DB
# missing new columns → insert failures / TypeErrors).
db.init_db()

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

st.title("Healthcare credentialing (MVP)")
st.caption(
    "Workflow: **Onboard** a provider → **Email & inbox** (send + load a reply) → **Process documents** "
    "(Document AI into SQLite) → **Provider records** to review stored text. "
    "Tip: avoid running the repo from a **synced OneDrive folder** for snappier loads; processing can take **15–60s** per batch."
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
        "Process documents",
        "Provider records",
    ]
)

# --- Tab 1: Admin onboarding — provider name + email ---
with tab_onboard:
    st.subheader("Onboard providers")
    st.caption("Only messages **From** these addresses appear in **Email & inbox**.")
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
            submitted = st.form_submit_button("Save provider", type="primary")

    if submitted:
        ok, msg = db.add_provider(provider_name, new_email)
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
            help="Deletes this row from SQLite; inbox will no longer match this address.",
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
        st.info("No providers onboarded yet. Enter a name and email above to add the first one.")

# --- Tab 2: Send mail + IMAP inbox + load message (preview only) ---
with tab_mail:
    providers = db.list_providers()
    allowed_emails = db.provider_email_set()

    st.subheader("Send credentialing email")
    if not providers:
        st.warning("Add at least one provider in **Provider onboarding** before sending mail.")
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

    st.subheader("Inbox (provider replies only)")
    st.caption(
        "Loads the admin mailbox from `.env` (IMAP). Only messages **From** an onboarded provider address are listed."
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
            help="Scans up to 200 recent messages until this many matches from onboarded senders are found.",
        )

    if fetch_clicked:
        if not allowed_emails:
            st.session_state["inbox_rows"] = []
            st.warning("Onboard at least one provider before loading the inbox.")
        else:
            from mail_reader import list_recent_messages

            with st.spinner("Connecting to mail server (times out after IMAP_TIMEOUT seconds)…"):
                ok, err, rows = list_recent_messages(
                    limit=int(inbox_limit),
                    allowed_sender_emails=allowed_emails,
                )
            if ok:
                st.session_state["inbox_rows"] = rows
                if rows:
                    st.success(f"Loaded {len(rows)} message(s) from onboarded providers.")
                else:
                    st.info(
                        "No messages from onboarded providers in the scanned range, or inbox is empty."
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
                "UID": r.get("uid", ""),
            }
            for r in inbox_rows
        ]
        st.dataframe(
            table_rows,
            hide_index=True,
            width="stretch",
            column_config={
                "Attachments": st.column_config.NumberColumn("# files", width="small"),
                "UID": st.column_config.TextColumn("IMAP UID", width="small"),
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
            help="Only onboarded providers appear in this list.",
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
                        "Open the **Process documents** tab to run **Google Document AI** on these files "
                        "and save text to **provider_documents**."
                    )
                else:
                    st.caption("No file attachments on this message.")
            elif not ok_msg:
                st.error(err_msg or "Could not load message.")
    else:
        if not allowed_emails:
            st.info("Onboard providers first, then click **Refresh inbox**.")
        else:
            st.info(
                "Click **Refresh inbox** to load replies from onboarded providers (requires IMAP in `.env`)."
            )

# --- Tab 3: Document AI processing for the loaded message ---
with tab_proc:
    st.subheader("Process documents")
    st.caption(
        "Uses the message you opened with **Load selected message** in **Email & inbox**. "
        "Runs **Google Document AI** on each attachment. Credentials: **Streamlit Secrets** — "
        "flat keys and/or `[gcp_service_account]` table (see `.streamlit/secrets.toml.example`); "
        "or locally **`.env`** with `GOOGLE_APPLICATION_CREDENTIALS` / `GOOGLE_SERVICE_ACCOUNT_JSON`. "
        "If errors still mention only `.env` paths, **redeploy** the app from latest **main**."
    )

    providers = db.list_providers()
    allowed_emails = db.provider_email_set()
    open_uid = st.session_state.get("open_uid")

    if not open_uid:
        st.info(
            "Go to **Email & inbox**, click **Refresh inbox**, choose a row, then **Load selected message**. "
            "Return here to process attachments."
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
            st.caption(f"IMAP UID **{open_uid}** — {detail.get('date') or ''}")
            atts = detail.get("attachments") or []
            if not atts:
                st.caption("No file attachments on this message.")
            else:
                st.markdown("**Attachments**")
                for att in atts:
                    st.write(f"- {att.get('filename', '(unnamed)')}")
                if st.button(
                    "Process attachments into provider records",
                    type="primary",
                    key=f"proc_{open_uid}",
                ):
                    from mail_reader import sender_email_from_header
                    from ocr_service import extract_text_from_attachment

                    sender_addr = sender_email_from_header(detail["from_addr"])
                    pid = db.get_provider_id_by_email(sender_addr)
                    if pid is None:
                        st.error("Sender email is not linked to an onboarded provider.")
                    else:
                        lines: list[str] = []
                        with st.spinner(
                            "Calling Google Document AI (large PDFs or many files can take 30–90s)…"
                        ):
                            for att in atts:
                                fname = att["filename"]
                                if db.should_skip_attachment_processing(pid, str(open_uid), fname):
                                    lines.append(f"Skipped (already extracted OK): {fname}")
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
                                    lines.append(f"{fname}: saved with error — {ocr_err}")
                                elif ocr_err:
                                    lines.append(f"{fname}: saved ({method}), note — {ocr_err}")
                                else:
                                    lines.append(f"{fname}: saved ({method}, {len(text)} chars)")
                        st.session_state["flash_success"] = "Processed attachments. " + " | ".join(
                            lines
                        )
                        st.rerun()
        elif not ok_msg:
            st.error(err_msg or "Could not load message.")

# --- Tab 4: Roster + processed documents per provider ---
with tab_records:
    st.subheader("Provider records")
    st.caption(
        "Overview of every onboarded provider, then full rows from **provider_documents** "
        "(added when you process attachments). Deleting a provider removes their document rows."
    )

    providers = db.list_providers()
    if not providers:
        st.info("Onboard a provider to see records here.")
    else:
        roster_rows = [
            {
                "id": p["id"],
                "name": (p.get("name") or "").strip() or "—",
                "email": p["email"],
                "stored_docs": int(p.get("document_count") or 0),
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
                "stored_docs": st.column_config.NumberColumn("Stored docs", width="small"),
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

        with st.expander("Clear processed data & inbox session", expanded=False):
            st.caption(
                "Deletes rows in SQLite **`provider_documents`** (OCR text and extracted fields). "
                "**Onboarded providers** are not removed. Inbox buttons only clear this browser session."
            )
            confirm_del_one = st.checkbox(
                f"I understand this removes every stored document for the selected provider (ID {doc_pid}).",
                key="confirm_delete_docs_one",
            )
            if st.button(
                "Delete all processed documents for this provider",
                type="primary",
                disabled=not confirm_del_one,
            ):
                n, msg = db.delete_all_documents_for_provider(doc_pid)
                st.session_state["flash_success"] = msg
                st.session_state.pop("open_uid", None)
                st.rerun()

            st.divider()
            st.markdown("**All providers** — destructive")
            confirm_del_all = st.checkbox(
                "I understand this deletes every processed document for **every** onboarded provider.",
                key="confirm_delete_docs_all",
            )
            wipe_phrase = st.text_input(
                "Type exactly: DELETE ALL DOCUMENTS",
                key="wipe_docs_phrase",
                help="Extra guard so this is not clicked by accident.",
            )
            if st.button(
                "Delete all processed documents (entire database)",
                type="secondary",
                disabled=not confirm_del_all or wipe_phrase.strip() != "DELETE ALL DOCUMENTS",
            ):
                n, msg = db.delete_all_documents_all_providers()
                st.session_state["flash_success"] = msg
                st.session_state.pop("open_uid", None)
                st.rerun()

            st.divider()
            if st.button("Clear inbox session state", type="secondary"):
                st.session_state.pop("inbox_rows", None)
                st.session_state.pop("open_uid", None)
                st.success("Cleared loaded inbox list and open message for this session.")
                st.rerun()

        if not docs:
            st.info("No processed documents for this provider yet.")
        else:
            from document_categorization import (
                categorize_and_structure,
                missing_credentialing_labels,
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
                "Re-run categorization for this provider",
                type="secondary",
                help="Re-applies filename + OCR heuristics to every row (useful after upgrading logic).",
            ):
                upd = getattr(db, "update_document_classification", None)
                if not callable(upd):
                    st.error(
                        "This deployment’s **database.py** is missing `update_document_classification`. "
                        "Sync **database.py** from the repo, then try again."
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
                    st.success(f"Updated {len(docs)} document(s).")
                    st.rerun()

            st.caption(
                "Tables **auto-fill** from stored JSON plus a fresh pass over OCR text (so columns show "
                "even if an older deploy did not save structured fields). Grouping uses the same logic; "
                "click **Re-run categorization** to persist fixes into SQLite."
            )

            with st.expander("Edit extracted fields (manual corrections)", expanded=False):
                st.caption(
                    "Pick a document, set **Document category**, adjust fields, then **Save changes**. "
                    "Only fields relevant to that category are written; other structured keys are left as-is."
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
                with st.form("manual_edit_document_form"):
                    new_cat = st.selectbox("Document category", cats, index=cat_index)
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
                    ok_u, msg_u = _safe_update_document_classification(
                        edit_pick,
                        new_cat,
                        structured_fields_to_json(updated),
                    )
                    if ok_u:
                        st.success(msg_u)
                        st.rerun()
                    else:
                        st.error(msg_u)

            with st.expander("Missing fields — email provider", expanded=False):
                prov_row = id_to_row[doc_pid]
                prov_email = str(prov_row.get("email") or "").strip()
                prov_name = (prov_row.get("name") or "").strip() or "Provider"
                st.caption(
                    "Uses the **same required-field rules** as the tables below. "
                    "The message is sent to this provider’s **onboarded email** (from Provider onboarding), "
                    "not the address read from a résumé."
                )
                gap_list: list[tuple[str, list[str]]] = []
                for d in docs:
                    dc, sf = _display_category_and_fields(d)
                    labels = missing_credentialing_labels(dc, sf)
                    if labels:
                        gap_list.append((str(d.get("filename") or "document"), labels))
                if not gap_list:
                    st.success("No required-field gaps detected for this provider’s documents.")
                else:
                    for fn, labels in gap_list:
                        st.markdown(f"**{fn}**")
                        for lab in labels:
                            st.markdown(f"- {lab}")
                    if st.button(
                        "Send missing-details email to provider",
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
                            "method": d["extraction_method"],
                        }
                    )
                st.dataframe(lic_rows, hide_index=True, width="stretch")

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
                            "method": d["extraction_method"],
                        }
                    )
                st.dataframe(cv_rows, hide_index=True, width="stretch")

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
                            "method": d["extraction_method"],
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
                st.dataframe(oth, hide_index=True, width="stretch")

            st.divider()
            st.markdown("**All documents**")

            def _all_docs_flat_row(d: dict) -> dict:
                dcat, sf = _display_category_and_fields(d)
                summ = (sf.get("description") or "")[:100]
                if len(sf.get("description") or "") > 100:
                    summ += "…"
                return {
                    "id": d["id"],
                    "category": dcat,
                    "filename": d["filename"],
                    "method": d["extraction_method"],
                    "saved": _format_added_at(d["created_at"]),
                    "name": sf.get("name", ""),
                    "email": sf.get("email", ""),
                    "phone": sf.get("phone", ""),
                    "location": sf.get("location", ""),
                    "summary": summ,
                    "license_number": sf.get("license_number", ""),
                    "expiry": sf.get("expiry_date", "") or sf.get("expiration_date", ""),
                    "issue_date": sf.get("issue_date", "") or sf.get("initial_license_date", ""),
                    "signature": sf.get("signature_present", ""),
                    "preview": (d.get("ocr_text") or "")[:100].replace("\n", " "),
                }

            preview_rows = [_all_docs_flat_row(d) for d in docs]
            st.dataframe(
                preview_rows,
                hide_index=True,
                width="stretch",
                column_config={
                    "id": st.column_config.NumberColumn("Doc ID", width="small"),
                    "summary": st.column_config.TextColumn("CV summary"),
                    "preview": st.column_config.TextColumn("Text preview"),
                },
            )
            doc_ids = [d["id"] for d in docs]
            pick_doc = st.selectbox(
                "View full extracted text",
                options=doc_ids,
                format_func=lambda did: next(
                    (f"{d['filename']} (#{d['id']})" for d in docs if d["id"] == did),
                    str(did),
                ),
                key="doc_full_pick",
            )
            full_row = next(d for d in docs if d["id"] == pick_doc)
            st.text_area(
                "Full OCR / extracted text",
                value=full_row.get("ocr_text") or "(empty)",
                height=300,
                disabled=True,
                key=f"fulltext_{pick_doc}",
            )
            if full_row.get("error_message"):
                st.warning(full_row["error_message"])

st.divider()
with st.expander("About this MVP"):
    st.markdown(
        """
        - Emails are stored in **SQLite** (`credentialing.db`).
        - Outbound mail uses **SMTP** from `.env`; inbound uses **IMAP** (defaults: Gmail host, same user/password).
        - The inbox lists only senders whose address was **onboarded** in **Provider onboarding**.
        - **Processed documents** live in table `provider_documents` (linked by `provider_id`); text comes from **Google Document AI**.
        - For **Google**, use an [App Password](https://support.google.com/accounts/answer/185833) with IMAP enabled on the account.
        """
    )
