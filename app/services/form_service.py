"""Prepare an application form in the browser for human review. Never submits."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation.form_mapping import FieldReport, FieldStatus, SensitiveMatcher
from app.automation.portals import select_portal
from app.automation.tasks import FillRequest, FillResult, fill_task
from app.automation.url_guard import check_portal_enabled, check_url, hostname_of
from app.automation.worker import BrowserWorker
from app.config import LOOPBACK_HOSTS, Settings
from app.database.repositories import ApplicationRepository, NotFoundError
from app.database.tables import ApplicationRecord, FormRunRecord
from app.models.application import ApplicationStatus
from app.services.config_loader import load_form_config
from app.utils.file_utils import is_within
from app.utils.logging_config import get_logger
from app.utils.redaction import redact_url

STARTABLE = frozenset(
    {
        ApplicationStatus.PREPARED,
        ApplicationStatus.FORM_STARTED,
        ApplicationStatus.AWAITING_APPROVAL,
        ApplicationStatus.FAILED,
    }
)
OUTCOME_READY = "READY_FOR_REVIEW"
OUTCOME_BLOCKED = "BLOCKED"


class FormNotAllowedError(ValueError):
    pass


class FormRunOutcome(BaseModel):
    application_id: str
    application_status: str
    outcome: str
    portal: str
    url: str
    live_session: bool
    blockers: list[dict[str, str]]
    screenshots: list[str]
    filled: int
    uploaded: int
    left_blank: int
    failed: int
    manual_fields: list[FieldReport]
    fields: list[FieldReport]
    created_at: datetime | None = None


def _needs_person(report: FieldReport) -> bool:
    if report.status is FieldStatus.FILL_FAILED:
        return True
    blank = report.status.value.startswith("LEFT_BLANK")
    sensitive = report.status is FieldStatus.LEFT_BLANK_SENSITIVE
    return blank and (report.required or sensitive)


def relative_path(settings: Settings, path: Path) -> str:
    root = settings.project_root.resolve()
    resolved = path.resolve()
    return resolved.relative_to(root).as_posix() if is_within(root, resolved) else str(resolved)


class FormPreparationService:
    def __init__(self, session: Session, settings: Settings, worker: BrowserWorker) -> None:
        self.session = session
        self.settings = settings
        self.worker = worker
        self.applications = ApplicationRepository(session)
        self.log = get_logger("app.audit")

    def _application(self, application_id: str) -> ApplicationRecord:
        application = self.applications.get(application_id)
        if application is None:
            raise NotFoundError(application_id)
        return application

    def _resume_path(self, application: ApplicationRecord) -> Path:
        if not application.tailored_resume_path:
            raise FormNotAllowedError("The application has no tailored resume; tailor one first")
        path = self.settings.resolve(Path(application.tailored_resume_path))
        if not is_within(self.settings.generated_resumes_dir, path) or not path.is_file():
            raise FormNotAllowedError("The tailored resume file is missing or in an unsafe place")
        return path

    def start(self, application_id: str, application_url: str | None = None) -> FormRunOutcome:
        application = self._application(application_id)
        if application.approved_at is not None:
            raise FormNotAllowedError(
                "A submission was already attempted for this application. Check the portal "
                "and finish it there; the form is not opened again to avoid a duplicate"
            )
        status = ApplicationStatus(application.application_status)
        if status not in STARTABLE:
            raise FormNotAllowedError(
                f"Application {application_id} is {status.value}; it must be PREPARED "
                "(resume tailored) before a form can be opened"
            )
        resume = self._resume_path(application)
        config = load_form_config(self.settings)
        url = check_url(
            application_url or application.job_url or "", self.settings.browser_allowed_hosts
        )
        check_portal_enabled(url, config.portals)
        portal = select_portal(config.portals)
        if config.using_example_answers and hostname_of(url) not in LOOPBACK_HOSTS:
            raise FormNotAllowedError(
                "config/safe_answers.yaml is missing; example answers are only used for local pages"
            )
        directory = self.settings.screenshots_dir / application_id
        if not is_within(self.settings.screenshots_dir, directory):
            raise FormNotAllowedError("Invalid screenshot location")

        # The browser runs before any database write so no write lock is held meanwhile.
        result = self.worker.run(
            fill_task(
                FillRequest(
                    application_id=application_id,
                    url=url,
                    portal=portal,
                    answers=config.answers,
                    matcher=SensitiveMatcher(config.sensitive),
                    threshold=config.portals.field_mapping_confidence_threshold,
                    resume_path=resume,
                    screenshot_dir=directory,
                )
            )
        )
        return self._record(application, portal.name, url, result, directory)

    def _record(
        self,
        application: ApplicationRecord,
        portal: str,
        url: str,
        result: FillResult,
        directory: Path,
    ) -> FormRunOutcome:
        blocked = bool(result.blockers)
        outcome = OUTCOME_BLOCKED if blocked else OUTCOME_READY
        screenshots = [
            relative_path(self.settings, directory / name) for name in result.screenshots
        ]
        record = FormRunRecord(
            application_id=application.application_id,
            portal=portal,
            url=redact_url(url),
            outcome=outcome,
            blockers=[b.model_dump() for b in result.blockers],
            fields=[f.model_dump(mode="json") for f in result.fields],
            screenshots=screenshots,
        )
        self.session.add(record)
        application.portal = portal
        application.screenshot_directory = relative_path(self.settings, directory)
        self.applications.set_status(
            application.application_id, ApplicationStatus.FORM_STARTED, "form opened"
        )
        if blocked:
            kinds = ", ".join(sorted({b.kind for b in result.blockers}))
            application.failure_reason = f"Manual action required ({kinds}); nothing was filled"
            self.applications.set_status(
                application.application_id, ApplicationStatus.FAILED, application.failure_reason
            )
        else:
            application.failure_reason = None
            self.applications.set_status(
                application.application_id,
                ApplicationStatus.AWAITING_APPROVAL,
                "form filled for review; nothing submitted",
            )
        self.session.flush()
        self.log.info(
            "form prepared",
            extra={
                "operation": "prepare_form",
                "application_id": application.application_id,
                "outcome": outcome,
            },
        )
        return self._outcome(application, record, live=result.session_kept)

    def _outcome(
        self, application: ApplicationRecord, record: FormRunRecord, *, live: bool
    ) -> FormRunOutcome:
        fields = [FieldReport.model_validate(f) for f in record.fields]

        def count(status: FieldStatus) -> int:
            return sum(f.status is status for f in fields)

        return FormRunOutcome(
            application_id=application.application_id,
            application_status=application.application_status,
            outcome=record.outcome,
            portal=record.portal,
            url=record.url,
            live_session=live,
            blockers=record.blockers,
            screenshots=record.screenshots,
            filled=count(FieldStatus.FILLED),
            uploaded=count(FieldStatus.UPLOADED),
            left_blank=sum(f.status.value.startswith("LEFT_BLANK") for f in fields),
            failed=count(FieldStatus.FILL_FAILED),
            manual_fields=[f for f in fields if _needs_person(f)],
            fields=fields,
            created_at=record.created_at,
        )

    def latest(self, application_id: str) -> FormRunOutcome:
        application = self._application(application_id)
        record = self.session.scalars(
            select(FormRunRecord)
            .where(FormRunRecord.application_id == application_id)
            .order_by(FormRunRecord.id.desc())
            .limit(1)
        ).first()
        if record is None:
            raise NotFoundError(f"No form run for {application_id}")
        return self._outcome(application, record, live=self.worker.has_session(application_id))

    def close(self, application_id: str) -> bool:
        self._application(application_id)
        return self.worker.close_session(application_id)
