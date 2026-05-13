"""
Extract text from PDFs and images using **Google Cloud Document AI** (processor OCR).

Credentials (pick one): **``GOOGLE_SERVICE_ACCOUNT_JSON``** with the full service-account JSON
(Streamlit Cloud / secrets), or **``GOOGLE_APPLICATION_CREDENTIALS``** as a path to the JSON file
(local ``.env``). Never commit key material. Optional env vars override defaults below.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Union

from dotenv import load_dotenv

from service_account_json import normalize_service_account_json_string

load_dotenv()

MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024

# Keys that are not part of the standard service-account schema (stripped before auth).
_EXTRA_SA_KEYS = frozenset({"processor_id"})


def _infer_mime_from_filename(fn: str) -> Optional[str]:
    """Guess Document AI mime from file extension when email MIME is wrong."""
    fn = fn.lower()
    if fn.endswith(".pdf"):
        return "application/pdf"
    if fn.endswith(".png"):
        return "image/png"
    if fn.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if fn.endswith((".tif", ".tiff")):
        return "image/tiff"
    if fn.endswith(".gif"):
        return "image/gif"
    if fn.endswith(".webp"):
        return "image/webp"
    if fn.endswith(".bmp"):
        return "image/bmp"
    return None


def _normalize_mime_for_document_ai(mime: str, filename: str) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    fn = (filename or "").lower()
    if m in ("image/jpg",):
        return "image/jpeg"
    if m:
        return m
    if fn.endswith(".pdf"):
        return "application/pdf"
    if fn.endswith(".png"):
        return "image/png"
    if fn.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if fn.endswith(".tiff") or fn.endswith(".tif"):
        return "image/tiff"
    if fn.endswith(".gif"):
        return "image/gif"
    if fn.endswith(".webp"):
        return "image/webp"
    return "application/octet-stream"


def _load_document_ai_settings() -> Union[tuple[dict, str, str, str], str]:
    """
    Returns (service_account_json_dict, project_id, location, processor_id) or an error string.

    ``load_dotenv(override=False)`` so values injected from Streamlit secrets / bootstrap are not
    wiped by a missing or partial local ``.env``.
    """
    load_dotenv(override=False)

    raw: dict | None = None
    json_env = normalize_service_account_json_string(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "")
    if json_env:
        try:
            loaded = json.loads(json_env)
        except json.JSONDecodeError as exc:
            return f"Invalid GOOGLE_SERVICE_ACCOUNT_JSON: {exc}"
        if not isinstance(loaded, dict):
            return "GOOGLE_SERVICE_ACCOUNT_JSON must be a JSON object."
        raw = loaded
    else:
        cred_path = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip().strip('"')
        if not cred_path:
            return (
                "Missing Google credentials. Add **GOOGLE_SERVICE_ACCOUNT_JSON** (paste the full "
                "service account JSON) to Streamlit Cloud secrets, or set "
                "**GOOGLE_APPLICATION_CREDENTIALS** to a JSON file path in your local `.env`."
            )
        p = Path(cred_path)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent / cred_path
        p = p.resolve()
        if not p.is_file():
            return f"Credentials file not found: {p}"
        try:
            with open(p, encoding="utf-8") as f:
                raw = json.load(f)
        except json.JSONDecodeError as exc:
            return f"Invalid JSON in credentials file: {exc}"

    project_id = (os.getenv("GCP_PROJECT_ID") or raw.get("project_id") or "").strip()
    if not project_id:
        return "Missing GCP project id (set GCP_PROJECT_ID or use a service account JSON with project_id)."

    location = (os.getenv("DOCUMENT_AI_LOCATION") or "us").strip().lower()
    processor_id = (
        (os.getenv("DOCUMENT_AI_PROCESSOR_ID") or "").strip()
        or str(raw.get("processor_id") or "").strip()
    )
    if not processor_id:
        return (
            "Missing Document AI processor id. Set DOCUMENT_AI_PROCESSOR_ID in `.env` / secrets "
            "or add a processor_id field to your JSON (non-standard, but supported here)."
        )

    return (raw, project_id, location, processor_id)


def _process_with_document_ai(data: bytes, mime: str) -> tuple[str, str, Optional[str]]:
    """Call Document AI synchronous ``process_document``."""
    from google.cloud import documentai_v1 as documentai
    from google.oauth2 import service_account

    cfg = _load_document_ai_settings()
    if isinstance(cfg, str):
        return "", "failed", cfg

    info, project_id, location, processor_id = cfg

    sa_info = {k: v for k, v in info.items() if k not in _EXTRA_SA_KEYS}
    credentials = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    client = documentai.DocumentProcessorServiceClient(credentials=credentials)
    name = client.processor_path(project_id, location, processor_id)

    raw_document = documentai.RawDocument(content=data, mime_type=mime)
    request = documentai.ProcessRequest(name=name, raw_document=raw_document)

    try:
        result = client.process_document(request=request)
    except Exception as exc:
        return "", "failed", f"Document AI error: {exc}"

    text = (result.document.text or "").strip()
    if text:
        return text, "document_ai", None
    return "", "failed", "Document AI returned an empty document (no extractable text)."


def extract_text_from_attachment(
    filename: str,
    mime: str,
    data: bytes,
) -> tuple[str, str, Optional[str]]:
    """
    Return ``(text, method, error)`` where ``error`` is ``None`` on success.

    Successful ``method`` is always ``document_ai`` for this integration.
    """
    if not data:
        return "", "failed", "Empty attachment."

    if len(data) > MAX_ATTACHMENT_BYTES:
        return "", "skipped", f"Attachment too large (max {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB)."

    mime_l = _normalize_mime_for_document_ai(mime, filename)
    fn = (filename or "").lower()

    supported_mimes = (
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/tiff",
        "image/gif",
        "image/webp",
        "image/bmp",
    )
    if mime_l not in supported_mimes:
        inferred = _infer_mime_from_filename(fn)
        if inferred:
            mime_l = inferred

    if mime_l in supported_mimes:
        return _process_with_document_ai(data, mime_l)

    return "", "unsupported", f"Unsupported type for Document AI: {mime_l} ({filename})"
