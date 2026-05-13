"""
Best-effort document type detection and light structured-field extraction from OCR text.

Results are **heuristic** (filename + keyword + regex). Real IDs vary by state/country;
treat extracted fields as draft data for admin review, not verified facts.
"""

from __future__ import annotations

import json
import re
from typing import Any

CATEGORY_LICENSE = "license"
CATEGORY_CV = "cv"
CATEGORY_OTHER = "other"

_DATE_RE = re.compile(
    r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b",
)
_PHONE_RE = re.compile(
    r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b|\+?\d[\d\s\-().]{8,}\d",
)


def detect_category(filename: str, ocr_text: str) -> str:
    fn = (filename or "").lower()
    text = (ocr_text or "")[:12000].lower()

    license_fn = any(
        k in fn
        for k in (
            "license",
            "licence",
            "driver",
            "dl_",
            "driving",
            "state_id",
            "id_front",
            "id_back",
            "identification",
        )
    )
    license_txt = any(
        phrase in text
        for phrase in (
            "driver license",
            "driver's license",
            "operators license",
            "motor vehicle",
            "class d",
            "class c",
            "dl no",
            "dl#",
            "dln",
            "license no",
            "license number",
            "expires",
            "exp date",
            "iss date",
            "date of birth",
            "restriction",
            "endorsement",
        )
    )
    if license_fn or (license_txt and ("exp" in text or "dob" in text or "dln" in text)):
        return CATEGORY_LICENSE

    cv_fn = any(k in fn for k in ("resume", "cv", "curriculum", "bio_", "biography"))
    cv_txt = any(
        phrase in text
        for phrase in (
            "curriculum vitae",
            "work experience",
            "employment history",
            "professional experience",
            "summary of qualifications",
            "objective",
            "education",
            "skills",
        )
    )
    if cv_fn or cv_txt:
        return CATEGORY_CV

    return CATEGORY_OTHER


def _norm_date(m: re.Match[str]) -> str:
    a, b, y = m.group(1), m.group(2), m.group(3)
    try:
        yi = int(y)
    except ValueError:
        return f"{a}/{b}/{y}"
    if len(y) == 2:
        y = str(2000 + yi if yi < 50 else 1900 + yi)
    return f"{a}/{b}/{y}"


def _norm_date_string(ds: str) -> str:
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", ds.strip())
    return _norm_date(m) if m else ds.strip()


def _first_date_after_label(text: str, labels: tuple[str, ...]) -> str:
    t = text
    low = t.lower()
    for lab in labels:
        i = low.find(lab.lower())
        if i == -1:
            continue
        window = t[i : i + 120]
        m = _DATE_RE.search(window)
        if m:
            return _norm_date(m)
    m = _DATE_RE.search(t)
    return _norm_date(m) if m else ""


def extract_license_fields(text: str) -> dict[str, str]:
    """Pull common driver-license style labels when present in OCR."""
    t = text or ""
    out = {
        "name": "",
        "license_number": "",
        "expiry_date": "",
        "issue_date": "",
    }

    exp = re.search(
        r"(?i)(?:exp(?:ir(?:es|y)?)?|expires?|exp\s*date)[\s:.-]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        t,
    )
    if exp:
        out["expiry_date"] = _norm_date_string(exp.group(1))

    iss = re.search(
        r"(?i)(?:iss(?:ued?)?|issue\s*date|date\s*issued)[\s:.-]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        t,
    )
    if iss:
        out["issue_date"] = _norm_date_string(iss.group(1))

    if not out["expiry_date"]:
        out["expiry_date"] = _first_date_after_label(
            t, ("exp", "expires", "expiration", "expiry")
        )
    if not out["issue_date"]:
        out["issue_date"] = _first_date_after_label(t, ("iss", "issued", "issue"))

    num = re.search(
        r"(?i)(?:dln|dl\s*#|license\s*#|license\s*no\.?|id\s*#|no\.?\s*)[:#\s]*([A-Za-z0-9]{5,20})\b",
        t,
    )
    if num:
        out["license_number"] = num.group(1).strip()

    if not out["license_number"]:
        m = re.search(r"\b([A-Z]\d{7,12}|\d{8,12}[A-Z]?)\b", t)
        if m and len(m.group(1)) <= 15:
            out["license_number"] = m.group(1)

    name_m = re.search(
        r"(?i)(?:name|driver\s*name|full\s*name|fn\b)[\s:.-]+([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){1,4})",
        t,
    )
    if name_m:
        out["name"] = name_m.group(1).strip()
    else:
        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
        for ln in lines[:12]:
            if 3 <= len(ln) <= 45 and re.match(r"^[A-Za-z'.-]+(?:\s+[A-Za-z'.-]+){1,3}$", ln):
                if not any(w.lower() in ln.lower() for w in ("license", "driver", "state", "class")):
                    out["name"] = ln
                    break

    return out


def extract_cv_fields(text: str) -> dict[str, str]:
    """Resume-style: first plausible name line, first phone, short summary blurb."""
    t = text or ""
    out: dict[str, str] = {"name": "", "phone": "", "description": ""}

    ph = _PHONE_RE.search(t)
    if ph:
        out["phone"] = re.sub(r"\s+", " ", ph.group(0).strip())

    name_m = re.search(
        r"(?i)^\s*(?:name|full\s*name)\s*[:\-]\s*(.+)$",
        t,
        re.MULTILINE,
    )
    if name_m:
        out["name"] = name_m.group(1).strip()[:120]
    else:
        for ln in t.splitlines():
            ln = ln.strip()
            if 4 <= len(ln) <= 60 and re.match(
                r"^[A-Z][a-z]+(?:\s+[A-Z][a-z'.-]+){1,3}$",
                ln,
            ):
                out["name"] = ln
                break

    body = re.sub(r"\s+", " ", t).strip()
    if len(body) > 400:
        out["description"] = body[:400].rsplit(" ", 1)[0] + "…"
    else:
        out["description"] = body[:800]

    return out


def categorize_and_structure(
    filename: str,
    ocr_text: str,
    extraction_method: str,
) -> tuple[str, dict[str, Any]]:
    """
    Return ``(category, structured_dict)``.

    Skips structure when OCR did not yield usable text or extraction failed.
    """
    if extraction_method in ("failed", "unsupported", "skipped") or not (ocr_text or "").strip():
        return CATEGORY_OTHER, {}

    cat = detect_category(filename, ocr_text)
    if cat == CATEGORY_LICENSE:
        return cat, extract_license_fields(ocr_text)
    if cat == CATEGORY_CV:
        return cat, extract_cv_fields(ocr_text)
    return CATEGORY_OTHER, {}


def structured_fields_to_json(fields: dict[str, Any]) -> str:
    return json.dumps(fields, ensure_ascii=False)
