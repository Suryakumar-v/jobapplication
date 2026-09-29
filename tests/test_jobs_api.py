from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.main import create_app

JOB = {
    "company": "Synthetic Networks Ltd",
    "title": "Network Security Engineer",
    "job_url": "https://jobs.example.com/openings/12345",
    "description": "Required: network security, RADIUS, firewall policy management experience.",
}


@pytest.fixture
def secured_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        project_root=tmp_path, config_directory=PROJECT_ROOT / "config", api_key="test-key"
    )
    with TestClient(create_app(settings), headers={"X-API-Key": "test-key"}) as test_client:
        yield test_client


def test_endpoints_require_api_key(tmp_path: Path) -> None:
    settings = Settings(
        project_root=tmp_path, config_directory=PROJECT_ROOT / "config", api_key="test-key"
    )
    with TestClient(create_app(settings)) as anonymous:
        assert anonymous.post("/jobs/import/json", json=JOB).status_code == 401
        assert anonymous.get("/jobs").status_code == 401
        assert anonymous.get("/health").status_code == 200


def test_import_json_then_duplicate(secured_client: TestClient) -> None:
    first = secured_client.post("/jobs/import/json", json=JOB).json()
    assert first["success"] is True
    assert first["operation"] == "import_json"
    assert first["created"] == 1
    job_id = first["results"][0]["job_id"]
    second = secured_client.post("/jobs/import/json", json=JOB).json()
    assert second["created"] == 0
    assert second["duplicates"] == 1
    assert second["results"][0]["duplicate_of_job_id"] == job_id
    assert second["results"][0]["duplicate_reason"] == "FINGERPRINT"
    assert len(secured_client.get("/jobs").json()) == 1


def test_webhook_accepts_list_and_wrapped_payloads(secured_client: TestClient) -> None:
    other = {
        **JOB,
        "company": "Other Corp",
        "job_url": "https://o.example.com/1",
        "description": "A different posting about SD-WAN operations and Splunk dashboards.",
    }
    body = secured_client.post("/jobs/webhook", json={"jobs": [JOB, other]}).json()
    assert body["operation"] == "webhook"
    assert body["created"] == 2
    listed = secured_client.get("/jobs").json()
    assert {j["source"] for j in listed} == {"WEBHOOK"}


def test_import_csv(secured_client: TestClient) -> None:
    csv_text = "company,title,url,description\nAcme,Engineer,https://a.example.com/1,\nAcme,Engineer,https://a.example.com/1,\n,,,\n"
    body = secured_client.post("/jobs/import/csv", json={"csv_text": csv_text}).json()
    assert (body["created"], body["duplicates"], body["rejected"]) == (1, 1, 1)


def test_import_text(secured_client: TestClient) -> None:
    text = (
        "Title: Firewall Engineer\nCompany: Acme\n\n"
        "Manage firewall policy and RADIUS for the network team."
    )
    body = secured_client.post("/jobs/import/text", json={"text": text}).json()
    assert body["created"] == 1
    assert body["results"][0]["title"] == "Firewall Engineer"


def test_import_url_needs_extraction_without_company_and_title(secured_client: TestClient) -> None:
    body = secured_client.post("/jobs/import/url", json={"url": "https://a.example.com/1"}).json()
    assert body["needs_extraction"] == 1
    assert body["created"] == 0
    with_details = secured_client.post(
        "/jobs/import/url",
        json={"url": "https://a.example.com/1", "company": "Acme", "title": "Engineer"},
    ).json()
    assert with_details["created"] == 1


def test_bad_payloads_are_rejected(secured_client: TestClient) -> None:
    assert secured_client.post("/jobs/import/json", json=[1, 2]).status_code == 422
    assert secured_client.post("/jobs/import/csv", json={"csv_text": ""}).status_code == 422
    assert secured_client.post("/jobs/import/url", json={"url": ""}).status_code == 422
    rejected = secured_client.post(
        "/jobs/import/json", json={"title": "x", "job_url": "file:///c"}
    ).json()
    assert rejected["rejected"] == 1
    assert rejected["created"] == 0


def test_check_endpoint_does_not_store(secured_client: TestClient) -> None:
    result = secured_client.post("/jobs/check", json=JOB).json()
    assert result["success"] is True
    assert result["is_duplicate"] is False
    assert secured_client.get("/jobs").json() == []
    secured_client.post("/jobs/import/json", json=JOB)
    again = secured_client.post("/jobs/check", json=JOB).json()
    assert again["is_duplicate"] is True
    assert again["fingerprint"] == result["fingerprint"]
    invalid = secured_client.post("/jobs/check", json={"title": "x"}).json()
    assert invalid["success"] is False


def test_get_job_and_missing_job(secured_client: TestClient) -> None:
    job_id = secured_client.post("/jobs/import/json", json=JOB).json()["results"][0]["job_id"]
    detail = secured_client.get(f"/jobs/{job_id}").json()
    assert detail["company"] == "Synthetic Networks Ltd"
    assert detail["status"] == "DISCOVERED"
    assert "description" not in detail
    assert detail["description_length"] > 0
    assert secured_client.get("/jobs/JOB-NOPE").status_code == 404
    assert secured_client.get("/jobs?status=SKIPPED").json() == []
    assert secured_client.get("/jobs?limit=0").status_code == 422


def test_max_jobs_per_run_enforced_by_api(tmp_path: Path) -> None:
    settings = Settings(
        project_root=tmp_path, config_directory=PROJECT_ROOT / "config", max_jobs_per_run=1
    )
    jobs = [{**JOB, "company": f"C{i}", "job_url": f"https://a.example.com/{i}"} for i in range(3)]
    with TestClient(create_app(settings)) as client:
        body = client.post("/jobs/import/json", json=jobs).json()
    assert (body["created"], body["deferred"]) == (1, 2)
