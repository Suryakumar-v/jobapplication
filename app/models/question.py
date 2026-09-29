"""Application question categories and answer configuration."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class QuestionCategory(StrEnum):
    SAFE_PROFILE = "SAFE_PROFILE"
    SAFE_JOB_SPECIFIC = "SAFE_JOB_SPECIFIC"
    SENSITIVE = "SENSITIVE"
    LEGAL = "LEGAL"
    DEMOGRAPHIC = "DEMOGRAPHIC"
    WORK_AUTHORIZATION = "WORK_AUTHORIZATION"
    SPONSORSHIP = "SPONSORSHIP"
    SALARY = "SALARY"
    SECURITY_CLEARANCE = "SECURITY_CLEARANCE"
    RELOCATION = "RELOCATION"
    CONFLICT_OF_INTEREST = "CONFLICT_OF_INTEREST"
    UNKNOWN = "UNKNOWN"


AUTOFILL_CATEGORIES = frozenset({QuestionCategory.SAFE_PROFILE, QuestionCategory.SAFE_JOB_SPECIFIC})


class SafeAnswer(BaseModel):
    """One pre-approved answer. Only SAFE_PROFILE / SAFE_JOB_SPECIFIC categories are allowed."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    patterns: list[str] = Field(min_length=1)
    value: str = Field(min_length=1)
    category: QuestionCategory
    source: str = Field(min_length=1)
    approved: bool = False
    manual_only: bool = False

    @field_validator("category")
    @classmethod
    def _must_be_safe(cls, value: QuestionCategory) -> QuestionCategory:
        if value not in AUTOFILL_CATEGORIES:
            raise ValueError(f"{value} answers can never be pre-approved for autofill")
        return value


class SafeAnswers(BaseModel):
    """config/safe_answers.yaml."""

    model_config = ConfigDict(extra="forbid")

    answers: list[SafeAnswer] = Field(default_factory=list)


class SensitiveQuestions(BaseModel):
    """config/sensitive_questions.yaml: keyword lists that force manual handling."""

    model_config = ConfigDict(extra="forbid")

    categories: dict[QuestionCategory, list[str]]

    @field_validator("categories")
    @classmethod
    def _no_safe_categories(
        cls, value: dict[QuestionCategory, list[str]]
    ) -> dict[QuestionCategory, list[str]]:
        for category in value:
            if category in AUTOFILL_CATEGORIES or category is QuestionCategory.UNKNOWN:
                raise ValueError(f"{category} cannot be listed as a sensitive category")
        required = set(QuestionCategory) - AUTOFILL_CATEGORIES - {QuestionCategory.UNKNOWN}
        missing = required - set(value)
        if missing:
            names = ", ".join(sorted(m.value for m in missing))
            raise ValueError(f"missing sensitive categories: {names}")
        return value
