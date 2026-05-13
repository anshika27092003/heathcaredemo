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

# Lines that look like a headline / specialty, not a person's name (CV OCR heuristics).
_CV_NON_NAME_SUBSTRINGS = (
    "chronic disease",
    "disease management",
    "patient care",
    "years of",
    "year of",
    "board-certified",
    "board certified",
    "internal medicine",
    "expertise",
    "professional summary",
    "summary of qualifications",
    "work experience",
    "employment history",
    "clinical experience",
    "core competencies",
    "physician with",
    "medicine physician",
    "secretary of",
    "department of",
)


def _cv_line_likely_not_person_name(ln: str) -> bool:
    s = (ln or "").lower()
    if any(fragment in s for fragment in _CV_NON_NAME_SUBSTRINGS):
        return True
    if re.search(
        r"\b(management|surgery|services|program|department|clinic|center|centre)\b",
        s,
    ) and len(ln.split()) >= 2:
        return True
    return False


# One token in a Western-style person name (handles O'Brien, Mary-Jane, Q. middle initial).
_CV_NAME_TOKEN = r"(?:[A-Z][a-z']*(?:-[A-Z][a-z']*)?|[A-Z]\.)"
# Full name without honorific: at least two tokens (e.g. Alex Mercer, Jane Q. Public).
_CV_NAME_CORE = _CV_NAME_TOKEN + r"(?:\s+" + _CV_NAME_TOKEN + r"){1,4}"


def _shorten_cv_header_line(ln: str) -> str:
    """If OCR merged the name with credentials/tagline, keep the left-hand name segment."""
    if not ln:
        return ln
    for sep in (
        r"\s+MD\b",
        r"\s+M\.D\.?",
        r"\s+D\.O\.?",
        r"\s+PhD\b",
        r"\s+RN\b",
        r"\s+Board[-\s]",
        r"\s+Board\s+Certified\b",
        r"\s+Internal\s+Medicine\b",
        r"\s+(?:E-?mail|Email|Phone|Tel|Mobile|LinkedIn)\b",
        r"\s+\(\d{3}\)",  # phone starting on same line
    ):
        parts = re.split(sep, ln, maxsplit=1, flags=re.I)
        if len(parts) > 1 and parts[0].strip():
            return parts[0].strip()
    return ln


def _extract_cv_name_from_header(text: str) -> str:
    """
    Prefer honorific + name at the top of the résumé, before credentials
    (avoids picking specialty lines like "Chronic Disease Management").
    """
    head = "\n".join((text or "").splitlines()[:10])[:800]
    lookahead = (
        r"(?=\s*(?:MD|M\.D\.|D\.O\.|PhD|DO|RN|PA-C|NP|Board\b|E-?mail|Email|Phone|Tel|"
        r"Mobile|LinkedIn|\(|\d{3}[-.\s]?\d{3}|\n|\Z))"
    )
    m = re.search(
        rf"(?m)^\s*((?:(?:Dr|Mr|Ms|Mrs)\.?\s+)?{_CV_NAME_CORE})\s*{lookahead}",
        head,
    )
    if m:
        cand = m.group(1).strip()
        if not _cv_line_likely_not_person_name(cand):
            return cand[:120]
    return ""


_LICENSE_NON_NAME_SUBSTRINGS = (
    "commonwealth of",
    "state of",
    "secretary of",
    "department of",
    "bureau of",
    "board of",
    "professional and",
    "occupational affairs",
    "pennsylvania",
    "united states",
    "license number",
    "expiration date",
    "initial license",
    "date of birth",
    "motor vehicle",
    "driver license",
    "driver's license",
    "operators license",
    "po box",
    "this license",
    "verify at",
    "not a license",
    "display only",
)


def _license_probable_last_comma_first(raw: str) -> bool:
    s = raw.replace("  ", " ").strip()
    return bool(re.match(r"^[A-Z]{1,20},\s*[A-Z][A-Z\s'.-]{1,35}$", s))


