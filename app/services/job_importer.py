"""Parse job inputs (JSON, CSV, pasted text, URL) and ingest them into the tracker."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.config import Settings
from app.database.repositories import JobRepository
from app.database.tables import JobRecord
from app.models.application import ApplicationStatus
from app.models.job import JobPosting, JobSource, WorkplaceType
from app.services.duplicate_detector import DuplicateDetector, DuplicateReason
from app.services.job_normalizer import NormalizedJob, normalize_job, normalize_url
from app.utils.logging_config import get_logger

MAX_CSV_ROWS = 5000
MAX_CSV_FIELD_CHARS = 200_000
MAX_TEXT_HEADER_LINES = 20

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "company": (
        "company",
        "company_name",
        "employer",
        "organization",
        "organisation",
        "hiring_organization",
    ),
    "title": ("title", "job_title", "jobtitle", "position", "role", "job_name"),
    "description": ("description", "job_description", "details", "summary", "content", "body"),
    "job_url": ("job_url", "url", "link", "apply_url", "application_url", "posting_url"),
    "external_id": ("external_id", "job_id", "id", "requisition_id", "req_id", "posting_id"),
    "location": ("location", "job_location", "city", "office"),
    "employment_type": ("employment_type", "job_type", "type", "contract_type", "schedule"),
    "workplace_type": ("workplace_type", "work_type", "work_model", "remote_type", "remote"),
    "salary_min": ("salary_min", "min_salary", "minimum_salary", "salary_from"),
    "salary_max": ("salary_max", "max_salary", "maximum_salary", "salary_to"),
    "posted_date": ("posted_date", "date_posted", "posted", "published", "published_at", "date"),
}

_TEXT_HEADERS: dict[str, tuple[str, ...]] = {
    "title": ("job title", "title", "position", "role"),
    "company": ("company", "employer", "organization", "organisation"),
    "location": ("location",),
    "employment_type": ("employment type", "job type"),
    "job_url": ("url", "job url", "apply url", "link"),
}
_TRUE = {"true", "yes", "y", "1"}
_SALARY = re.compile(r"(\d[\d,.]*)\s*([kK])?")


class ImportFormatError(ValueError):
    """The submitted payload is not parseable in the declared format."""


class ItemStatus(StrEnum):
    CREATED = "CREATED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"
    NEEDS_EXTRACTION = "NEEDS_EXTRACTION"
    DEFERRED = "DEFERRED"


class ItemResult(BaseModel):
    index: int
    status: ItemStatus
    job_id: str | None = None
    fingerprint: str | None = None
    company: str | None = None
    title: str | None = None
    duplicate_reason: DuplicateReason | None = None
    duplicate_of_job_id: str | None = None
    existing_application_id: str | None = None
    errors: list[str] = []


class IngestionSummary(BaseModel):
    received: int
    created: int
    duplicates: int
    rejected: int
    needs_extraction: int
    deferred: int
    results: list[ItemResult]


class CheckOutcome(BaseModel):
    valid: bool
    errors: list[str] = []
    fingerprint: str | None = None
    job_id: str | None = None
    is_duplicate: bool = False
    duplicate_reason: DuplicateReason | None = None
    duplicate_of_job_id: str | None = None
    existing_application_id: str | None = None


def _clean_str(value: Any) -> str | None:
    if value is None or (isinstance(value, Mapping | Sequence) and not isinstance(value, str)):
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    text = str(value).strip()
    return text or None


def _parse_salary(value: Any) -> int | None:
    text = _clean_str(value)
    if text is None:
        return None
    match = _SALARY.search(text.replace(" ", ""))
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(number * 1000) if match.group(2) else int(number)


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _clean_str(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_workplace(value: Any) -> WorkplaceType:
    if isinstance(value, bool):
        return WorkplaceType.REMOTE if value else WorkplaceType.UNKNOWN
    text = _clean_str(value)
    if text is None:
        return WorkplaceType.UNKNOWN
    lowered = text.lower().replace("-", "").replace(" ", "")
    if lowered in _TRUE or lowered == "remote":
        return WorkplaceType.REMOTE
    if lowered == "hybrid":
        return WorkplaceType.HYBRID
    if lowered in {"onsite", "office", "inoffice"}:
        return WorkplaceType.ONSITE
    return WorkplaceType.UNKNOWN


def canonicalize_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Map arbitrary source field names to the canonical job fields; unknown fields are dropped."""
    lowered = {
        re.sub(r"[\s\-]+", "_", str(key).strip().lower()): value for key, value in raw.items()
    }
    record: dict[str, Any] = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            value = lowered.get(alias)
            if isinstance(value, bool) and field == "workplace_type":
                record[field] = value
                break
            if _clean_str(value) is not None:
                record[field] = value
                break
    return record


