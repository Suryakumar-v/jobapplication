"""Scoring configuration and decision values."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Recommendation(StrEnum):
    APPLY = "APPLY"
    REVIEW = "REVIEW"
    SKIP = "SKIP"
    INELIGIBLE = "INELIGIBLE"


class ScoringWeights(BaseModel):
    """Percent weights; must sum to 100."""

    model_config = ConfigDict(extra="forbid")

    required_skills: float = Field(ge=0)
    relevant_experience: float = Field(ge=0)
    preferred_skills: float = Field(ge=0)
    certifications: float = Field(ge=0)
    job_title_relevance: float = Field(ge=0)
    location_and_remote: float = Field(ge=0)
    employment_type: float = Field(ge=0)

    @model_validator(mode="after")
    def _sum_to_100(self) -> ScoringWeights:
        total = sum(self.model_dump().values())
        if abs(total - 100.0) > 1e-6:
            raise ValueError(f"weights must sum to 100 (got {total})")
        return self


class ScoringConfig(BaseModel):
    """config/scoring_weights.yaml."""

    model_config = ConfigDict(extra="forbid")

    minimum_score: int = Field(default=75, ge=0, le=100)
    weights: ScoringWeights


class VocabularyEntry(BaseModel):
    """A term the matcher can recognise in a job description, with equivalent spellings."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_plain_string(cls, value: Any) -> Any:
        return {"name": value} if isinstance(value, str) else value


class SkillVocabulary(BaseModel):
    """config/skill_vocabulary.yaml: terms detectable in postings, not claims about you."""

    model_config = ConfigDict(extra="forbid")

    skills: list[VocabularyEntry] = Field(default_factory=list)
    certifications: list[VocabularyEntry] = Field(default_factory=list)

    @field_validator("skills", "certifications")
    @classmethod
    def _no_blank_terms(cls, entries: list[VocabularyEntry]) -> list[VocabularyEntry]:
        for entry in entries:
            if not entry.name.strip() or any(not alias.strip() for alias in entry.aliases):
                raise ValueError("vocabulary terms must not be blank")
        return entries


class MatchResult(BaseModel):
    """Deterministic, explainable outcome of matching one job against the candidate profile."""

    overall_score: float
    threshold: int
    recommendation: Recommendation
    required_skills_matched: list[str] = Field(default_factory=list)
    required_skills_missing: list[str] = Field(default_factory=list)
    preferred_skills_matched: list[str] = Field(default_factory=list)
    preferred_skills_missing: list[str] = Field(default_factory=list)
    certifications_matched: list[str] = Field(default_factory=list)
    certifications_missing: list[str] = Field(default_factory=list)
    required_skills_score: float
    preferred_skills_score: float
    certifications_score: float
    experience_score: float
    title_score: float
    location_score: float
    employment_type_score: float
    mandatory_requirements_met: bool
    mandatory_failures: list[str] = Field(default_factory=list)
    mandatory_unverified: list[str] = Field(default_factory=list)
    filter_reasons: list[str] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    explanation: str

    @property
    def missing_requirements(self) -> list[str]:
        return [
            *self.required_skills_missing,
            *self.certifications_missing,
            *self.mandatory_failures,
        ]
