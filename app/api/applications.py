"""Read-only application endpoints used by the workflows and the tracker."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from app.database.repositories import ApplicationRepository
from app.database.tables import ApplicationRecord
from app.dependencies import ApiKeyDep, SessionDep

router = APIRouter(prefix="/applications", tags=["applications"], dependencies=[ApiKeyDep])


class ApplicationSummary(BaseModel):
    application_id: str
    job_id: str
    company: str
    job_title: str
    location: str | None
    portal: str | None
    job_url: str | None
    match_score: float | None
    recommendation: str | None
    application_status: str
    has_tailored_resume: bool
    failure_reason: str | None
    awaiting_approval_at: datetime | None
    updated_at: datetime


def _summary(record: ApplicationRecord) -> ApplicationSummary:
    return ApplicationSummary(
        application_id=record.application_id,
        job_id=record.job_id,
        company=record.company,
        job_title=record.job_title,
        location=record.location,
        portal=record.portal,
        job_url=record.job_url,
        match_score=record.match_score,
        recommendation=record.recommendation,
        application_status=record.application_status,
        has_tailored_resume=bool(record.tailored_resume_path),
        failure_reason=record.failure_reason,
        awaiting_approval_at=record.awaiting_approval_at,
        updated_at=record.updated_at,
    )


@router.get("", response_model=list[ApplicationSummary])
def list_applications(
    session: SessionDep,
    application_status: Annotated[str | None, Query(alias="status", max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ApplicationSummary]:
    query = select(ApplicationRecord).order_by(ApplicationRecord.id).limit(limit).offset(offset)
    if application_status is not None:
        query = query.where(ApplicationRecord.application_status == application_status)
    return [_summary(record) for record in session.scalars(query)]


@router.get("/{application_id}", response_model=ApplicationSummary)
def get_application(application_id: str, session: SessionDep) -> ApplicationSummary:
    record = ApplicationRepository(session).get(application_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    return _summary(record)
