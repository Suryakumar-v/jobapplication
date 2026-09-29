"""Browser-backed endpoints: job extraction and form preparation for review."""

from __future__ import annotations

from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.automation.url_guard import UrlNotAllowedError
from app.automation.worker import (
    BrowserTimeoutError,
    BrowserUnavailableError,
    BrowserWorker,
    SessionLimitError,
)
from app.database.repositories import InvalidTransitionError, NotFoundError
from app.dependencies import ApiKeyDep, SessionDep, SettingsDep
from app.services.config_loader import ConfigError
from app.services.form_service import FormNotAllowedError, FormPreparationService, FormRunOutcome
from app.services.job_extraction_service import (
    ExtractionOutcome,
    JobExtractionService,
    ManualActionRequiredError,
)

jobs_router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[ApiKeyDep])
applications_router = APIRouter(
    prefix="/applications", tags=["applications"], dependencies=[ApiKeyDep]
)


def get_worker(request: Request) -> BrowserWorker:
    worker: BrowserWorker = request.app.state.browser
    return worker


WorkerDep = Annotated[BrowserWorker, Depends(get_worker)]


class ExtractRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    company: str | None = Field(default=None, max_length=255)
    title: str | None = Field(default=None, max_length=255)


class StartFormRequest(BaseModel):
    """Omit ``application_url`` to open the job's own URL."""

    application_url: str | None = Field(default=None, max_length=2048)


class CloseResponse(BaseModel):
    application_id: str
    closed: bool


def _raise_browser_error(exc: Exception) -> NoReturn:
    if isinstance(exc, UrlNotAllowedError):
        raise HTTPException(422, str(exc)) from exc
    if isinstance(exc, ManualActionRequiredError):
        detail = {
            "message": str(exc),
            "manual_action_required": [b.model_dump() for b in exc.blockers],
        }
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from exc
    if isinstance(exc, FormNotAllowedError | SessionLimitError | InvalidTransitionError):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if isinstance(exc, NotFoundError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from exc
    if isinstance(exc, BrowserTimeoutError):
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, str(exc)) from exc
    if isinstance(exc, BrowserUnavailableError | ConfigError):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    raise exc


@jobs_router.post("/extract", response_model=ExtractionOutcome)
def extract_job(
    body: ExtractRequest, session: SessionDep, settings: SettingsDep, worker: WorkerDep
) -> ExtractionOutcome:
    """Open a job page read-only, extract the posting and store it like any imported job."""
    try:
        outcome = JobExtractionService(session, settings, worker).extract(
            body.url, company=body.company, title=body.title
        )
        session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Concurrent import conflict; retry the request"
        ) from exc
    except (
        UrlNotAllowedError,
        ManualActionRequiredError,
        BrowserTimeoutError,
        BrowserUnavailableError,
        ConfigError,
    ) as exc:
        _raise_browser_error(exc)
    return outcome


@applications_router.post("/{application_id}/form/start", response_model=FormRunOutcome)
def start_form(
    application_id: str,
    body: StartFormRequest,
    session: SessionDep,
    settings: SettingsDep,
    worker: WorkerDep,
) -> FormRunOutcome:
    """Open the form, fill approved safe fields, attach the tailored resume. Never submits."""
    try:
        return FormPreparationService(session, settings, worker).start(
            application_id, body.application_url
        )
    except (
        UrlNotAllowedError,
        FormNotAllowedError,
        SessionLimitError,
        InvalidTransitionError,
        NotFoundError,
        BrowserTimeoutError,
        BrowserUnavailableError,
        ConfigError,
    ) as exc:
        _raise_browser_error(exc)


@applications_router.get("/{application_id}/form", response_model=FormRunOutcome)
def get_form(
    application_id: str, session: SessionDep, settings: SettingsDep, worker: WorkerDep
) -> FormRunOutcome:
    try:
        return FormPreparationService(session, settings, worker).latest(application_id)
    except (NotFoundError, BrowserTimeoutError, BrowserUnavailableError) as exc:
        _raise_browser_error(exc)


@applications_router.post("/{application_id}/form/close", response_model=CloseResponse)
def close_form(
    application_id: str, session: SessionDep, settings: SettingsDep, worker: WorkerDep
) -> CloseResponse:
    """Close the review browser. The application status is not changed."""
    try:
        closed = FormPreparationService(session, settings, worker).close(application_id)
    except (NotFoundError, BrowserTimeoutError) as exc:
        _raise_browser_error(exc)
    return CloseResponse(application_id=application_id, closed=closed)
