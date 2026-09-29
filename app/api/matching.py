"""Matching endpoints: score stored jobs against the candidate profile."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.database.repositories import JobRepository
from app.dependencies import ApiKeyDep, SessionDep, SettingsDep
from app.services.config_loader import ConfigError
from app.services.matching_service import (
    AnalysisNotAllowedError,
    AnalysisOutcome,
    BatchAnalysis,
    JobMatchingService,
    JobNotFoundError,
)

router = APIRouter(prefix="/matching", tags=["matching"], dependencies=[ApiKeyDep])


class AnalyzeRequest(BaseModel):
    """Omit ``job_ids`` to analyse jobs still in DISCOVERED status."""

    job_ids: Annotated[list[str] | None, Field(max_length=1000)] = None
    limit: int = Field(default=50, ge=1, le=1000)


def _config_unavailable(exc: ConfigError) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))


@router.post("/analyze", response_model=BatchAnalysis)
def analyze_jobs(body: AnalyzeRequest, session: SessionDep, settings: SettingsDep) -> BatchAnalysis:
    try:
        return JobMatchingService(session, settings).analyze_batch(body.job_ids, body.limit)
    except ConfigError as exc:
        raise _config_unavailable(exc) from exc


@router.post("/analyze/{job_id}", response_model=AnalysisOutcome)
def analyze_job(job_id: str, session: SessionDep, settings: SettingsDep) -> AnalysisOutcome:
    try:
        return JobMatchingService(session, settings).analyze_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found") from exc
    except AnalysisNotAllowedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ConfigError as exc:
        raise _config_unavailable(exc) from exc


@router.get("/{job_id}")
def get_match(job_id: str, session: SessionDep) -> dict[str, Any]:
    """Return the stored result of the last analysis."""
    job = JobRepository(session).get_by_job_id(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    if job.matching_details is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job has not been analysed")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "analyzed_at": job.analyzed_at,
        "result": job.matching_details,
    }
