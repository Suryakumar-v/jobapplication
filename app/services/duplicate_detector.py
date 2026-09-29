"""Duplicate detection against the jobs and applications already in the database."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from app.database.tables import ApplicationRecord, DuplicateRecord, JobRecord
from app.services.job_normalizer import NormalizedJob
from app.utils.text_utils import normalize_text

FUZZY_CANDIDATE_LIMIT = 200
FUZZY_COMPARE_CHARS = 3000


class DuplicateReason(StrEnum):
    FINGERPRINT = "FINGERPRINT"
    URL = "URL"
    EXTERNAL_ID = "EXTERNAL_ID"
    COMPANY_TITLE = "COMPANY_TITLE"
    DESCRIPTION_HASH = "DESCRIPTION_HASH"
    EXISTING_APPLICATION = "EXISTING_APPLICATION"
    FUZZY_DESCRIPTION = "FUZZY_DESCRIPTION"


@dataclass(frozen=True)
class DuplicateResult:
    is_duplicate: bool
    reason: DuplicateReason | None = None
    original_job_id: str | None = None
    existing_application_id: str | None = None


NOT_DUPLICATE = DuplicateResult(is_duplicate=False)


class DuplicateDetector:
    """Checks are ordered from most to least certain; the first match wins."""

    def __init__(self, session: Session, fuzzy_threshold: float = 0.0) -> None:
        self.session = session
        self.fuzzy_threshold = fuzzy_threshold

    def check(self, job: NormalizedJob) -> DuplicateResult:
        for finder in (
            self._by_fingerprint,
            self._by_url,
            self._by_external_id,
            self._by_company_title,
            self._by_description_hash,
        ):
            original = finder(job)
            if original is not None:
                reason, match = original
                return self._result(reason, match.job_id)
        from_application = self._by_existing_application(job)
        if from_application is not None:
            return from_application
        if self.fuzzy_threshold > 0:
            fuzzy = self._by_fuzzy_description(job)
            if fuzzy is not None:
                return self._result(DuplicateReason.FUZZY_DESCRIPTION, fuzzy.job_id)
        return NOT_DUPLICATE

    def record(self, job: NormalizedJob, result: DuplicateResult) -> DuplicateRecord:
        """Persist why a job was rejected as a duplicate and which job it duplicates."""
        if not result.is_duplicate or result.reason is None or result.original_job_id is None:
            raise ValueError("Only duplicate results can be recorded")
        entry = DuplicateRecord(
            fingerprint=job.fingerprint,
            duplicate_of_job_id=result.original_job_id,
            reason=result.reason.value,
            source=job.posting.source.value,
            job_url=job.posting.job_url,
        )
        self.session.add(entry)
        self.session.flush()
        return entry

    def _result(self, reason: DuplicateReason, original_job_id: str) -> DuplicateResult:
        application_id = self.session.scalar(
            select(ApplicationRecord.application_id).where(
                ApplicationRecord.job_id == original_job_id
            )
        )
        return DuplicateResult(True, reason, original_job_id, application_id)

    def _first(self, *conditions: ColumnElement[bool]) -> JobRecord | None:
        return self.session.scalars(
            select(JobRecord).where(*conditions).order_by(JobRecord.id).limit(1)
        ).first()

    def _by_fingerprint(self, job: NormalizedJob) -> tuple[DuplicateReason, JobRecord] | None:
        match = self._first(JobRecord.fingerprint == job.fingerprint)
        return (DuplicateReason.FINGERPRINT, match) if match else None

    def _by_url(self, job: NormalizedJob) -> tuple[DuplicateReason, JobRecord] | None:
        if job.normalized_url is None:
            return None
        match = self._first(JobRecord.normalized_url == job.normalized_url)
        return (DuplicateReason.URL, match) if match else None

    def _by_external_id(self, job: NormalizedJob) -> tuple[DuplicateReason, JobRecord] | None:
        if job.external_id is None:
            return None
        # Job IDs are only unique within one employer, so the company must match too.
        match = self._first(
            JobRecord.external_id == job.external_id,
            JobRecord.normalized_company == job.normalized_company,
        )
        return (DuplicateReason.EXTERNAL_ID, match) if match else None

    def _by_company_title(self, job: NormalizedJob) -> tuple[DuplicateReason, JobRecord] | None:
        candidates = self.session.scalars(
            select(JobRecord).where(JobRecord.company_title_key == job.company_title_key)
        ).all()
        for candidate in candidates:
            existing_location = normalize_text(candidate.location or "")
            # Same title at the same employer in a different place is a different job.
            if not existing_location or not job.normalized_location:
                return DuplicateReason.COMPANY_TITLE, candidate
            if existing_location == job.normalized_location:
                return DuplicateReason.COMPANY_TITLE, candidate
        return None

    def _by_description_hash(self, job: NormalizedJob) -> tuple[DuplicateReason, JobRecord] | None:
        if job.description_hash is None:
            return None
        match = self._first(JobRecord.description_hash == job.description_hash)
        return (DuplicateReason.DESCRIPTION_HASH, match) if match else None

    def _by_existing_application(self, job: NormalizedJob) -> DuplicateResult | None:
        application = self.session.scalar(
            select(ApplicationRecord).where(ApplicationRecord.fingerprint == job.fingerprint)
        )
        if application is None:
            return None
        return DuplicateResult(
            True,
            DuplicateReason.EXISTING_APPLICATION,
            application.job_id,
            application.application_id,
        )

    def _by_fuzzy_description(self, job: NormalizedJob) -> JobRecord | None:
        if not job.posting.description:
            return None
        mine = normalize_text(job.posting.description)[:FUZZY_COMPARE_CHARS]
        candidates = self.session.scalars(
            select(JobRecord)
            .where(
                JobRecord.normalized_company == job.normalized_company,
                JobRecord.description != "",
            )
            .order_by(JobRecord.id.desc())
            .limit(FUZZY_CANDIDATE_LIMIT)
        ).all()
        for candidate in candidates:
            theirs = normalize_text(candidate.description)[:FUZZY_COMPARE_CHARS]
            if SequenceMatcher(None, mine, theirs).ratio() >= self.fuzzy_threshold:
                return candidate
        return None
