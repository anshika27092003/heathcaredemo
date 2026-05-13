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
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
)


def detect_category(filename: str, ocr_text: str) -> str:
    fn = (filename or "").lower()
    text = (ocr_text or "")[:12000].lower()

    # Professional / medical board certificates (often misread as CV if they say "physician").
    prof_license = any(
        phrase in text
        for phrase in (
            "initial license date",
            "license status",
            "expiration date",
            "bureau of professional",
            "professional and occupational affairs",
            "department of state",
            "board of medicine",
            "medical physician and surgeon",
            "physician and surgeon",
            "professional license",
            "occupational affairs",
        )
    )
    if prof_license and (
        "license" in text
        or "expiration" in text
        or "initial license" in text
        or "license number" in text
    ):
        return CATEGORY_LICENSE

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
            "professional summary",
            "summary of qualifications",
            "objective",
            "education",
            "skills",
            "core competencies",
            "residency and fellowship",
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


def _signature_present(text: str) -> str:
    """``yes`` / ``no`` / ``unknown`` — OCR-only guess; not a legal opinion."""
    t = text or ""
    if re.search(r"(?i)\bsignature\b", t):
        return "yes"
    if re.search(r"(?i)electronic\s+signature|digitally\s+signed", t):
        return "yes"
    if re.search(r"(?i)\bsigned\s+by\b", t):
        return "yes"
    if re.search(r"(?i)\b/s/|/s/", t):
        return "yes"
    if re.search(r"_{5,}", t):
        return "yes"
    if re.search(r"(?i)\bno\s+signature\b|\bunsigned\b", t):
        return "no"
    return "unknown"


def extract_license_fields(text: str) -> dict[str, str]:
    """Driver licenses, state IDs, and professional board certificates (PA-style, etc.)."""
    t = text or ""
    out: dict[str, str] = {
        "name": "",
        "license_number": "",
        "expiry_date": "",
        "expiration_date": "",
        "issue_date": "",
        "initial_license_date": "",
        "signature_present": _signature_present(t),
    }

    # Board / professional certificate wording (labels and values may be several lines apart).
    exp_m = re.search(
        r"(?is)expiration\s+date([\s\S]{0,220}?)(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        t,
    )
    if exp_m:
        d = _norm_date_string(exp_m.group(2))
        out["expiry_date"] = d
        out["expiration_date"] = d

    if not out["expiry_date"]:
        exp = re.search(
            r"(?i)(?:exp(?:ir(?:es|y)?)?|expires?|exp\s*date)[\s:.-]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            t,
        )
        if exp:
            d = _norm_date_string(exp.group(1))
            out["expiry_date"] = d
            out["expiration_date"] = d

    init_m = re.search(
        r"(?is)initial\s+license\s+date([\s\S]{0,220}?)(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        t,
    )
    if init_m:
        d = _norm_date_string(init_m.group(2))
        out["issue_date"] = d
        out["initial_license_date"] = d

    if not out["issue_date"]:
        iss = re.search(
            r"(?i)(?:iss(?:ued?)?|issue\s*date|date\s*issued)[\s:.-]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            t,
        )
        if iss:
            d = _norm_date_string(iss.group(1))
            out["issue_date"] = d
            out["initial_license_date"] = d

    if not out["expiry_date"]:
        out["expiry_date"] = _first_date_after_label(
            t, ("expiration date", "exp", "expires", "expiration", "expiry")
        )
        out["expiration_date"] = out["expiry_date"]
    if not out["issue_date"]:
        out["issue_date"] = _first_date_after_label(
            t, ("initial license date", "iss", "issued", "issue")
        )
        out["initial_license_date"] = out["issue_date"]

    num = re.search(
        r"(?is)license\s+number([\s\S]{0,220}?)\b([A-Za-z]{1,4}\d{5,}[A-Za-z0-9]*)\b",
        t,
    )
    if num:
        out["license_number"] = num.group(2).strip()

    if not out["license_number"]:
        low = t.lower()
        idx = low.find("license number")
        if idx != -1:
            chunk = t[idx : idx + 500]
            m = re.search(r"\b([A-Z]{2}\d{5,}[A-Z0-9]?)\b", chunk)
            if m and len(m.group(1)) <= 16:
                out["license_number"] = m.group(1)

    if not out["license_number"]:
        num = re.search(
            r"(?i)(?:dln|dl\s*#|license\s*#|license\s*no\.?|id\s*#|no\.?\s*)[:#\s]*([A-Za-z0-9]{5,20})\b",
            t,
        )
        if num:
            out["license_number"] = num.group(1).strip()

    if not out["license_number"]:
        m = re.search(r"\b([A-Z]{1,3}\d{5,}[A-Z0-9]?|[A-Z]\d{7,12}|\d{8,12}[A-Z]?)\b", t)
        if m and len(m.group(1)) <= 18:
            out["license_number"] = m.group(1)

    name_m = re.search(
        r"(?i)(?:name|driver\s*name|full\s*name|fn\b)[\s:.-]+([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){1,4})",
        t,
    )
    if name_m:
        out["name"] = name_m.group(1).strip()
    else:
        caps = re.search(
            r"(?is)(?:physician\s+and\s+surgeon|medical\s+physician)[^\n]*\n\s*([A-Z][A-Z\s'.-]{6,50})\b",
            t,
        )
        if caps:
            cand = " ".join(caps.group(1).split())
            if not any(
                x in cand.upper()
                for x in ("DEPARTMENT", "LICENSE", "PENNSYLVANIA", "BUREAU", "PROFESSIONAL", "FESSIONA")
            ):
                out["name"] = cand.title() if cand.isupper() else cand
        if not out["name"]:
            lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
            for ln in lines[:20]:
                if 8 <= len(ln) <= 55 and re.match(r"^[A-Z][A-Z\s'.-]{6,50}$", ln):
                    if not any(
                        x in ln.upper()
                        for x in (
                            "DEPARTMENT",
                            "LICENSE",
                            "PENNSYLVANIA",
                            "BUREAU",
                            "DISPLAY",
                            "NOTIFY",
                            "COMMONWEALTH",
                            "PROFESSIONAL",
                            "FESSIONA",
                            "ALTERATION",
                        )
                    ):
                        out["name"] = " ".join(ln.split())
                        break
        if not out["name"]:
            for ln in lines[:12]:
                if 3 <= len(ln) <= 45 and re.match(
                    r"^[A-Za-z'.-]+(?:\s+[A-Za-z'.-]+){1,3}$", ln
                ):
                    if not any(
                        w.lower() in ln.lower()
                        for w in ("license", "driver", "state", "class")
                    ):
                        out["name"] = ln
                        break

    return out


