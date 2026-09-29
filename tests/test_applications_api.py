from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import PROJECT_ROOT, Settings
from app.database.repositories import ApplicationRepository, JobRepository
from app.main import create_app
from app.models.application import ApplicationStatus

from .test_database import make_job


def _seed(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        jobs = JobRepository(session)
        applications = ApplicationRepository(session)
        first = applications.create_for_job(jobs.add(make_job("1")))
        second = applications.create_for_job(jobs.add(make_job("2")))
        applications.set_status(first.application_id, ApplicationStatus.PREPARED)
        second.tailored_resume_path = "resumes/generated/x.docx"


def test_list_and_filter_applications(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _seed(session_factory)
    with TestClient(create_app(settings)) as client:
        everything = client.get("/applications").json()
        assert len(everything) == 2
        prepared = client.get("/applications", params={"status": "PREPARED"}).json()
        assert [a["job_id"] for a in prepared] == ["JOB-1"]
        assert client.get("/applications", params={"status": "SUBMITTED"}).json() == []
        assert client.get("/applications", params={"limit": 1}).json()[0]["job_id"] == "JOB-1"
        assert client.get("/applications", params={"limit": 0}).status_code == 422


def test_get_application_and_resume_flag(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _seed(session_factory)
    with TestClient(create_app(settings)) as client:
        by_job = {a["job_id"]: a for a in client.get("/applications").json()}
        assert by_job["JOB-1"]["has_tailored_resume"] is False
        assert by_job["JOB-2"]["has_tailored_resume"] is True
        assert "tailored_resume_path" not in by_job["JOB-2"]
        one = client.get(f"/applications/{by_job['JOB-1']['application_id']}")
        assert one.status_code == 200
        assert client.get("/applications/APP-19700101-0001").status_code == 404


def test_application_endpoints_require_api_key(tmp_path: Path) -> None:
    keyed = Settings(
        project_root=tmp_path, config_directory=PROJECT_ROOT / "config", api_key="test-key"
    )
    with TestClient(create_app(keyed)) as anonymous:
        assert anonymous.get("/applications").status_code == 401
        assert anonymous.get("/applications/APP-19700101-0001").status_code == 401
