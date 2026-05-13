"""
Shared helpers for reading mail-related environment variables.

Kept separate from ``email_service`` so ``mail_reader`` does not import ``email_service``,
avoiding circular import issues when other modules import ``email_service`` at startup.
"""

from __future__ import annotations

from typing import Optional


def strip_env(value: Optional[str]) -> Optional[str]:
    """Trim accidental spaces/newlines often pasted into .env values."""
    if value is None:
        return None
    s = value.strip()
    return s if s else None


def normalize_app_password(password: Optional[str]) -> Optional[str]:
    """
    Gmail / Google Workspace App Passwords are often pasted as four groups of four letters.
    SMTP expects one continuous 16-character password with no spaces.
    """
    if not password:
        return password
    collapsed = "".join(password.split())
    if len(collapsed) == 16 and collapsed.isalnum():
        return collapsed
    return password
