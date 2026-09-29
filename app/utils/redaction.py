"""Redaction of secrets and personal data before logging or returning errors."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REDACTED = "[REDACTED]"

SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "cookie",
    "session",
    "storage_state",
    "credential",
    "answer",
    "ssn",
    "date_of_birth",
    "dob",
    "gender",
    "race",
    "ethnicity",
    "disability",
    "veteran",
)

_KEY_VALUE = re.compile(
    r"""(?ix)
    (?P<key>["']?(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|set-cookie|
        storage_state|access_token|refresh_token|approval_token|x-api-key)["']?)
    (?P<sep>\s*[:=]\s*)
    (?P<value>"[^"]*"|'[^']*'|[^\s,;&}]+)
    """
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PHONE = re.compile(r"(?<![\w-])\+?\d[\d\s().-]{8,}\d(?![\w-])")
_LONG_SECRET = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")


def redact_text(text: str) -> str:
    """Mask credentials, bearer tokens, emails, phone numbers and long opaque strings."""
    text = _KEY_VALUE.sub(lambda m: f"{m['key']}{m['sep']}{REDACTED}", text)
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    text = _EMAIL.sub("[REDACTED_EMAIL]", text)
    text = _PHONE.sub("[REDACTED_PHONE]", text)
    return _LONG_SECRET.sub(REDACTED, text)


def redact_url(url: str) -> str:
    """Drop credentials, query string and fragment; keep scheme, host and path."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def redact_value(value: Any, key: str | None = None) -> Any:
    """Recursively redact mappings and sequences; mask values under sensitive keys."""
    if key is not None and is_sensitive_key(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(k): redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [redact_value(item) for item in value]
    return value
