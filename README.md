# Healthcare Credentialing MVP (Streamlit)

A small admin tool to collect provider email addresses in SQLite, send credentialing requests via SMTP, and **read inbound replies** (any sender) with **attachments** via IMAP (Gmail / Workspace-friendly).

## Prerequisites

- Python 3.10 or newer recommended
- A Gmail / Google Workspace mailbox (or compatible SMTP+IMAP host) for sending and reading mail

## Enable reading mail (Google)

For Gmail / Workspace inboxes, turn **IMAP on**: Gmail settings → **Forwarding and POP/IMAP** → **Enable IMAP**. The same **App Password** used for SMTP usually works for IMAP.

If email is hosted elsewhere (for example Microsoft 365), use your provider’s **SMTP** and **IMAP** hostnames in `.env` (`SMTP_*` and optional `IMAP_*`).

## Setup

1. **Create a virtual environment** (recommended):

   ```bash
   python -m venv .venv
   ```

   Activate it:

   - Windows (PowerShell): `.venv\Scripts\Activate.ps1`
   - macOS/Linux: `source .venv/bin/activate`

2. **Install dependencies**:

   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables**:

   - Copy `.env.example` to `.env`
   - Set `SMTP_USER`, `SMTP_PASSWORD`, and optionally `SMTP_FROM`
   - For Gmail with 2FA: create an App Password and use it as `SMTP_PASSWORD`
   - Inbound mail uses the **same** credentials unless you set optional `IMAP_USER` / `IMAP_PASSWORD` / `IMAP_HOST`
   - **Attachment OCR** uses **Google Cloud Document AI**: put a service account JSON on disk, set `GOOGLE_APPLICATION_CREDENTIALS` to its path, and set `DOCUMENT_AI_PROCESSOR_ID` (and `DOCUMENT_AI_LOCATION` if not `us`). See `.env.example`.

   ```bash
   streamlit run app.py
   ```

   This project is configured to use **port 8502** (see `.streamlit/config.toml`) so the default **8501** stays available for other Streamlit apps. Open **http://localhost:8502** after starting the server. Use **Refresh inbox** to load recent mail from **any sender**, preview bodies, and download attachments.

## Project files

| File             | Purpose                                      |
| ---------------- | -------------------------------------------- |
| `app.py`         | Streamlit UI                                 |
| `database.py`    | SQLite access, validation, duplicate checks  |
| `email_service.py` | SMTP send + env-based configuration      |
| `mail_reader.py` | IMAP: list messages + fetch attachments          |
| `ocr_service.py` | Google Document AI text extraction for attachments |
| `requirements.txt` | Python dependencies                      |
| `.env.example`   | Template for secrets (copy to `.env`)      |
| `.streamlit/config.toml` | Streamlit server port (**8502**)   |

The SQLite database file `credentialing.db` is created automatically in the project folder on first save.

## Security notes

- Keep `.env` out of version control (it is listed in `.gitignore` if you use git).
- Use app passwords or scoped credentials, not your primary account password when 2FA is enabled.