def extract_cv_fields(text: str) -> dict[str, str]:
    """Résumé / CV: name, contact email & phone, location, plus a short summary when possible."""
    t = text or ""
    out: dict[str, str] = {
        "name": "",
        "email": "",
        "phone": "",
        "location": "",
        "description": "",
    }

    em = _EMAIL_RE.search(t)
    if em:
        out["email"] = em.group(0).strip()

    ph = _PHONE_RE.search(t)
    if ph:
        out["phone"] = re.sub(r"\s+", " ", ph.group(0).strip())

    loc_m = re.search(r"(?im)^\s*location\s*:\s*(.+)$", t)
    if loc_m:
        out["location"] = " ".join(loc_m.group(1).split())[:200]

    if not out["location"]:
        addr_m = re.search(r"(?im)^\s*address\s*:\s*(.+)$", t)
        if addr_m:
            out["location"] = " ".join(addr_m.group(1).split())[:200]

    name_m = re.search(
        r"(?i)^\s*(?:name|full\s*name)\s*[:\-]\s*(.+)$",
        t,
        re.MULTILINE,
    )
    if name_m:
        out["name"] = name_m.group(1).strip()[:120]
    else:
        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
        for ln in lines[:10]:
            if "@" in ln or re.search(r"\d{3}[-.\s]?\d{3}", ln):
                continue
            if re.match(
                r"^(Dr\.?\s+)?[A-Z][a-zA-Z'.-]+(?:\s+[A-Z][a-zA-Z'.-]+){0,4}$",
                ln,
            ) and 4 <= len(ln) <= 70:
                skip_kw = (
                    "board-certified",
                    "board certified",
                    "physician",
                    "medicine",
                    "years of",
                    "expertise",
                    "internal medicine",
                    "summary",
                    "contact",
                )
                if not any(k in ln.lower() for k in skip_kw):
                    out["name"] = ln
                    break
        if not out["name"]:
            for ln in lines[:12]:
                if 4 <= len(ln) <= 60 and re.match(
                    r"^[A-Z][a-z]+(?:\s+[A-Z][a-z'.-]+){1,3}$",
                    ln,
                ):
                    out["name"] = ln
                    break

    summary_m = re.search(
        r"(?is)professional\s+summary\s*(.+?)(?=\n\s*(?:residency|education|clinical\s+experience|work\s+experience|skills)\b|\Z)",
        t,
    )
    if summary_m:
        body = re.sub(r"\s+", " ", summary_m.group(1)).strip()
    else:
        body = re.sub(r"\s+", " ", t).strip()
    if len(body) > 500:
        out["description"] = body[:500].rsplit(" ", 1)[0] + "…"
    else:
        out["description"] = body[:900]

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