def _license_line_likely_not_person_name(ln: str) -> bool:
    s = (ln or "").strip().lower()
    if len(s) < 2:
        return True
    if any(x in s for x in _LICENSE_NON_NAME_SUBSTRINGS):
        return True
    raw = (ln or "").strip()
    if raw.isupper() and re.search(r"\bOF\b|\bAND\b|\bTHE\b", raw):
        if not _license_probable_last_comma_first(raw):
            return True
    return False


def _extract_license_person_name(t: str) -> str:
    """Best-effort licensee / holder name; rejects jurisdiction and agency headers."""
    text = t or ""

    m = re.search(
        r"(?m)^\s*([A-Z][A-Z\s'.-]{1,22},\s*[A-Z][A-Z\s'.-]{1,30})\s*$",
        text,
    )
    if m:
        cand = " ".join(m.group(1).split())
        if _license_probable_last_comma_first(cand) and not _license_line_likely_not_person_name(
            cand.replace(",", " ")
        ):
            return cand.title() if cand.isupper() else cand

    label_patterns = (
        r"(?is)(?:licensee|practitioner|holder|registrant)\s*name\s*[:\n.\s-]+\s*((?:Dr\.?\s+)?[A-Z][A-Za-z'.-]*(?:[ \t]+[A-Z][A-Za-z'.-]*){1,4})\b",
        r"(?is)(?:name|full\s*name|driver\s*name)\s*[:\n.\s-]+\s*((?:Dr\.?\s+)?[A-Z][A-Za-z'.-]*(?:[ \t]+[A-Z][A-Za-z'.-]*){1,4})\b",
    )
    for pat in label_patterns:
        m = re.search(pat, text)
        if m:
            cand = " ".join(m.group(1).split())
            if 3 <= len(cand) <= 85 and not _license_line_likely_not_person_name(cand):
                return cand

    m = re.search(
        r"(?is)this\s+certifies\s+that\s+(?:Dr\.?\s+)?([A-Z][A-Za-z'.-]+(?:[ \t]+[A-Z][A-Za-z'.-]*){1,4})\b",
        text,
    )
    if m:
        cand = " ".join(m.group(1).split())
        if not _license_line_likely_not_person_name(cand):
            return cand[:120]

    name_m = re.search(
        r"(?is)(?:name|driver\s*name|full\s*name|fn\b)[\s:.-]+((?:Dr\.?\s+)?[A-Z][A-Za-z'.-]+(?:[ \t]+[A-Z][A-Za-z'.-]+){1,4})\b",
        text,
    )
    if name_m:
        cand = name_m.group(1).strip()
        if not _license_line_likely_not_person_name(cand):
            return cand

    caps_same = re.search(
        r"(?is)(?:physician\s+and\s+surgeon|medical\s+physician)\s*[:\n]?\s*((?:Dr\.?\s+)?[A-Z][A-Za-z'.-]+(?:[ \t]+[A-Z][A-Za-z'.-]*){0,4})\b",
        text,
    )
    if caps_same:
        cand = " ".join(caps_same.group(1).split())
        if 4 <= len(cand) <= 70 and not _license_line_likely_not_person_name(cand):
            if not any(
                x in cand.upper()
                for x in ("DEPARTMENT", "LICENSE", "PENNSYLVANIA", "BUREAU", "PROFESSIONAL", "FESSIONA")
            ):
                return cand.title() if cand.isupper() else cand

    caps_next = re.search(
        r"(?is)(?:physician\s+and\s+surgeon|medical\s+physician)[^\n]{0,160}\n\s*([A-Z][A-Za-z'.-]+(?:[ \t]+[A-Z][A-Za-z'.-]*){0,4})\b",
        text,
    )
    if caps_next:
        cand = " ".join(caps_next.group(1).split())
        if 4 <= len(cand) <= 70 and not _license_line_likely_not_person_name(cand):
            if not any(
                x in cand.upper()
                for x in ("DEPARTMENT", "LICENSE", "PENNSYLVANIA", "BUREAU", "PROFESSIONAL", "FESSIONA")
            ):
                return cand.title() if cand.isupper() else cand

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines[:24]:
        if _license_line_likely_not_person_name(ln):
            continue
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
                    "SECRETARY",
                    "OCCUPATIONAL",
                    "STATE OF",
                )
            ):
                return " ".join(ln.split())

    for ln in lines[:16]:
        if _license_line_likely_not_person_name(ln):
            continue
        if 3 <= len(ln) <= 45 and re.match(
            r"^[A-Za-z'.-]+(?:\s+[A-Za-z'.-]+){1,3}$",
            ln,
        ):
            if not any(
                w.lower() in ln.lower()
                for w in ("license", "driver", "state", "class", "expires", "expiry")
            ):
                return ln

    return ""


