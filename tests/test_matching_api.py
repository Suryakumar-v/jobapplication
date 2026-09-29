from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from app.config import PROJECT_ROOT, Settings
from app.database.migrations import SCHEMA_VERSION, get_schema_version, initialize_database
from app.database.session import create_db_engine
from app.database.tables import JobRecord
from app.main import create_app

from .test_matching_engine import STRONG

WEAK = "Required: Kubernetes, Terraform, Azure"


def job(company: str, description: str = STRONG, **extra: Any) -> dict[str, Any]:
    return {
        "company": company,
        "title": "Network Security Engineer",
        "description": f"{description}\nAbout {company}.",
        "location": "Springfield",
        "employment_type": "Full-time",
        "job_url": f"https://jobs.example.com/{company.replace(' ', '-').lower()}/1",
        **extra,
    }


def import_jobs(client: TestClient, *records: dict[str, Any]) -> list[str]:
    body = client.post("/jobs/import/json", json={"jobs": list(records)}).json()
    return [item["job_id"] for item in body["results"]]


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_analyze_single_job_persists_result(api: TestClient) -> None:
    (job_id,) = import_jobs(api, job("Acme Networks"))
    response = api.post(f"/matching/analyze/{job_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ANALYZED"
    assert body["result"]["recommendation"] == "APPLY"
    assert body["result"]["overall_score"] == 100.0

    stored = api.get(f"/jobs/{job_id}").json()
    assert stored["status"] == "ANALYZED"
    assert stored["match_score"] == 100.0
    assert stored["recommendation"] == "APPLY"
    detail = api.get(f"/matching/{job_id}").json()
    assert detail["result"]["required_skills_missing"] == []
    assert detail["analyzed_at"]


def test_statuses_follow_recommendation(api: TestClient) -> None:
    ids = import_jobs(
        api,
        job("Good Co"),
        job("Weak Co", WEAK),
        job("Cert Co", STRONG + "\nCISSP required"),
        job("Vague Co", "Join a friendly team with great benefits."),
    )
    summary = api.post("/matching/analyze", json={}).json()
    assert (summary["apply"], summary["review"], summary["skip"], summary["ineligible"]) == (
        1,
        1,
        1,
        1,
    )
    assert summary["using_example_config"] is True
    statuses = {
        api.get(f"/jobs/{i}").json()["company"]: api.get(f"/jobs/{i}").json()["status"] for i in ids
    }
    assert statuses == {
        "Good Co": "ANALYZED",
        "Weak Co": "SKIPPED",
        "Cert Co": "INELIGIBLE",
        "Vague Co": "REVIEW_REQUIRED",
    }


def test_batch_only_takes_discovered_jobs_and_honours_limit(api: TestClient) -> None:
    import_jobs(api, job("One Co"), job("Two Co"), job("Three Co"))
    first = api.post("/matching/analyze", json={"limit": 2}).json()
    assert (first["requested"], first["analyzed"]) == (2, 2)
    second = api.post("/matching/analyze", json={}).json()
    assert second["analyzed"] == 1
    assert api.post("/matching/analyze", json={}).json()["analyzed"] == 0


def test_explicit_ids_report_unknown_and_reanalysis_is_stable(api: TestClient) -> None:
    (job_id,) = import_jobs(api, job("Acme Networks"))
    api.post(f"/matching/analyze/{job_id}")
    body = api.post("/matching/analyze", json={"job_ids": [job_id, "JOB-missing"]}).json()
    assert body["analyzed"] == 1
    assert body["not_found"] == ["JOB-missing"]
    assert body["outcomes"][0]["result"]["overall_score"] == 100.0


def test_job_past_analysis_is_not_reanalysed(api: TestClient) -> None:
    (job_id,) = import_jobs(api, job("Acme Networks"))
    with api.app.state.session_factory() as session:  # type: ignore[attr-defined]
        record = session.query(JobRecord).filter_by(job_id=job_id).one()
        record.status = "PREPARED"
        session.commit()
    assert api.post(f"/matching/analyze/{job_id}").status_code == 409
    body = api.post("/matching/analyze", json={"job_ids": [job_id]}).json()
    assert body["not_allowed"] == [job_id]


def test_unknown_job_and_unanalysed_lookup(api: TestClient) -> None:
    assert api.post("/matching/analyze/JOB-none").status_code == 404
    assert api.get("/matching/JOB-none").status_code == 404
    (job_id,) = import_jobs(api, job("Acme Networks"))
    assert api.get(f"/matching/{job_id}").status_code == 404


def test_daily_cap_applies_across_jobs(tmp_path: Path) -> None:
    capped = Settings(
        project_root=tmp_path,
        config_directory=PROJECT_ROOT / "config",
        max_applications_per_day=1,
    )
    with TestClient(create_app(capped)) as client:
        import_jobs(client, job("First Co"), job("Second Co"))
        summary = client.post("/matching/analyze", json={}).json()
        assert (summary["apply"], summary["review"]) == (1, 1)
        reasons = [o["result"]["review_reasons"] for o in summary["outcomes"]]
        assert any("limit of 1" in r for group in reasons for r in group)


def test_environment_threshold_can_only_tighten(tmp_path: Path) -> None:
    strict = Settings(
        project_root=tmp_path, config_directory=PROJECT_ROOT / "config", job_match_threshold=100
    )
    with TestClient(create_app(strict)) as client:
        import_jobs(client, job("Acme Networks", STRONG.replace("Splunk", "Zscaler")))
        outcome = client.post("/matching/analyze", json={}).json()["outcomes"][0]
        assert outcome["result"]["threshold"] == 100
        assert outcome["result"]["recommendation"] == "SKIP"


def test_missing_configuration_returns_503(tmp_path: Path) -> None:
    empty = tmp_path / "empty_config"
    empty.mkdir()
    broken = Settings(project_root=tmp_path, config_directory=empty)
    with TestClient(create_app(broken)) as client:
        (job_id,) = import_jobs(client, job("Acme Networks"))
        assert client.post(f"/matching/analyze/{job_id}").status_code == 503
        assert client.post("/matching/analyze", json={}).status_code == 503


def test_matching_requires_api_key(tmp_path: Path) -> None:
    keyed = Settings(project_root=tmp_path, config_directory=PROJECT_ROOT / "config", api_key="k")
    with TestClient(create_app(keyed)) as client:
        assert client.post("/matching/analyze", json={}).status_code == 401
        assert client.get("/matching/JOB-1").status_code == 401
        assert client.post("/matching/analyze/JOB-1").status_code == 401


def test_v1_database_is_upgraded(settings: Settings) -> None:
    engine = create_db_engine(settings.database_file)
    initialize_database(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_jobs_analyzed_at"))
        connection.execute(text("ALTER TABLE jobs DROP COLUMN analyzed_at"))
        connection.execute(text("ALTER TABLE jobs DROP COLUMN matching_details"))
        connection.execute(text("DELETE FROM schema_version"))
        connection.execute(
            text("INSERT INTO schema_version (version, applied_at) VALUES (1, '2026-01-01')")
        )
    assert "analyzed_at" not in {c["name"] for c in inspect(engine).get_columns("jobs")}
    initialize_database(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("jobs")}
    assert {"analyzed_at", "matching_details"} <= columns
    assert get_schema_version(engine) == SCHEMA_VERSION
    assert inspect(engine).has_table("form_runs")
    engine.dispose()
