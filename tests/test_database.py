from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, create_engine, inspect, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.config import Settings
from app.database.migrations import (
    SCHEMA_VERSION,
    SchemaVersionError,
    get_schema_version,
    initialize_database,
)
from app.database.repositories import (
    ApplicationRepository,
    InvalidTransitionError,
    JobRepository,
    NotFoundError,
)
from app.database.tables import ApplicationRecord, JobRecord, SchemaVersionRecord
from app.models.application import ApplicationStatus


def make_job(suffix: str = "1") -> JobRecord:
    return JobRecord(
        job_id=f"JOB-{suffix}",
        fingerprint=f"fp-{suffix}",
        company="Synthetic Networks Ltd",
        normalized_company="synthetic networks ltd",
        title="Network Security Engineer",
        normalized_title="network security engineer",
        company_title_key="synthetic networks ltd|network security engineer",
        source="MANUAL_URL",
        job_url="https://jobs.example.com/1",
        location="Springfield",
    )


def test_initialize_creates_expected_tables(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert {
        "jobs",
        "duplicate_records",
        "applications",
        "status_history",
        "tailored_resumes",
        "approvals",
        "approval_attempts",
        "form_runs",
        "schema_version",
    } <= tables
    assert get_schema_version(engine) == SCHEMA_VERSION


def test_initialize_is_idempotent(engine: Engine, session: Session) -> None:
    initialize_database(engine)
    initialize_database(engine)
    assert len(session.scalars(select(SchemaVersionRecord)).all()) == 1


def test_newer_schema_is_refused(engine: Engine, session: Session) -> None:
    session.add(SchemaVersionRecord(version=SCHEMA_VERSION + 1))
    session.commit()
    with pytest.raises(SchemaVersionError):
        initialize_database(engine)


def test_foreign_keys_enforced(session: Session) -> None:
    session.add(
        ApplicationRecord(
            application_id="APP-1",
            job_id="missing",
            fingerprint="x",
            company="c",
            job_title="t",
            source="TEXT",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_fingerprint_is_unique(session: Session) -> None:
    repo = JobRepository(session)
    repo.add(make_job("1"))
    duplicate = make_job("2")
    duplicate.fingerprint = "fp-1"
    with pytest.raises(IntegrityError):
        repo.add(duplicate)


def test_datetimes_round_trip_as_utc(session: Session) -> None:
    job = JobRepository(session).add(make_job())
    session.commit()
    session.expire_all()
    loaded = JobRepository(session).get_by_job_id(job.job_id)
    assert loaded is not None
    assert loaded.created_at.tzinfo is not None
    assert loaded.created_at.utcoffset() == timedelta(0)


def test_naive_datetime_rejected(session: Session) -> None:
    job = make_job()
    job.date_discovered = datetime(2026, 9, 29)
    session.add(job)
    with pytest.raises(StatementError, match="naive datetime"):
        session.commit()


def test_application_id_sequence(session: Session) -> None:
    jobs = JobRepository(session)
    applications = ApplicationRepository(session)
    other_day = datetime(2020, 1, 2, tzinfo=UTC).date()
    assert applications.next_application_id(other_day) == "APP-20200102-0001"
    first = applications.create_for_job(jobs.add(make_job("1")))
    second = applications.create_for_job(jobs.add(make_job("2")))
    assert first.application_id.endswith("-0001")
    assert second.application_id.endswith("-0002")
    assert applications.next_application_id().endswith("-0003")
    assert applications.next_application_id(other_day) == "APP-20200102-0001"


def test_one_application_per_job(session: Session) -> None:
    job = JobRepository(session).add(make_job())
    applications = ApplicationRepository(session)
    applications.create_for_job(job)
    with pytest.raises(InvalidTransitionError):
        applications.create_for_job(job)


def test_status_changes_are_recorded_with_timestamps(session: Session) -> None:
    job = JobRepository(session).add(make_job())
    applications = ApplicationRepository(session)
    application = applications.create_for_job(job)
    applications.set_status(application.application_id, ApplicationStatus.FORM_STARTED)
    applications.set_status(
        application.application_id, ApplicationStatus.AWAITING_APPROVAL, "ready"
    )
    session.commit()
    loaded = applications.get(application.application_id)
    assert loaded is not None
    assert loaded.application_status == "AWAITING_APPROVAL"
    assert loaded.application_started_at is not None
    assert loaded.awaiting_approval_at is not None
    assert [h.to_status for h in loaded.history] == [
        "DISCOVERED",
        "FORM_STARTED",
        "AWAITING_APPROVAL",
    ]


def test_submitted_is_final(session: Session) -> None:
    job = JobRepository(session).add(make_job())
    applications = ApplicationRepository(session)
    application = applications.create_for_job(job)
    applications.set_status(application.application_id, ApplicationStatus.SUBMITTED)
    with pytest.raises(InvalidTransitionError):
        applications.set_status(application.application_id, ApplicationStatus.FAILED)
    assert (
        applications.set_status(
            application.application_id, ApplicationStatus.SUBMITTED
        ).submitted_at
        is not None
    )


def test_unknown_application_raises(session: Session) -> None:
    with pytest.raises(NotFoundError):
        ApplicationRepository(session).set_status("APP-0", ApplicationStatus.FAILED)


def test_database_persists_between_engines(engine: Engine, settings: Settings) -> None:
    other = create_engine(f"sqlite:///{settings.database_file.as_posix()}")
    assert inspect(other).has_table("jobs")
    other.dispose()
