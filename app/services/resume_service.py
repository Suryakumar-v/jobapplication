"""Generate, validate and record a tailored resume for an analysed job."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database.repositories import ApplicationRepository, JobRepository, NotFoundError
from app.database.tables import TailoredResumeRecord
from app.models.application import ApplicationStatus
from app.models.scoring import MatchResult, Recommendation
from app.services.matching_service import JobMatchingService, JobNotFoundError
from app.services.resume_tailor import (
    ResumeValidation,
    build_plan,
    describe_plan,
    parse_docx,
    read_master_text,
    render_docx,
    validate_resume,
)
from app.utils.file_utils import is_within, safe_filename
from app.utils.hashing import sha256_file
from app.utils.logging_config import get_logger


class TailoringNotAllowedError(ValueError):
    pass


class ValidationFailedError(ValueError):
    def __init__(self, validation: ResumeValidation) -> None:
        super().__init__("Tailored resume failed truthfulness validation")
        self.validation = validation


class TailoredResumeSummary(BaseModel):
    version: int
    path: str
    master_resume_sha256: str
    tailoring_summary: str | None
    validation: ResumeValidation
    created_at: datetime


class TailoringOutcome(BaseModel):
    job_id: str
    application_id: str
    status: str
    version: int
    path: str
    master_resume_sha256: str
    tailoring_summary: str
    validation: ResumeValidation


class ResumeTailoringService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.jobs = JobRepository(session)
        self.applications = ApplicationRepository(session)
        self.matching = JobMatchingService(session, settings)

    def _stored_path(self, path: Path) -> str:
        root = self.settings.project_root.resolve()
        resolved = path.resolve()
        return resolved.relative_to(root).as_posix() if is_within(root, resolved) else str(resolved)

    def _next_version(self, application_id: str) -> int:
        count = self.session.scalar(
            select(func.count())
            .select_from(TailoredResumeRecord)
            .where(TailoredResumeRecord.application_id == application_id)
        )
        return (count or 0) + 1

    def tailor(self, job_id: str) -> TailoringOutcome:
        job = self.jobs.get_by_job_id(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        config = self.matching.config
        if "candidate_profile" in config.example_files:
            raise TailoringNotAllowedError(
                "config/candidate_profile.yaml is missing; the synthetic example profile "
                "must never be used to produce a resume"
            )
        application = self.applications.get_by_job_id(job_id)
        if application is None:
            if job.status != ApplicationStatus.ANALYZED.value:
                raise TailoringNotAllowedError(
                    f"Job {job_id} is {job.status}; only ANALYZED jobs can be tailored"
                )
        elif application.application_status != ApplicationStatus.PREPARED.value:
            raise TailoringNotAllowedError(
                f"Application {application.application_id} is {application.application_status};"
                " a resume can only be regenerated while it is PREPARED"
            )
        if job.recommendation != Recommendation.APPLY.value or job.matching_details is None:
            raise TailoringNotAllowedError(f"Job {job_id} has no APPLY match result")

        profile = config.candidate
        match = MatchResult.model_validate(job.matching_details)
        master_path = self.settings.resolve(Path(profile.master_resume_path))
        master_text = read_master_text(master_path)
        master_hash = sha256_file(master_path)

        application_id = (
            application.application_id if application else self.applications.next_application_id()
        )
        version = self._next_version(application_id)
        directory = self.settings.generated_resumes_dir / application_id
        if not is_within(self.settings.generated_resumes_dir, directory):
            raise TailoringNotAllowedError("Invalid output location")
        directory.mkdir(parents=True, exist_ok=True)
        base = safe_filename(f"{profile.personal.first_name} {profile.personal.last_name} Resume")
        stem = base if version == 1 else f"{base}-v{version}"
        target = directory / f"{stem}.docx"
        if target.exists() or target.resolve() == master_path.resolve():
            raise TailoringNotAllowedError("Refusing to overwrite an existing file")

        plan = build_plan(profile, master_text, match, self.matching.engine)
        render_docx(profile, plan, target)
        validation = validate_resume(parse_docx(target), profile, master_text, self.matching.engine)
        if not validation.passed:
            target.unlink(missing_ok=True)
            get_logger("app.audit").warning(
                "tailored resume rejected",
                extra={"operation": "tailor_resume", "job_id": job_id},
            )
            raise ValidationFailedError(validation)

        summary = describe_plan(plan, match)
        stored = self._stored_path(target)
        if application is None:
            application = self.applications.create_for_job(job, ApplicationStatus.ANALYZED)
        self.session.add(
            TailoredResumeRecord(
                application_id=application.application_id,
                job_id=job.job_id,
                master_resume_hash=master_hash,
                path=stored,
                tailoring_summary=summary,
                validation_result=validation.model_dump(mode="json"),
            )
        )
        application.tailored_resume_path = stored
        self.applications.set_status(
            application.application_id, ApplicationStatus.PREPARED, f"resume tailored (v{version})"
        )
        job.status = ApplicationStatus.PREPARED.value
        self.session.flush()
        get_logger("app.audit").info(
            "resume tailored",
            extra={
                "operation": "tailor_resume",
                "job_id": job_id,
                "application_id": application.application_id,
                "version": version,
            },
        )
        return TailoringOutcome(
            job_id=job.job_id,
            application_id=application.application_id,
            status=application.application_status,
            version=version,
            path=stored,
            master_resume_sha256=master_hash,
            tailoring_summary=summary,
            validation=validation,
        )

    def history(self, application_id: str) -> list[TailoredResumeSummary]:
        if self.applications.get(application_id) is None:
            raise NotFoundError(application_id)
        records = self.session.scalars(
            select(TailoredResumeRecord)
            .where(TailoredResumeRecord.application_id == application_id)
            .order_by(TailoredResumeRecord.id)
        )
        return [
            TailoredResumeSummary(
                version=index,
                path=record.path,
                master_resume_sha256=record.master_resume_hash,
                tailoring_summary=record.tailoring_summary,
                validation=ResumeValidation.model_validate(record.validation_result),
                created_at=record.created_at,
            )
            for index, record in enumerate(records, start=1)
        ]
