"""SQLAlchemy table definitions. SQLite is the authoritative store."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Date, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, UTCDateTime, utcnow
from app.models.application import ApplicationStatus


class JobRecord(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    normalized_url: Mapped[str | None] = mapped_column(String(2048), index=True)
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    company: Mapped[str] = mapped_column(String(255))
    normalized_company: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255))
    normalized_title: Mapped[str] = mapped_column(String(255))
    company_title_key: Mapped[str] = mapped_column(String(512), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    description_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    location: Mapped[str | None] = mapped_column(String(255))
    employment_type: Mapped[str | None] = mapped_column(String(64))
    workplace_type: Mapped[str | None] = mapped_column(String(32))
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    posted_date: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(32))
    job_url: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(32), default=ApplicationStatus.DISCOVERED.value)
    match_score: Mapped[float | None] = mapped_column(Float)
    recommendation: Mapped[str | None] = mapped_column(String(16))
    matching_explanation: Mapped[str | None] = mapped_column(Text)
    missing_requirements: Mapped[list[str] | None] = mapped_column(JSON)
    matching_details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    analyzed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    date_discovered: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    application: Mapped[ApplicationRecord | None] = relationship(
        back_populates="job", uselist=False
    )


class DuplicateRecord(Base):
    __tablename__ = "duplicate_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    duplicate_of_job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"))
    reason: Mapped[str] = mapped_column(String(64))
    source: Mapped[str | None] = mapped_column(String(32))
    job_url: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ApplicationRecord(Base):
    """One row per prepared application; also the tracker row exported to Excel/CSV."""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    company: Mapped[str] = mapped_column(String(255))
    job_title: Mapped[str] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255))
    employment_type: Mapped[str | None] = mapped_column(String(64))
    workplace_type: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32))
    job_url: Mapped[str | None] = mapped_column(String(2048))
    portal: Mapped[str | None] = mapped_column(String(64))
    date_discovered: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    match_score: Mapped[float | None] = mapped_column(Float)
    recommendation: Mapped[str | None] = mapped_column(String(16))
    matching_explanation: Mapped[str | None] = mapped_column(Text)
    missing_requirements: Mapped[list[str] | None] = mapped_column(JSON)
    tailored_resume_path: Mapped[str | None] = mapped_column(String(1024))
    application_status: Mapped[str] = mapped_column(
        String(32), default=ApplicationStatus.DISCOVERED.value, index=True
    )
    application_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    awaiting_approval_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    confirmation_number: Mapped[str | None] = mapped_column(String(255))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    screenshot_directory: Mapped[str | None] = mapped_column(String(1024))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    job: Mapped[JobRecord] = relationship(back_populates="application")
    history: Mapped[list[StatusHistoryRecord]] = relationship(
        back_populates="application", order_by="StatusHistoryRecord.id"
    )


class StatusHistoryRecord(Base):
    __tablename__ = "status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.application_id"))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    application: Mapped[ApplicationRecord] = relationship(back_populates="history")


class TailoredResumeRecord(Base):
    __tablename__ = "tailored_resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.application_id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"))
    master_resume_hash: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(String(1024))
    tailoring_summary: Mapped[str | None] = mapped_column(Text)
    validation_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class FormRunRecord(Base):
    """One browser form-preparation attempt. Stores field decisions, never answer values."""

    __tablename__ = "form_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.application_id"), index=True
    )
    portal: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(2048))
    outcome: Mapped[str] = mapped_column(String(32))
    blockers: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    screenshots: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ApprovalRecord(Base):
    """Single-use, time-limited approval bound to one application. Only a hash is stored."""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.application_id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (Index("ix_approvals_application", "application_id"),)


class ApprovalAttemptRecord(Base):
    """Audit trail of accepted and rejected approval attempts (no raw tokens)."""

    __tablename__ = "approval_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[str | None] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AIAnalysisRecord(Base):
    """Advisory AI reading of one posting. Never feeds the score, status or resume."""

    __tablename__ = "ai_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    insights: Mapped[dict[str, Any]] = mapped_column(JSON)
    comparison: Mapped[dict[str, Any]] = mapped_column(JSON)
    using_example_config: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SchemaVersionRecord(Base):
    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
