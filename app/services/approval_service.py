"""Manual approval gate and the single guarded submission path.

A person asks for an approval (which re-checks the live review page), then confirms it by typing
exactly ``SUBMIT <application_id>``. The approval is stored only as a hash, expires after
APPROVAL_TOKEN_EXPIRY seconds, and is consumed atomically before the browser is touched, so it
can never be used twice. Nothing else in the system can submit an application.
"""

from __future__ import annotations

import secrets
import threading
from datetime import datetime, timedelta
from typing import NoReturn

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.automation.portals import select_portal
from app.automation.tasks import (
    SUBMIT_BLOCKED,
    SUBMIT_NOT_SENT,
    SUBMIT_SENT,
    check_problems,
    inspect_task,
    submit_task,
)
from app.automation.url_guard import hostname_of
from app.automation.worker import BrowserTimeoutError, BrowserUnavailableError, BrowserWorker
from app.config import LOOPBACK_HOSTS, Settings
from app.database.base import utcnow
from app.database.repositories import ApplicationRepository, NotFoundError
from app.database.tables import (
    ApplicationRecord,
    ApprovalAttemptRecord,
    ApprovalRecord,
    FormRunRecord,
)
from app.models.application import ApplicationStatus
from app.services.config_loader import load_form_config
from app.services.form_service import OUTCOME_READY, FormPreparationService, relative_path
from app.utils.hashing import sha256_text
from app.utils.logging_config import get_logger

MAX_TEXT_ATTEMPTS = 3
SUBMIT_TIMEOUT_SECONDS = 150.0
ATTEMPT_HISTORY = 20

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def required_text(application_id: str) -> str:
    return f"SUBMIT {application_id}"


class ApprovalNotAllowedError(ValueError):
    """The application is not in a state where approval or submission can proceed."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class ApprovalRejectedError(ValueError):
    """The approval token or text was wrong, expired or already used."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SubmissionInProgressError(RuntimeError):
    pass


class ReviewSummary(BaseModel):
    company: str
    job_title: str
    url: str
    portal: str
    filled: int
    uploaded: int
    left_blank: int
    manual_fields: list[str]
    screenshots: list[str]


class ApprovalChallenge(BaseModel):
    application_id: str
    required_text: str
    approval_token: str
    expires_at: datetime
    expires_in_seconds: int
    review: ReviewSummary


class SubmissionOutcome(BaseModel):
    application_id: str
    application_status: str
    submitted: bool
    result: str
    message: str
    http_status: int | None = None
    confirmation_number: str | None = None
    confirmation_text_found: bool = False
    screenshots: list[str] = []


class AttemptSummary(BaseModel):
    outcome: str
    reason: str | None
    created_at: datetime


class ApprovalStatus(BaseModel):
    application_id: str
    application_status: str
    pending: bool
    expires_at: datetime | None
    attempts: list[AttemptSummary]


