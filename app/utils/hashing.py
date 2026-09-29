"""Hashing helpers used for fingerprints and integrity checks."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def stable_fingerprint(*parts: str, length: int = 32) -> str:
    """Deterministic fingerprint of ordered parts; the separator prevents boundary collisions."""
    joined = "\x1f".join(parts)
    return sha256_text(joined)[:length]
