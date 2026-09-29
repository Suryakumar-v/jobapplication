from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.repositories import ApplicationRepository, JobRepository
from app.database.tables import DuplicateRecord, JobRecord
from app.models.job import JobPosting
from app.services.duplicate_detector import DuplicateDetector, DuplicateReason
from app.services.job_importer import _to_record
from app.services.job_normalizer import NormalizedJob, normalize_job

LONG_DESCRIPTION = (
    "Required: network security, RADIUS, firewall policy management. "
    "Preferred: Aruba ClearPass and Splunk. Responsibilities include policy changes."
)


def make(session: Session, **overrides: object) -> NormalizedJob:
    data: dict[str, object] = {
        "company": "Synthetic Networks Ltd",
        "title": "Network Security Engineer",
        "job_url": "https://jobs.example.com/openings/1",
        "external_id": "SYN-1",
        "location": "Springfield",
        "description": LONG_DESCRIPTION,
    }
    data.update(overrides)
    return normalize_job(JobPosting.model_validate(data))


def store(session: Session, job: NormalizedJob) -> JobRecord:
    return JobRepository(session).add(_to_record(job))


def test_new_job_is_not_duplicate(session: Session) -> None:
    assert not DuplicateDetector(session).check(make(session)).is_duplicate


def test_same_fingerprint(session: Session) -> None:
    original = store(session, make(session))
    result = DuplicateDetector(session).check(make(session))
    assert result.is_duplicate
    assert result.reason is DuplicateReason.FINGERPRINT
    assert result.original_job_id == original.job_id


def test_same_url_different_everything_else(session: Session) -> None:
    original = store(session, make(session))
    other = make(
        session,
        title="Totally Different",
        external_id="X",
        description="",
        job_url="http://www.jobs.example.com/openings/1/?utm_source=feed",
    )
    result = DuplicateDetector(session).check(other)
    assert result.reason is DuplicateReason.URL
    assert result.original_job_id == original.job_id


def test_same_external_id_same_company(session: Session) -> None:
    store(session, make(session))
    other = make(session, title="Other", job_url="https://x.example.com/9", description="")
    assert DuplicateDetector(session).check(other).reason is DuplicateReason.EXTERNAL_ID


def test_same_external_id_different_company_is_not_duplicate(session: Session) -> None:
    store(session, make(session))
    other = make(
        session,
        company="Another Corp",
        title="Other",
        job_url="https://x.example.com/9",
        description="",
    )
    assert not DuplicateDetector(session).check(other).is_duplicate


def test_same_company_and_title(session: Session) -> None:
    store(session, make(session))
    other = make(
        session,
        company="synthetic networks",
        title="Network Security Engineer (Remote)",
        external_id=None,
        job_url="https://other.example.com/zzz",
        description="",
    )
    assert DuplicateDetector(session).check(other).reason is DuplicateReason.COMPANY_TITLE


def test_same_company_title_different_location_is_distinct(session: Session) -> None:
    store(session, make(session))
    other = make(
        session,
        location="Shelbyville",
        external_id=None,
        job_url="https://other.example.com/zzz",
        description="",
    )
    assert not DuplicateDetector(session).check(other).is_duplicate


def test_same_company_title_missing_location_is_duplicate(session: Session) -> None:
    store(session, make(session))
    other = make(
        session, location=None, external_id=None, job_url="https://o.example.com/1", description=""
    )
    assert DuplicateDetector(session).check(other).reason is DuplicateReason.COMPANY_TITLE


def test_same_description_hash_other_company(session: Session) -> None:
    store(session, make(session))
    reposted = make(
        session,
        company="Recruiter Agency",
        title="Security Person",
        external_id="R-9",
        job_url="https://agency.example.com/9",
    )
    assert DuplicateDetector(session).check(reposted).reason is DuplicateReason.DESCRIPTION_HASH


def test_short_descriptions_do_not_collide(session: Session) -> None:
    store(session, make(session, description="Great job"))
    other = make(
        session,
        company="Another Corp",
        title="Other",
        external_id="Q",
        job_url="https://q.example.com/1",
        description="Great job",
    )
    assert not DuplicateDetector(session).check(other).is_duplicate


def test_existing_application_is_reported(session: Session) -> None:
    job = store(session, make(session))
    application = ApplicationRepository(session).create_for_job(job)
    result = DuplicateDetector(session).check(make(session))
    assert result.is_duplicate
    assert result.existing_application_id == application.application_id


def test_existing_application_matched_by_fingerprint_when_job_row_lacks_match(
    session: Session,
) -> None:
    job = store(session, make(session))
    ApplicationRepository(session).create_for_job(job)
    # The job row was altered so job-level checks no longer match; the application still does.
    session.query(JobRecord).update({"fingerprint": "changed", "normalized_url": None})
    session.query(JobRecord).update(
        {"external_id": None, "company_title_key": "x", "description_hash": None}
    )
    result = DuplicateDetector(session).check(make(session))
    assert result.reason is DuplicateReason.EXISTING_APPLICATION
    assert result.original_job_id == job.job_id


def test_fuzzy_matching_is_off_by_default_and_optional(session: Session) -> None:
    store(session, make(session))
    near = make(
        session,
        title="Unrelated Title",
        external_id="N-1",
        job_url="https://n.example.com/1",
        description=LONG_DESCRIPTION.replace("policy changes", "policy updates"),
    )
    assert not DuplicateDetector(session).check(near).is_duplicate
    fuzzy = DuplicateDetector(session, fuzzy_threshold=0.9).check(near)
    assert fuzzy.reason is DuplicateReason.FUZZY_DESCRIPTION


def test_fuzzy_ignores_dissimilar_descriptions(session: Session) -> None:
    store(session, make(session))
    other = make(
        session,
        title="Chef",
        external_id="C-1",
        job_url="https://c.example.com/1",
        description=(
            "Prepare meals, manage the kitchen and order supplies for the restaurant daily."
        ),
    )
    assert not DuplicateDetector(session, fuzzy_threshold=0.9).check(other).is_duplicate


def test_duplicate_is_recorded_and_linked(session: Session) -> None:
    original = store(session, make(session))
    detector = DuplicateDetector(session)
    candidate = make(session, job_url="https://mirror.example.com/x")
    result = detector.check(candidate)
    entry = detector.record(candidate, result)
    stored = session.scalars(select(DuplicateRecord)).one()
    assert stored.id == entry.id
    assert stored.duplicate_of_job_id == original.job_id
    assert result.reason is not None
    assert stored.reason == result.reason.value
    assert session.scalars(select(JobRecord)).all() == [original]


def test_recording_a_non_duplicate_is_refused(session: Session) -> None:
    detector = DuplicateDetector(session)
    candidate = make(session)
    with pytest.raises(ValueError, match="Only duplicate"):
        detector.record(candidate, detector.check(candidate))