def _lock_for(application_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(application_id, threading.Lock())


class ApprovalService:
    def __init__(self, session: Session, settings: Settings, worker: BrowserWorker) -> None:
        self.session = session
        self.settings = settings
        self.worker = worker
        self.applications = ApplicationRepository(session)
        self.log = get_logger("app.audit")

    # ---- helpers ------------------------------------------------------------------------

    def _application(self, application_id: str) -> ApplicationRecord:
        application = self.applications.get(application_id)
        if application is None:
            raise NotFoundError(application_id)
        return application

    def _attempt(self, application_id: str, outcome: str, reason: str | None = None) -> None:
        self.session.add(
            ApprovalAttemptRecord(
                application_id=application_id,
                outcome=outcome,
                reason=reason[:255] if reason else None,
            )
        )
        self.session.flush()
        self.log.info(
            "approval event",
            extra={"operation": "approval", "application_id": application_id, "outcome": outcome},
        )

    def _reject(self, application_id: str, outcome: str, code: str, message: str) -> NoReturn:
        self._attempt(application_id, outcome, code)
        self.session.commit()
        raise ApprovalRejectedError(code, message)

    def _unconsumed(self, application_id: str) -> list[ApprovalRecord]:
        return list(
            self.session.scalars(
                select(ApprovalRecord).where(
                    ApprovalRecord.application_id == application_id,
                    ApprovalRecord.consumed_at.is_(None),
                )
            )
        )

    def _latest_run(self, application_id: str) -> FormRunRecord | None:
        return self.session.scalars(
            select(FormRunRecord)
            .where(FormRunRecord.application_id == application_id)
            .order_by(FormRunRecord.id.desc())
            .limit(1)
        ).first()

    def _problems(self, application: ApplicationRecord) -> list[str]:
        """Everything that must be true right now, including the live state of the page."""
        violations = self.settings.safety_violations()
        if violations:
            return violations
        status = ApplicationStatus(application.application_status)
        if status is not ApplicationStatus.AWAITING_APPROVAL:
            return [f"Application is {status.value}; it must be AWAITING_APPROVAL"]
        if application.approved_at is not None:
            return ["A submission was already attempted for this application"]
        run = self._latest_run(application.application_id)
        if run is None or run.outcome != OUTCOME_READY:
            return ["No completed form preparation was found; start the form again"]
        config = load_form_config(self.settings)
        form_host = hostname_of(run.url)
        problems: list[str] = []
        if form_host not in LOOPBACK_HOSTS:
            portal_name = application.portal or "generic"
            portal_config = config.portals.portals.get(portal_name)
            if portal_config is None or not portal_config.validated:
                problems.append(
                    f"Portal '{portal_name}' is not marked validated in portal_settings.yaml; "
                    "submit this application yourself"
                )
        check = self.worker.run(
            inspect_task(application.application_id, select_portal(config.portals)), timeout=60
        )
        problems.extend(check_problems(check))
        if check.session_open and hostname_of(check.page_url) != form_host:
            problems.append("The review browser is no longer on the prepared form's site")
        return problems

    def _review(self, application: ApplicationRecord) -> ReviewSummary:
        form = FormPreparationService(self.session, self.settings, self.worker).latest(
            application.application_id
        )
        return ReviewSummary(
            company=application.company,
            job_title=application.job_title,
            url=form.url,
            portal=form.portal,
            filled=form.filled,
            uploaded=form.uploaded,
            left_blank=form.left_blank,
            manual_fields=[field.label for field in form.manual_fields],
            screenshots=form.screenshots,
        )

    # ---- request ------------------------------------------------------------------------

    def request(self, application_id: str) -> ApprovalChallenge:
        application = self._application(application_id)
        problems = self._problems(application)
        if problems:
            self._attempt(application_id, "REQUEST_REFUSED", "; ".join(problems))
            self.session.commit()
            raise ApprovalNotAllowedError(problems)
        now = utcnow()
        for earlier in self._unconsumed(application_id):
            earlier.consumed_at = now
            self._attempt(application_id, "SUPERSEDED")
        token = secrets.token_urlsafe(32)
        expiry = self.settings.approval_token_expiry
        expires_at = now + timedelta(seconds=expiry)
        self.session.add(
            ApprovalRecord(
                application_id=application_id,
                token_hash=sha256_text(token),
                created_at=now,
                expires_at=expires_at,
            )
        )
        self._attempt(application_id, "REQUESTED")
        review = self._review(application)
        self.session.commit()
        return ApprovalChallenge(
            application_id=application_id,
            required_text=required_text(application_id),
            approval_token=token,
            expires_at=expires_at,
            expires_in_seconds=expiry,
            review=review,
        )

    # ---- confirm ------------------------------------------------------------------------

    def confirm(self, application_id: str, token: str, text: str) -> SubmissionOutcome:
        lock = _lock_for(application_id)
        if not lock.acquire(blocking=False):
            raise SubmissionInProgressError("A submission for this application is in progress")
        try:
            return self._confirm(application_id, token, text)
        finally:
            lock.release()

    def _confirm(self, application_id: str, token: str, text: str) -> SubmissionOutcome:
        application = self._application(application_id)
        approval = self.session.scalar(
            select(ApprovalRecord).where(ApprovalRecord.token_hash == sha256_text(token))
        )
        if approval is None or approval.application_id != application_id:
            self._reject(
                application_id, "REJECTED_UNKNOWN", "unknown_approval", "Approval is not valid"
            )
        now = utcnow()
        if approval.consumed_at is not None:
            self._reject(application_id, "REJECTED_REPLAY", "already_used", "Approval already used")
        if approval.expires_at <= now:
            approval.consumed_at = now
            self._reject(application_id, "REJECTED_EXPIRED", "expired", "Approval has expired")
        if text != required_text(application_id):
            failures = 1 + (
                self.session.scalar(
                    select(func.count())
                    .select_from(ApprovalAttemptRecord)
                    .where(
                        ApprovalAttemptRecord.application_id == application_id,
                        ApprovalAttemptRecord.outcome == "REJECTED_TEXT",
                        ApprovalAttemptRecord.created_at >= approval.created_at,
                    )
                )
                or 0
            )
            if failures >= MAX_TEXT_ATTEMPTS:
                approval.consumed_at = now
            self._reject(
                application_id,
                "REJECTED_TEXT",
                "text_mismatch",
                "The approval text must be exactly: " + required_text(application_id),
            )

        problems = self._problems(application)
        if problems:
            self._attempt(application_id, "CONFIRM_BLOCKED", "; ".join(problems))
            self.session.commit()
            raise ApprovalNotAllowedError(problems)

        # Single use: consume before the browser is touched, so a failure cannot be replayed.
        won = self.session.execute(
            update(ApprovalRecord)
            .where(ApprovalRecord.id == approval.id, ApprovalRecord.consumed_at.is_(None))
            .values(consumed_at=now)
            .returning(ApprovalRecord.id)
        ).first()
        if won is None:
            self._reject(application_id, "REJECTED_REPLAY", "already_used", "Approval already used")
        application.approved_at = now
        self._attempt(application_id, "APPROVED")
        self.session.commit()
        return self._submit(application)

    def _submit(self, application: ApplicationRecord) -> SubmissionOutcome:
        application_id = application.application_id
        portal = select_portal(load_form_config(self.settings).portals)
        directory = self.settings.screenshots_dir / application_id
        try:
            result = self.worker.run(
                submit_task(application_id, portal, directory), timeout=SUBMIT_TIMEOUT_SECONDS
            )
        except BrowserTimeoutError:
            return self._failed(
                application,
                "UNKNOWN",
                "Submission outcome unknown: the browser timed out. Check the portal before "
                "trying again.",
            )
        except BrowserUnavailableError:
            application.approved_at = None  # nothing was sent, so a new approval may be requested
            self._attempt(application_id, "SUBMIT_ERROR", "browser unavailable")
            self.session.commit()
            raise

        shots = [relative_path(self.settings, directory / name) for name in result.screenshots]
        if result.outcome == SUBMIT_SENT:
            note = (
                f"Submit request returned HTTP {result.http_status}; confirmation text "
                f"{'found' if result.confirmation_text_found else 'not found'}."
            )
            application.confirmation_number = result.confirmation_number
            application.failure_reason = None
            application.notes = f"{application.notes}\n{note}" if application.notes else note
            self.applications.set_status(
                application_id, ApplicationStatus.SUBMITTED, "submitted after manual approval"
            )
            self._attempt(application_id, "SUBMITTED", f"HTTP {result.http_status}")
            self.session.commit()
            return SubmissionOutcome(
                application_id=application_id,
                application_status=application.application_status,
                submitted=True,
                result=result.outcome,
                message=note,
                http_status=result.http_status,
                confirmation_number=result.confirmation_number,
                confirmation_text_found=result.confirmation_text_found,
                screenshots=shots,
            )
        if result.outcome in (SUBMIT_BLOCKED, SUBMIT_NOT_SENT):
            reason = (
                "; ".join(check_problems(result.check))
                if result.outcome == SUBMIT_BLOCKED
                else "The submit button was clicked but no request was sent"
            )
            self._attempt(application_id, f"SUBMIT_{result.outcome}", reason)
            application.approved_at = None  # no request was sent, so a new approval is allowed
            self.session.commit()
            return SubmissionOutcome(
                application_id=application_id,
                application_status=application.application_status,
                submitted=False,
                result=result.outcome,
                message=f"Nothing was submitted. {reason}. Request a new approval to try again.",
                screenshots=shots,
            )
        status = f"HTTP {result.http_status}" if result.http_status else "no response"
        return self._failed(
            application,
            result.outcome,
            f"The submit request was sent but the portal answered with {status}. Check the portal "
            "before trying again.",
            http_status=result.http_status,
            screenshots=shots,
        )

    def _failed(
        self,
        application: ApplicationRecord,
        result: str,
        message: str,
        *,
        http_status: int | None = None,
        screenshots: list[str] | None = None,
    ) -> SubmissionOutcome:
        application.failure_reason = message
        self.applications.set_status(application.application_id, ApplicationStatus.FAILED, message)
        self._attempt(application.application_id, "SUBMIT_FAILED", message)
        self.session.commit()
        return SubmissionOutcome(
            application_id=application.application_id,
            application_status=application.application_status,
            submitted=False,
            result=result,
            message=message,
            http_status=http_status,
            screenshots=screenshots or [],
        )

    # ---- cancel and status --------------------------------------------------------------

    def cancel(self, application_id: str) -> int:
        self._application(application_id)
        now = utcnow()
        pending = self._unconsumed(application_id)
        for approval in pending:
            approval.consumed_at = now
        if pending:
            self._attempt(application_id, "CANCELLED")
        self.session.commit()
        return len(pending)

    def status(self, application_id: str) -> ApprovalStatus:
        application = self._application(application_id)
        now = utcnow()
        live = [a for a in self._unconsumed(application_id) if a.expires_at > now]
        attempts = self.session.scalars(
            select(ApprovalAttemptRecord)
            .where(ApprovalAttemptRecord.application_id == application_id)
            .order_by(ApprovalAttemptRecord.id.desc())
            .limit(ATTEMPT_HISTORY)
        )
        return ApprovalStatus(
            application_id=application_id,
            application_status=application.application_status,
            pending=bool(live),
            expires_at=max((a.expires_at for a in live), default=None),
            attempts=[
                AttemptSummary(outcome=a.outcome, reason=a.reason, created_at=a.created_at)
                for a in attempts
            ],
        )
