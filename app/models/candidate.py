"""Candidate profile: the single source of truth for personal data and resume facts."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersonalInfo(_Strict):
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    preferred_name: str | None = None
    email: EmailStr
    phone: str = Field(min_length=7)
    city: str
    country: str
    linkedin_url: HttpUrl | None = None
    portfolio_url: HttpUrl | None = None
    github_url: HttpUrl | None = None
    current_job_title: str | None = None


class Skill(_Strict):
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    years: float | None = Field(default=None, ge=0)


class Certification(_Strict):
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    issuer: str | None = None
    year: int | None = Field(default=None, ge=1970, le=2100)


class Experience(_Strict):
    employer: str
    title: str
    location: str | None = None
    start_date: date
    end_date: date | None = None
    bullets: list[str] = Field(min_length=1)
    skills_used: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dates_ordered(self) -> Experience:
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        return self


class Education(_Strict):
    institution: str
    degree: str
    field_of_study: str | None = None
    graduation_year: int | None = Field(default=None, ge=1950, le=2100)


class CandidateProfile(_Strict):
    personal: PersonalInfo
    summary: str = ""
    total_years_experience: float = Field(ge=0)
    master_resume_path: str = "resumes/source/master_resume.docx"
    skills: list[Skill] = Field(min_length=1)
    certifications: list[Certification] = Field(default_factory=list)
    experience: list[Experience] = Field(min_length=1)
    education: list[Education] = Field(default_factory=list)

    @property
    def skill_names(self) -> set[str]:
        names: set[str] = set()
        for skill in self.skills:
            names.add(skill.name.lower())
            names.update(alias.lower() for alias in skill.aliases)
        return names
