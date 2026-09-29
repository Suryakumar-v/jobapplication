"""Filesystem helpers."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from app.utils.text_utils import slugify


def ensure_directories(paths: Iterable[Path]) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def is_within(base: Path, candidate: Path) -> bool:
    """True when candidate resolves inside base (guards against path traversal)."""
    try:
        candidate.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def safe_filename(name: str, extension: str = "") -> str:
    """Slugified file name; never contains path separators or user-supplied dots."""
    ext = extension.lstrip(".")
    base = slugify(name, max_length=80)
    return f"{base}.{ext}" if ext else base


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
