"""
Required credentialing attachments (per provider) and filename-based detection.

Matching is **by attachment filename** (case-insensitive). Encourage providers to use clear
names such as ``resume.pdf``, ``medical_license.pdf``, ``dea_certificate.pdf``.
"""

from __future__ import annotations

from typing import NamedTuple

# Display order matches product requirements.
REQUIRED_CREDENTIAL_DOCUMENTS: tuple[str, ...] = (
    "CV/Resume",
    "Medical License",
    "DEA Certificate",
    "Board Certifications",
    "Insurance Documents",
)

# First matching row wins for each filename (more specific rows first).
_FILENAME_DOC_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DEA Certificate", ("dea.", "dea_", " dea", "deacert", "dea cert", "dea registration", "drug enforcement")),
    (
        "Medical License",
        (
            "medical license",
            "physician license",
            "state license",
            "license",
            "licence",
            " lic",
            " lic.",
            "state lic",
            "phys lic",
        ),
    ),
    ("Board Certifications", ("board cert", "board certification", "abms", "board eligible")),
    ("Insurance Documents", ("insurance", "malpractice", "liability", "coi", "policy")),
    (
        "CV/Resume",
        (
            "resume",
            "curriculum vitae",
            "curriculum",
            "c.v",
            " cv",
            " cv.",
            "vita",
            "cv",
        ),
    ),
)


def _normalize_filename_for_match(filename: str) -> str:
    base = (filename or "").rsplit("/", 1)[-1].lower()
    return base.replace("_", " ").replace("-", " ")


def classify_attachment_filename(filename: str) -> str | None:
    """Return one of ``REQUIRED_CREDENTIAL_DOCUMENTS`` labels, or ``None`` if no rule matched."""
    n = _normalize_filename_for_match(filename)
    if not n.strip():
        return None
    for label, needles in _FILENAME_DOC_RULES:
        if any(needle in n for needle in needles):
            return label
    return None


class AttachmentMatchReport(NamedTuple):
    """Per-attachment filename classification for UI and reminder emails."""

    matched_by_category: dict[str, tuple[str, ...]]
    missing_categories: tuple[str, ...]
    unmatched_filenames: tuple[str, ...]


def build_attachment_match_report(filenames: list[str]) -> AttachmentMatchReport:
    """Map each non-empty filename to a required category or to *unmatched*."""
    buckets: dict[str, list[str]] = {label: [] for label in REQUIRED_CREDENTIAL_DOCUMENTS}
    unmatched: list[str] = []
    for raw in filenames:
        fn = (raw or "").strip()
        if not fn:
            continue
        label = classify_attachment_filename(fn)
        if label:
            buckets[label].append(fn)
        else:
            unmatched.append(fn)
    matched = {k: tuple(v) for k, v in buckets.items() if v}
    missing = tuple(label for label in REQUIRED_CREDENTIAL_DOCUMENTS if not buckets[label])
    return AttachmentMatchReport(matched, missing, tuple(unmatched))


def detected_document_categories(filenames: list[str]) -> set[str]:
    """Set of required labels identified from the given attachment names."""
    found: set[str] = set()
    for fn in filenames:
        label = classify_attachment_filename(fn)
        if label:
            found.add(label)
    return found


def missing_required_documents(filenames: list[str]) -> list[str]:
    """Labels from ``REQUIRED_CREDENTIAL_DOCUMENTS`` not matched by any attachment name."""
    found = detected_document_categories(filenames)
    return [label for label in REQUIRED_CREDENTIAL_DOCUMENTS if label not in found]
