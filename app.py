"""
Streamlit admin UI: manage provider emails and trigger credentialing notifications.
"""

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
from email_service import send_credentialing_email

# mail_reader / ocr_service are imported lazily where used so the first paint does not
# load IMAP + Google Document AI stacks until you open inbox / run OCR.


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


@st.cache_resource
def _bootstrap_db() -> None:
    """SQLite DDL once per process — avoids repeating work on every Streamlit rerun."""
    db.init_db()


_bootstrap_db()

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
                                db.insert_provider_document(
                                    provider_id=pid,
                                    imap_uid=str(open_uid),
                                    source_subject=detail.get("subject") or "",
                                    source_from=detail.get("from_addr") or "",
                                    filename=fname,
                                    mime_type=att.get("content_type"),
                                    extraction_method=method,
                                    ocr_text=text,
                                    error_message=ocr_err,
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
        if not docs:
            st.info("No processed documents for this provider yet.")
        else:
            preview_rows = [
                {
                    "id": d["id"],
                    "filename": d["filename"],
                    "method": d["extraction_method"],
                    "imap_uid": d["imap_uid"],
                    "subject": (d.get("source_subject") or "")[:60],
                    "error": (d.get("error_message") or "")[:80],
                    "saved": _format_added_at(d["created_at"]),
                    "preview": (d.get("ocr_text") or "")[:200].replace("\n", " "),
                }
                for d in docs
            ]
            st.dataframe(
                preview_rows,
                hide_index=True,
                width="stretch",
                column_config={
                    "id": st.column_config.NumberColumn("Doc ID", width="small"),
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
