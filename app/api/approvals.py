"""Approval endpoints. Only a person should call these; no n8n workflow does."""

from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.browser import WorkerDep
from app.automation.worker import BrowserTimeoutError, BrowserUnavailableError
from app.database.repositories import InvalidTransitionError, NotFoundError
from app.dependencies import ApiKeyDep, SessionDep, SettingsDep
from app.services.approval_service import (
    ApprovalChallenge,
    ApprovalNotAllowedError,
    ApprovalRejectedError,
    ApprovalService,
    ApprovalStatus,
    SubmissionInProgressError,
    SubmissionOutcome,
)
from app.services.config_loader import ConfigError

router = APIRouter(prefix="/applications", tags=["approval"], dependencies=[ApiKeyDep])


class ConfirmRequest(BaseModel):
    approval_token: str = Field(min_length=20, max_length=200)
    approval_text: str = Field(max_length=200)


class CancelResponse(BaseModel):
    application_id: str
    cancelled: int


def _raise(exc: Exception) -> NoReturn:
    if isinstance(exc, NotFoundError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found") from exc
    if isinstance(exc, ApprovalNotAllowedError):
        detail = {"message": "Approval cannot proceed", "reasons": exc.reasons}
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from exc
    if isinstance(exc, ApprovalRejectedError):
        detail = {"message": str(exc), "code": exc.code}
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail) from exc
    if isinstance(exc, SubmissionInProgressError | InvalidTransitionError):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if isinstance(exc, BrowserTimeoutError):
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, str(exc)) from exc
    if isinstance(exc, BrowserUnavailableError | ConfigError):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    raise exc


_HANDLED = (
    NotFoundError,
    ApprovalNotAllowedError,
    ApprovalRejectedError,
    SubmissionInProgressError,
    InvalidTransitionError,
    BrowserTimeoutError,
    BrowserUnavailableError,
    ConfigError,
)


@router.post("/{application_id}/approval/request", response_model=ApprovalChallenge)
def request_approval(
    application_id: str, session: SessionDep, settings: SettingsDep, worker: WorkerDep
) -> ApprovalChallenge:
    """Re-check the live review page and issue a single-use, time-limited approval token."""
    try:
        return ApprovalService(session, settings, worker).request(application_id)
    except _HANDLED as exc:
        _raise(exc)


@router.post("/{application_id}/approval/confirm", response_model=SubmissionOutcome)
def confirm_approval(
    application_id: str,
    body: ConfirmRequest,
    session: SessionDep,
    settings: SettingsDep,
    worker: WorkerDep,
) -> SubmissionOutcome:
    """Submit the reviewed form once, and only when the text is exactly SUBMIT <application_id>."""
    try:
        return ApprovalService(session, settings, worker).confirm(
            application_id, body.approval_token, body.approval_text
        )
    except _HANDLED as exc:
        _raise(exc)


@router.post("/{application_id}/approval/cancel", response_model=CancelResponse)
def cancel_approval(
    application_id: str, session: SessionDep, settings: SettingsDep, worker: WorkerDep
) -> CancelResponse:
    try:
        cancelled = ApprovalService(session, settings, worker).cancel(application_id)
    except _HANDLED as exc:
        _raise(exc)
    return CancelResponse(application_id=application_id, cancelled=cancelled)


@router.get("/{application_id}/approval", response_model=ApprovalStatus)
def approval_status(
    application_id: str, session: SessionDep, settings: SettingsDep, worker: WorkerDep
) -> ApprovalStatus:
    """Pending state and recent approval attempts. Tokens are never returned."""
    try:
        return ApprovalService(session, settings, worker).status(application_id)
    except _HANDLED as exc:
        _raise(exc)
