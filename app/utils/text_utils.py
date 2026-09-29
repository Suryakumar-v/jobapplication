"""Text normalisation helpers."""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_whitespace(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def normalize_text(text: str) -> str:
    """Lower-case, strip accents and punctuation, collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    return normalize_whitespace(_NON_ALNUM.sub(" ", ascii_text))


def slugify(text: str, max_length: int = 60) -> str:
    slug = normalize_text(text).replace(" ", "-")
    return slug[:max_length].strip("-") or "untitled"