def parse_json_payload(data: Any) -> list[dict[str, Any]]:
    """Accept one job object, a list of jobs, or {"jobs": [...]}."""
    if isinstance(data, Mapping) and isinstance(data.get("jobs"), list):
        data = data["jobs"]
    if isinstance(data, Mapping):
        data = [data]
    if not isinstance(data, list) or not all(isinstance(item, Mapping) for item in data):
        raise ImportFormatError("Expected a job object, a list of job objects, or {'jobs': [...]}")
    return [dict(item) for item in data]


def parse_csv_text(text: str, limit: int = MAX_CSV_ROWS) -> list[dict[str, Any]]:
    csv.field_size_limit(MAX_CSV_FIELD_CHARS)
    try:
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        if not reader.fieldnames:
            raise ImportFormatError("CSV has no header row")
        rows: list[dict[str, Any]] = []
        for row in reader:
            rows.append({k: v for k, v in row.items() if k is not None})
            if len(rows) >= limit:
                break
    except csv.Error as exc:
        raise ImportFormatError(f"Invalid CSV: {exc}") from exc
    return rows


def parse_job_text(
    text: str,
    *,
    company: str | None = None,
    title: str | None = None,
    job_url: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    """Build a record from pasted text; explicit arguments override 'Title: ...' style headers."""
    found: dict[str, str] = {}
    for line in text.splitlines()[:MAX_TEXT_HEADER_LINES]:
        for field, labels in _TEXT_HEADERS.items():
            if field in found:
                continue
            match = re.match(rf"^\s*(?:{'|'.join(labels)})\s*[:\-]\s*(.+?)\s*$", line, re.I)
            if match:
                found[field] = match.group(1)
    record: dict[str, Any] = {**found, "description": text}
    for field, value in (
        ("company", company),
        ("title", title),
        ("job_url", job_url),
        ("location", location),
    ):
        if value and value.strip():
            record[field] = value.strip()
    return record


def _validation_messages(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc']) or 'record'}: {e['msg']}" for e in exc.errors()]


def build_posting(
    record: Mapping[str, Any], source: JobSource
) -> tuple[JobPosting | None, ItemStatus | None, list[str]]:
    """Return (posting, None, []) on success, else (None, REJECTED|NEEDS_EXTRACTION, errors)."""
    canonical = canonicalize_record(record)
    errors: list[str] = []
    missing = [f for f in ("company", "title") if not _clean_str(canonical.get(f))]
    url = _clean_str(canonical.get("job_url"))
    if url is not None and normalize_url(url) is None:
        errors.append("job_url: must be a valid http(s) URL")
    if source is JobSource.MANUAL_URL and missing and url and not errors:
        return (
            None,
            ItemStatus.NEEDS_EXTRACTION,
            [
                f"Missing {', '.join(missing)}; supply them or use browser extraction "
                "(POST /jobs/extract)."
            ],
        )
    if missing:
        errors.append(f"Missing required field(s): {', '.join(missing)}")
    if url is None and not _clean_str(canonical.get("description")):
        errors.append("Provide at least one of job_url or description")
    if errors:
        return None, ItemStatus.REJECTED, errors
    try:
        posting = JobPosting(
            company=_clean_str(canonical["company"]) or "",
            title=_clean_str(canonical["title"]) or "",
            description=_clean_str(canonical.get("description")) or "",
            job_url=url,
            external_id=_clean_str(canonical.get("external_id")),
            location=_clean_str(canonical.get("location")),
            employment_type=_clean_str(canonical.get("employment_type")),
            workplace_type=_parse_workplace(canonical.get("workplace_type")),
            salary_min=_parse_salary(canonical.get("salary_min")),
            salary_max=_parse_salary(canonical.get("salary_max")),
            posted_date=_parse_date(canonical.get("posted_date")),
            source=source,
        )
    except ValidationError as exc:
        return None, ItemStatus.REJECTED, _validation_messages(exc)
    return posting, None, []


class JobIngestionService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.jobs = JobRepository(session)
        self.detector = DuplicateDetector(session, settings.fuzzy_duplicate_threshold)
        self.log = get_logger("app.services.ingestion")

    def ingest_records(
        self, records: Sequence[Mapping[str, Any]], source: JobSource
    ) -> IngestionSummary:
        results: list[ItemResult] = []
        limit = self.settings.max_jobs_per_run
        for index, record in enumerate(records):
            if index >= limit:
                results.append(
                    ItemResult(
                        index=index,
                        status=ItemStatus.DEFERRED,
                        errors=[f"Exceeds MAX_JOBS_PER_RUN={limit}; resubmit in a later run"],
                    )
                )
                continue
            results.append(self._ingest_one(index, record, source))
        summary = IngestionSummary(
            received=len(records),
            created=sum(r.status is ItemStatus.CREATED for r in results),
            duplicates=sum(r.status is ItemStatus.DUPLICATE for r in results),
            rejected=sum(r.status is ItemStatus.REJECTED for r in results),
            needs_extraction=sum(r.status is ItemStatus.NEEDS_EXTRACTION for r in results),
            deferred=sum(r.status is ItemStatus.DEFERRED for r in results),
            results=results,
        )
        self.log.info(
            "job import finished: received=%d created=%d duplicates=%d rejected=%d",
            summary.received,
            summary.created,
            summary.duplicates,
            summary.rejected,
            extra={"operation": "job_import", "result": source.value},
        )
        return summary

    def check(self, record: Mapping[str, Any], source: JobSource) -> CheckOutcome:
        """Validate, fingerprint and duplicate-check a job without storing anything."""
        posting, status, errors = build_posting(record, source)
        if posting is None:
            return CheckOutcome(valid=False, errors=errors or [str(status)])
        normalized = normalize_job(posting)
        duplicate = self.detector.check(normalized)
        return CheckOutcome(
            valid=True,
            fingerprint=normalized.fingerprint,
            job_id=normalized.job_id,
            is_duplicate=duplicate.is_duplicate,
            duplicate_reason=duplicate.reason,
            duplicate_of_job_id=duplicate.original_job_id,
            existing_application_id=duplicate.existing_application_id,
        )

    def _ingest_one(self, index: int, record: Mapping[str, Any], source: JobSource) -> ItemResult:
        posting, status, errors = build_posting(record, source)
        if posting is None or status is not None:
            return ItemResult(index=index, status=status or ItemStatus.REJECTED, errors=errors)
        normalized = normalize_job(posting)
        duplicate = self.detector.check(normalized)
        base: dict[str, Any] = {
            "index": index,
            "fingerprint": normalized.fingerprint,
            "company": normalized.posting.company,
            "title": normalized.posting.title,
        }
        if duplicate.is_duplicate:
            self.detector.record(normalized, duplicate)
            return ItemResult(
                **base,
                status=ItemStatus.DUPLICATE,
                duplicate_reason=duplicate.reason,
                duplicate_of_job_id=duplicate.original_job_id,
                existing_application_id=duplicate.existing_application_id,
            )
        self.jobs.add(_to_record(normalized))
        return ItemResult(**base, status=ItemStatus.CREATED, job_id=normalized.job_id)


def _to_record(job: NormalizedJob) -> JobRecord:
    posting = job.posting
    return JobRecord(
        job_id=job.job_id,
        fingerprint=job.fingerprint,
        normalized_url=job.normalized_url,
        external_id=job.external_id,
        company=posting.company,
        normalized_company=job.normalized_company,
        title=posting.title,
        normalized_title=job.normalized_title,
        company_title_key=job.company_title_key,
        description=posting.description,
        description_hash=job.description_hash,
        location=posting.location,
        employment_type=posting.employment_type,
        workplace_type=posting.workplace_type.value,
        salary_min=posting.salary_min,
        salary_max=posting.salary_max,
        posted_date=posting.posted_date,
        source=posting.source.value,
        job_url=posting.job_url,
        status=ApplicationStatus.DISCOVERED.value,
    )
