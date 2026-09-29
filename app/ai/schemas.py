"""Shapes for AI input and output. Anything a provider returns is untrusted text."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_SUMMARY_CHARS = 600
MAX_SKILLS = 30
MAX_SKILL_CHARS = 60
MAX_FLAGS = 10
MAX_FLAG_CHARS = 200
MAX_SENIORITY_CHARS = 40

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")


def clean_text(value: str, limit: int) -> str:
    """Remove control characters, links and e-mail addresses; collapse spaces; cap length."""
    text = _EMAIL.sub("", _URL.sub("", _CONTROL.sub("", value)))
    return " ".join(text.split())[:limit]


def _clean_list(value: Any, *, max_items: int, max_chars: int) -> list[str]:
    """Keep short, unique, non-empty strings. Over-long items are dropped, not truncated."""
    if not isinstance(value, list):
        raise ValueError("expected a list of strings")
    seen: set[str] = set()
    items: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            continue
        text = clean_text(raw, max_chars + 1)
        if not text or len(text) > max_chars or text.lower() in seen:
            continue
        seen.add(text.lower())
        items.append(text)
        if len(items) == max_items:
            break
    return items


class JobRequest(BaseModel):
    """The only data a provider ever receives: the posting itself, no candidate data."""

    company: str
    title: str
    description: str


class JobInsights(BaseModel):
    """What the model claims the posting says. Unknown keys are ignored, never acted on."""

    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    seniority: str | None = None
    red_flags: list[str] = Field(default_factory=list)

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, value: Any) -> str:
        return clean_text(value, MAX_SUMMARY_CHARS) if isinstance(value, str) else ""

    @field_validator("seniority", mode="before")
    @classmethod
    def _seniority(cls, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        return clean_text(value, MAX_SENIORITY_CHARS) or None

    @field_validator("required_skills", "preferred_skills", mode="before")
    @classmethod
    def _skills(cls, value: Any) -> list[str]:
        return _clean_list(
            value if value is not None else [], max_items=MAX_SKILLS, max_chars=MAX_SKILL_CHARS
        )

    @field_validator("red_flags", mode="before")
    @classmethod
    def _flags(cls, value: Any) -> list[str]:
        return _clean_list(
            value if value is not None else [], max_items=MAX_FLAGS, max_chars=MAX_FLAG_CHARS
        )


class SkillComparison(BaseModel):
    """Deterministic check of the model's skill list against the candidate profile."""

    required_covered: list[str] = Field(default_factory=list)
    required_gaps: list[str] = Field(default_factory=list)
    preferred_covered: list[str] = Field(default_factory=list)
    preferred_gaps: list[str] = Field(default_factory=list)
