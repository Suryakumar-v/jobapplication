"""Job posting and job-search preference models."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class JobSource(StrEnum):
    MANUAL_URL = "MANUAL_URL"
    WEBHOOK = "WEBHOOK"
    CSV = "CSV"
    JSON = "JSON"
    TEXT = "TEXT"
    RSS = "RSS"
    API = "API"
    BROWSER = "BROWSER"


class WorkplaceType(StrEnum):
    ONSITE = "ONSITE"
    HYBRID = "HYBRID"
    REMOTE = "REMOTE"
    UNKNOWN = "UNKNOWN"


class JobPosting(BaseModel):
    """Normalised job posting handed between n8n, the API and the database."""

    model_config = ConfigDict(extra="forbid")

    company: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=255)
    description: str = ""
    job_url: str | None = Field(default=None, max_length=2048)
    external_id: str | None = Field(default=None, max_length=255)
    location: str | None = Field(default=None, max_length=255)
    employment_type: str | None = Field(default=None, max_length=64)
    workplace_type: WorkplaceType = WorkplaceType.UNKNOWN
    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)
    posted_date: date | None = None
    source: JobSource = JobSource.MANUAL_URL


class SearchPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred_titles: list[str] = Field(min_length=1)
    excluded_titles: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    remote_preference: str = Field(default="any", pattern="^(any|remote|hybrid|onsite)$")
    employment_types: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    preferred_companies: list[str] = Field(default_factory=list)
    minimum_salary: int | None = Field(default=None, ge=0)
    maximum_job_age_days: int = Field(default=30, ge=1)
    max_jobs_per_run: int = Field(default=50, ge=1)
    max_applications_per_day: int = Field(default=10, ge=1)
