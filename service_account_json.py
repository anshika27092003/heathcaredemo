"""
Helpers for Google service-account JSON often pasted into Streamlit Secrets / .env.

Invalid ``private_key`` values frequently contain *literal* newlines inside the JSON string.
JSON only allows those as ``\\n`` escapes, which triggers "Invalid control character" from
``json.loads``. We rewrite that one field using ``json.dumps`` on the raw PEM substring.
"""

from __future__ import annotations

import json
import re


def normalize_service_account_json_string(raw: str) -> str:
    """
    Return a string that ``json.loads`` can parse, or the stripped input if no repair applies.

    Best-effort: only attempts a known fix when the parse error mentions control characters.
    """
    s = (raw or "").strip().lstrip("\ufeff")
    if not s:
        return s
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError as exc:
        msg = str(exc).lower()
        if "control character" not in msg and "invalid \\u" not in msg:
            return s
    fixed = _escape_private_key_field(s)
    if fixed != s:
        try:
            json.loads(fixed)
            return fixed
        except json.JSONDecodeError:
            pass
    return s


def _escape_private_key_field(s: str) -> str:
    """Replace the ``private_key`` JSON string value with a properly escaped version."""
    m = re.search(r'("private_key"\s*:\s*")([\s\S]*?)(")(\s*[,}])', s)
    if not m:
        return s
    body = m.group(2)
    escaped = json.dumps(body)[1:-1]
    return s[: m.start(2)] + escaped + s[m.start(3) :]
