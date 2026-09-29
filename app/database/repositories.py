"""Data-access helpers."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.base import utcnow
from app.database.tables import ApplicationRecord, JobRecord, StatusHistoryRecord
from app.models.application import ApplicationStatus

FINAL_STATUSES = frozenset(
    {ApplicationStatus.SUBMITTED, ApplicationStatus.WITHDRAWN, ApplicationStatus.DUPLICATE}
)

_STATUS_TIMESTAMPS: dict[ApplicationStatus, str] = {
    ApplicationStatus.FORM_STARTED: "application_started_at",
    ApplicationStatus.AWAITING_APPROVAL: "awaiting_approval_at",
    ApplicationStatus.SUBMITTED: "submitted_at",
}


class NotFoundError(LookupError):
    pass


class InvalidTransitionError(ValueError):
    pass


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, job: JobRecord) -> JobRecord:
        self.session.add(job)
        self.session.flush()
        return job

    def get_by_job_id(self, job_id: str) -> JobRecord | None:
        return self.session.scalar(select(JobRecord).where(JobRecord.job_id == job_id))

    def get_by_fingerprint(self, fingerprint: str) -> JobRecord | None:
        return self.session.scalar(select(JobRecord).where(JobRecord.fingerprint == fingerprint))

    def list_jobs(
        self, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[JobRecord]:
        query = select(JobRecord).order_by(JobRecord.id.desc()).limit(limit).offset(offset)
        if status is not None:
            query = query.where(JobRecord.status == status)
        return list(self.session.scalars(query))


class ApplicationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def next_application_id(self, on: date | None = None) -> str:
        day = (on or utcnow().date()).strftime("%Y%m%d")
        prefix = f"APP-{day}-"
        latest = self.session.scalar(
            select(func.max(ApplicationRecord.application_id)).where(
                ApplicationRecord.application_id.like(f"{prefix}%")
            )
        )
        sequence = int(latest.rsplit("-", 1)[1]) + 1 if latest else 1
        return f"{prefix}{sequence:04d}"

    def get(self, application_id: str) -> ApplicationRecord | None:
        return self.session.scalar(
            select(ApplicationRecord).where(ApplicationRecord.application_id == application_id)
        )

    def get_by_job_id(self, job_id: str) -> ApplicationRecord | None:
        return self.session.scalar(
            select(ApplicationRecord).where(ApplicationRecord.job_id == job_id)
        )

    def create_for_job(
        self, job: JobRecord, status: ApplicationStatus = ApplicationStatus.DISCOVERED
    ) -> ApplicationRecord:
        """Create the single application record for a job; refuses a second one."""
        if self.get_by_job_id(job.job_id) is not None:
            raise InvalidTransitionError(f"Job {job.job_id} already has an application record")
        application = ApplicationRecord(
            application_id=self.next_application_id(),
            job_id=job.job_id,
            fingerprint=job.fingerprint,
            company=job.company,
            job_title=job.title,
            location=job.location,
            employment_type=job.employment_type,
            workplace_type=job.workplace_type,
            source=job.source,
            job_url=job.job_url,
            date_discovered=job.date_discovered,
            match_score=job.match_score,
            recommendation=job.recommendation,
            matching_explanation=job.matching_explanation,
            missing_requirements=job.missing_requirements,
            application_status=status.value,
        )
        self.session.add(application)
        self.session.flush()
        self.session.add(
            StatusHistoryRecord(
                application_id=application.application_id,
                from_status=None,
                to_status=status.value,
                reason="created",
            )
        )
        self.session.flush()
        return application

    def set_status(
        self,
        application_id: str,
        new_status: ApplicationStatus,
        reason: str | None = None,
        *,
        at: datetime | None = None,
    ) -> ApplicationRecord:
        application = self.get(application_id)
        if application is None:
            raise NotFoundError(f"Application {application_id} not found")
        current = ApplicationStatus(application.application_status)
        if current in FINAL_STATUSES and new_status != current:
            raise InvalidTransitionError(f"{current} is final; cannot change to {new_status}")
        if current == new_status:
            return application
        moment = at or utcnow()
        application.application_status = new_status.value
        timestamp_field = _STATUS_TIMESTAMPS.get(new_status)
        if timestamp_field is not None:
            setattr(application, timestamp_field, moment)
        self.session.add(
            StatusHistoryRecord(
                application_id=application_id,
                from_status=current.value,
                to_status=new_status.value,
                reason=reason,
                created_at=moment,
            )
        )
        self.session.flush()
        return application
