"""Resume endpoints: generate a truthful, validated resume for an analysed job."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.database.repositories import NotFoundError
from app.dependencies import ApiKeyDep, SessionDep, SettingsDep
from app.services.config_loader import ConfigError
from app.services.matching_service import JobNotFoundError
from app.services.resume_service import (
    ResumeTailoringService,
    TailoredResumeSummary,
    TailoringNotAllowedError,
    TailoringOutcome,
    ValidationFailedError,
)
from app.services.resume_tailor import MasterResumeError

router = APIRouter(prefix="/resumes", tags=["resumes"], dependencies=[ApiKeyDep])


@router.post("/tailor/{job_id}", response_model=TailoringOutcome)
def tailor_resume(job_id: str, session: SessionDep, settings: SettingsDep) -> TailoringOutcome:
    """Create a tailored .docx for an ANALYZED job; the master resume is never modified."""
    try:
        return ResumeTailoringService(session, settings).tailor(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found") from exc
    except TailoringNotAllowedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except MasterResumeError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValidationFailedError as exc:
        detail: dict[str, Any] = {
            "message": str(exc),
            "validation": exc.validation.model_dump(mode="json"),
        }
        raise HTTPException(422, detail) from exc
    except ConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


@router.get("/{application_id}", response_model=list[TailoredResumeSummary])
def resume_history(
    application_id: str, session: SessionDep, settings: SettingsDep
) -> list[TailoredResumeSummary]:
    try:
        return ResumeTailoringService(session, settings).history(application_id)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found") from exc