def detect_category(filename: str, ocr_text: str) -> str:
    fn = (filename or "").lower()
    text = (ocr_text or "")[:50000].lower()

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

    # Résumé / CV before generic "license in body" rules — avoids "exp" matching "Experience".
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
            "clinical experience",
        )
    )
    if cv_fn or cv_txt:
        return CATEGORY_CV

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
    _expiry_or_dob = bool(
        re.search(r"(?i)\b(?:expires?|expir(?:es|y)|exp\.?\s*date)\b", text)
        or re.search(r"(?i)\b(?:dob|date\s*of\s*birth)\b", text)
        or re.search(r"(?i)\bdln\b", text)
    )
    if license_fn or (license_txt and _expiry_or_dob):
        return CATEGORY_LICENSE

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

    out["name"] = _extract_license_person_name(t)

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
        out["name"] = _extract_cv_name_from_header(t)

    if not out["name"]:
        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
        for ln in lines[:10]:
            if "@" in ln or re.search(r"\d{3}[-.\s]?\d{3}", ln):
                continue
            cand = _shorten_cv_header_line(ln)
            if _cv_line_likely_not_person_name(cand):
                continue
            if re.match(
                rf"^(?:(?:Dr|Mr|Ms|Mrs)\.?\s+)?{_CV_NAME_CORE}$",
                cand,
            ) and 4 <= len(cand) <= 85:
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
                if not any(k in cand.lower() for k in skip_kw):
                    out["name"] = cand
                    break
        if not out["name"]:
            for ln in lines[:14]:
                if _cv_line_likely_not_person_name(ln):
                    continue
                if 4 <= len(ln) <= 60 and re.match(
                    rf"^(?:(?:Dr|Mr|Ms|Mrs)\.?\s+)?{_CV_NAME_CORE}$",
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
    try:
        return json.dumps(fields or {}, ensure_ascii=False, default=str)
    except TypeError:
        return "{}"


def _sf_blank(sf: dict[str, Any], key: str) -> bool:
    v = sf.get(key)
    return v is None or str(v).strip() == ""


def missing_credentialing_labels(category: str, sf: dict[str, Any]) -> list[str]:
    """
    Human-readable labels for important fields that are empty or unclear (admin / provider follow-up).
    """
    sf = sf or {}
    c = (category or "other").strip().lower()
    if c not in ("license", "cv", "other"):
        c = "other"
    missing: list[str] = []

    def add(label: str, condition: bool) -> None:
        if condition:
            missing.append(label)

    if c == "license":
        add("Full name", _sf_blank(sf, "name"))
        add("License number", _sf_blank(sf, "license_number"))
        exp_ok = not _sf_blank(sf, "expiry_date") or not _sf_blank(sf, "expiration_date")
        add("Expiration date", not exp_ok)
        iss_ok = not _sf_blank(sf, "issue_date") or not _sf_blank(sf, "initial_license_date")
        add("Issue / initial license date", not iss_ok)
        sig = str(sf.get("signature_present") or "").strip().lower()
        add("Signature status (not confirmed as yes/no)", sig not in ("yes", "no"))
    elif c == "cv":
        add("Name", _sf_blank(sf, "name"))
        add("Email", _sf_blank(sf, "email"))
        add("Phone", _sf_blank(sf, "phone"))
        add("Location / address", _sf_blank(sf, "location"))
    else:
        add("Name", _sf_blank(sf, "name"))
        add("Email", _sf_blank(sf, "email"))
        add("Phone", _sf_blank(sf, "phone"))
        add("License number", _sf_blank(sf, "license_number"))
        exp_ok = not _sf_blank(sf, "expiry_date") or not _sf_blank(sf, "expiration_date")
        add("Expiration date", not exp_ok)
    return missing


