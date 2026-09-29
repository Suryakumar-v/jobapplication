from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database.tables import DuplicateRecord, JobRecord
from app.models.job import JobSource, WorkplaceType
from app.services.duplicate_detector import DuplicateReason
from app.services.job_importer import (
    ImportFormatError,
    ItemStatus,
    JobIngestionService,
    build_posting,
    canonicalize_record,
    parse_csv_text,
    parse_job_text,
    parse_json_payload,
)

from .conftest import FIXTURES

DESCRIPTION = "Required: network security, RADIUS, firewall policy management experience."


def count(session: Session, table: type[JobRecord] | type[DuplicateRecord]) -> int:
    return session.scalar(select(func.count()).select_from(table)) or 0


def test_canonicalize_maps_aliases_and_drops_unknown() -> None:
    record = canonicalize_record(
        {
            "Company Name": "Acme",
            "Job-Title": "Engineer",
            "URL": "https://a.example.com/1",
            "Job ID": 42,
            "unknown_column": "x",
            "Remote": True,
        }
    )
    assert record["company"] == "Acme"
    assert record["title"] == "Engineer"
    assert record["job_url"] == "https://a.example.com/1"
    assert record["external_id"] == 42
    assert record["workplace_type"] is True
    assert "unknown_column" not in record


def test_parse_json_payload_shapes() -> None:
    one = {"company": "A", "title": "B"}
    assert parse_json_payload(one) == [one]
    assert parse_json_payload([one, one]) == [one, one]
    assert parse_json_payload({"jobs": [one]}) == [one]
    for bad in ("text", 5, [1, 2]):
        with pytest.raises(ImportFormatError):
            parse_json_payload(bad)


def test_parse_csv() -> None:
    rows = parse_csv_text("\ufeffcompany,title,url\nAcme,Engineer,https://a.example.com/1\n,,\n")
    assert rows[0] == {"company": "Acme", "title": "Engineer", "url": "https://a.example.com/1"}
    assert len(rows) == 2
    with pytest.raises(ImportFormatError):
        parse_csv_text("")
    assert len(parse_csv_text("a\n1\n2\n3\n", limit=2)) == 2


def test_parse_job_text_headers_and_overrides() -> None:
    text = (
        "Job Title: Network Security Engineer\nCompany - Synthetic Networks\n"
        "Location: Springfield\n\nBody"
    )
    record = parse_job_text(text)
    assert record["title"] == "Network Security Engineer"
    assert record["company"] == "Synthetic Networks"
    assert record["location"] == "Springfield"
    assert record["description"] == text
    override = parse_job_text(text, company="Override Inc", job_url="https://a.example.com/1")
    assert override["company"] == "Override Inc"
    assert override["job_url"] == "https://a.example.com/1"


def test_build_posting_success_and_conversions() -> None:
    posting, status, errors = build_posting(
        {
            "company": " Acme ",
            "title": "Engineer",
            "description": DESCRIPTION,
            "salary_min": "$120k",
            "salary_max": "150,000",
            "posted_date": "2026-09-01T10:00:00Z",
            "remote": "yes",
        },
        JobSource.JSON,
    )
    assert status is None and errors == []
    assert posting is not None
    assert (posting.company, posting.salary_min, posting.salary_max) == ("Acme", 120000, 150000)
    assert posting.posted_date is not None and posting.posted_date.isoformat() == "2026-09-01"
    assert posting.workplace_type is WorkplaceType.REMOTE


@pytest.mark.parametrize(
    ("record", "fragment"),
    [
        ({"title": "T", "description": DESCRIPTION}, "company"),
        ({"company": "C", "description": DESCRIPTION}, "title"),
        ({"company": "C", "title": "T"}, "job_url or description"),
        ({"company": "C", "title": "T", "job_url": "javascript:alert(1)"}, "valid http(s) URL"),
        ({"company": "C" * 300, "title": "T", "description": DESCRIPTION}, "company"),
    ],
)
def test_build_posting_rejections(record: dict[str, str], fragment: str) -> None:
    posting, status, errors = build_posting(record, JobSource.JSON)
    assert posting is None
    assert status is ItemStatus.REJECTED
    assert any(fragment in e for e in errors)


def test_manual_url_without_company_needs_extraction() -> None:
    posting, status, errors = build_posting(
        {"url": "https://a.example.com/1"}, JobSource.MANUAL_URL
    )
    assert posting is None
    assert status is ItemStatus.NEEDS_EXTRACTION
    assert "browser extraction" in errors[0]


