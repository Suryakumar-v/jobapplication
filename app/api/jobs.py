"""Job ingestion endpoints: import, duplicate check, lookup."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.database.repositories import JobRepository
from app.database.tables import JobRecord
from app.dependencies import ApiKeyDep, SessionDep, SettingsDep
from app.models.job import JobSource
from app.services.job_importer import (
    ImportFormatError,
    IngestionSummary,
    JobIngestionService,
    parse_csv_text,
    parse_job_text,
    parse_json_payload,
)

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[ApiKeyDep])

MAX_JSON_JOBS = 1000
MAX_CSV_CHARS = 2_000_000
MAX_TEXT_CHARS = 200_000


class ImportResponse(IngestionSummary):
    success: bool = True
    operation: str


class UrlImportRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    company: str | None = Field(default=None, max_length=255)
    title: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    location: str | None = Field(default=None, max_length=255)


class TextImportRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    company: str | None = Field(default=None, max_length=255)
    title: str | None = Field(default=None, max_length=255)
    job_url: str | None = Field(default=None, max_length=2048)
    location: str | None = Field(default=None, max_length=255)


class CsvImportRequest(BaseModel):
    csv_text: str = Field(min_length=1, max_length=MAX_CSV_CHARS)


class JobSummary(BaseModel):
    job_id: str
    fingerprint: str
    company: str
    title: str
    location: str | None
    employment_type: str | None
    workplace_type: str | None
    source: str
    job_url: str | None
    status: str
    match_score: float | None
    recommendation: str | None
    description_length: int


JsonBody = Annotated[dict[str, Any] | list[dict[str, Any]], Body()]


def _respond(operation: str, summary: IngestionSummary) -> ImportResponse:
    return ImportResponse(operation=operation, **summary.model_dump())


def _ingest(
    session: SessionDep,
    settings: SettingsDep,
    operation: str,
    source: JobSource,
    records: list[dict[str, Any]],
) -> ImportResponse:
    try:
        summary = JobIngestionService(session, settings).ingest_records(records, source)
        session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Concurrent import conflict; retry the request"
        ) from exc
    return _respond(operation, summary)


def _format_error(exc: ImportFormatError) -> HTTPException:
    return HTTPException(422, str(exc))


@router.post("/import/url", response_model=ImportResponse)
def import_url(
    body: UrlImportRequest, session: SessionDep, settings: SettingsDep
) -> ImportResponse:
    """Manual URL. Company and title are required until browser extraction exists."""
    record = body.model_dump(exclude_none=True)
    record["job_url"] = record.pop("url")
    return _ingest(session, settings, "import_url", JobSource.MANUAL_URL, [record])


@router.post("/import/text", response_model=ImportResponse)
def import_text(
    body: TextImportRequest, session: SessionDep, settings: SettingsDep
) -> ImportResponse:
    record = parse_job_text(
        body.text,
        company=body.company,
        title=body.title,
        job_url=body.job_url,
        location=body.location,
    )
    return _ingest(session, settings, "import_text", JobSource.TEXT, [record])


@router.post("/import/json", response_model=ImportResponse)
def import_json(body: JsonBody, session: SessionDep, settings: SettingsDep) -> ImportResponse:
    try:
        records = parse_json_payload(body)
    except ImportFormatError as exc:
        raise _format_error(exc) from exc
    if len(records) > MAX_JSON_JOBS:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"Max {MAX_JSON_JOBS} jobs")
    return _ingest(session, settings, "import_json", JobSource.JSON, records)


@router.post("/import/csv", response_model=ImportResponse)
def import_csv(
    body: CsvImportRequest, session: SessionDep, settings: SettingsDep
) -> ImportResponse:
    try:
        records = parse_csv_text(body.csv_text)
    except ImportFormatError as exc:
        raise _format_error(exc) from exc
    return _ingest(session, settings, "import_csv", JobSource.CSV, records)


@router.post("/webhook", response_model=ImportResponse)
def webhook(body: JsonBody, session: SessionDep, settings: SettingsDep) -> ImportResponse:
    """Same payload shapes as /import/json, tagged as a webhook submission."""
    try:
        records = parse_json_payload(body)
    except ImportFormatError as exc:
        raise _format_error(exc) from exc
    if len(records) > MAX_JSON_JOBS:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"Max {MAX_JSON_JOBS} jobs")
    return _ingest(session, settings, "webhook", JobSource.WEBHOOK, records)


@router.post("/check")
def check_job(
    body: Annotated[dict[str, Any], Body()], session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Validate, fingerprint and duplicate-check one job without storing it."""
    outcome = JobIngestionService(session, settings).check(body, JobSource.JSON)
    return {"success": outcome.valid, "operation": "check_job", **outcome.model_dump()}


@router.get("", response_model=list[JobSummary])
def list_jobs(
    session: SessionDep,
    job_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[JobSummary]:
    return [_summary(job) for job in JobRepository(session).list_jobs(job_status, limit, offset)]


@router.get("/{job_id}", response_model=JobSummary)
def get_job(job_id: str, session: SessionDep) -> JobSummary:
    job = JobRepository(session).get_by_job_id(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return _summary(job)


def _summary(job: JobRecord) -> JobSummary:
    return JobSummary(
        job_id=job.job_id,
        fingerprint=job.fingerprint,
        company=job.company,
        title=job.title,
        location=job.location,
        employment_type=job.employment_type,
        workplace_type=job.workplace_type,
        source=job.source,
        job_url=job.job_url,
        status=job.status,
        match_score=job.match_score,
        recommendation=job.recommendation,
        description_length=len(job.description or ""),
    )
