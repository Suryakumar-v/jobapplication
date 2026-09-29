"""Optional AI endpoints. Advisory output only; no workflow depends on them."""

from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, HTTPException, status

from app.ai.providers import AIProviderError, AITimeoutError
from app.dependencies import ApiKeyDep, SessionDep, SettingsDep
from app.services.ai_service import (
    AIDisabledError,
    AIInputError,
    AIJobAnalysis,
    AIJobAnalysisService,
    AIStatus,
    describe_status,
)
from app.services.config_loader import ConfigError
from app.services.matching_service import JobNotFoundError

router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[ApiKeyDep])


def _raise(exc: Exception) -> NoReturn:
    if isinstance(exc, JobNotFoundError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found") from exc
    if isinstance(exc, AIDisabledError):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if isinstance(exc, AIInputError):
        raise HTTPException(422, str(exc)) from exc
    if isinstance(exc, ConfigError):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if isinstance(exc, AITimeoutError):
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, str(exc)) from exc
    if isinstance(exc, AIProviderError):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    raise exc


@router.get("/status", response_model=AIStatus)
def ai_status(settings: SettingsDep) -> AIStatus:
    return describe_status(settings)


@router.post("/jobs/{job_id}/analyze", response_model=AIJobAnalysis)
def analyze_job(
    job_id: str, session: SessionDep, settings: SettingsDep, force: bool = False
) -> AIJobAnalysis:
    try:
        return AIJobAnalysisService(session, settings).analyze(job_id, force=force)
    except (
        JobNotFoundError,
        AIDisabledError,
        AIInputError,
        ConfigError,
        AIProviderError,
    ) as exc:
        _raise(exc)


@router.get("/jobs/{job_id}", response_model=AIJobAnalysis)
def get_analysis(job_id: str, session: SessionDep, settings: SettingsDep) -> AIJobAnalysis:
    found = AIJobAnalysisService(session, settings).latest(job_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No AI analysis stored for this job")
    return found