def test_ingest_creates_job_with_normalized_fields(session: Session, settings: Settings) -> None:
    summary = JobIngestionService(session, settings).ingest_records(
        [
            {
                "company": "Synthetic Networks Ltd",
                "title": "Sr. Network Engineer",
                "url": "https://a.example.com/1",
            }
        ],
        JobSource.JSON,
    )
    assert (summary.created, summary.duplicates, summary.rejected) == (1, 0, 0)
    job = session.scalars(select(JobRecord)).one()
    assert job.job_id == summary.results[0].job_id
    assert job.normalized_company == "synthetic networks"
    assert job.normalized_title == "senior network engineer"
    assert job.normalized_url == "https://a.example.com/1"
    assert job.status == "DISCOVERED"
    assert job.source == "JSON"


def test_duplicates_inside_one_batch_and_across_batches(
    session: Session, settings: Settings
) -> None:
    record = {"company": "Acme", "title": "Engineer", "url": "https://a.example.com/1"}
    service = JobIngestionService(session, settings)
    first = service.ingest_records(
        [record, record, {**record, "url": "https://b.example.com/2"}], JobSource.JSON
    )
    assert (first.created, first.duplicates) == (1, 2)
    assert first.results[1].duplicate_reason is DuplicateReason.FINGERPRINT
    assert first.results[2].duplicate_reason is DuplicateReason.COMPANY_TITLE
    assert first.results[1].duplicate_of_job_id == first.results[0].job_id
    second = service.ingest_records([record], JobSource.CSV)
    assert (second.created, second.duplicates) == (0, 1)
    assert count(session, JobRecord) == 1
    assert count(session, DuplicateRecord) == 3


def test_no_application_or_analysis_side_effects_for_duplicates(
    session: Session, settings: Settings
) -> None:
    from app.database.tables import ApplicationRecord, TailoredResumeRecord

    record = {"company": "Acme", "title": "Engineer", "url": "https://a.example.com/1"}
    service = JobIngestionService(session, settings)
    service.ingest_records([record, record], JobSource.JSON)
    assert session.scalar(select(func.count()).select_from(ApplicationRecord)) == 0
    assert session.scalar(select(func.count()).select_from(TailoredResumeRecord)) == 0


def test_invalid_items_do_not_stop_the_batch(session: Session, settings: Settings) -> None:
    summary = JobIngestionService(session, settings).ingest_records(
        [
            {"company": "A", "title": "T", "url": "https://a.example.com/1"},
            {"title": "no company", "url": "https://a.example.com/2"},
            {"company": "B", "title": "T2", "url": "https://a.example.com/3"},
        ],
        JobSource.JSON,
    )
    assert [r.status for r in summary.results] == [
        ItemStatus.CREATED,
        ItemStatus.REJECTED,
        ItemStatus.CREATED,
    ]


def test_max_jobs_per_run_defers_extra_jobs(session: Session, settings: Settings) -> None:
    limited = settings.model_copy(update={"max_jobs_per_run": 2})
    records = [
        {"company": f"Co{i}", "title": "Engineer", "url": f"https://a.example.com/{i}"}
        for i in range(4)
    ]
    summary = JobIngestionService(session, limited).ingest_records(records, JobSource.JSON)
    assert (summary.created, summary.deferred) == (2, 2)
    assert count(session, JobRecord) == 2


def test_check_does_not_store(session: Session, settings: Settings) -> None:
    service = JobIngestionService(session, settings)
    record = {"company": "Acme", "title": "Engineer", "url": "https://a.example.com/1"}
    before = service.check(record, JobSource.JSON)
    assert before.valid and not before.is_duplicate and before.fingerprint
    assert count(session, JobRecord) == 0
    service.ingest_records([record], JobSource.JSON)
    after = service.check(record, JobSource.JSON)
    assert after.is_duplicate and after.fingerprint == before.fingerprint
    assert not service.check({"title": "x"}, JobSource.JSON).valid


def test_mock_job_fixture_round_trip(session: Session, settings: Settings) -> None:
    record = json.loads((FIXTURES / "mock_job.json").read_text(encoding="utf-8"))
    summary = JobIngestionService(session, settings).ingest_records([record], JobSource.JSON)
    assert summary.created == 1
    job = session.scalars(select(JobRecord)).one()
    assert job.workplace_type == "HYBRID"
    assert job.normalized_url == "https://jobs.example.com/openings/12345"
