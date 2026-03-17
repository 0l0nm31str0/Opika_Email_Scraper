"""
Email syntax validation — standard regex check.
Drops malformed addresses immediately.
"""

from __future__ import annotations

import re

# RFC 5322 -ish email pattern (practical, not pedantic)
_EMAIL_PATTERN = re.compile(
    r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
)


def is_valid_syntax(email: str) -> bool:
    """Return True if *email* passes basic syntax validation."""
    if not email or not isinstance(email, str):
        return False
    email = email.strip().lower()
    if len(email) > 254:  # RFC 5321 max length
        return False
    return bool(_EMAIL_PATTERN.match(email))


def clean_email(email: str) -> str:
    """Normalise an email address: lowercase, strip whitespace."""
    return email.strip().lower()
