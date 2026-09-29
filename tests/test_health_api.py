from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.main import UnsafeConfigurationError, create_app


def test_health_reports_ok_with_safe_defaults(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["startup_ok"] is True
    assert body["database"] == {"ok": True, "schema_version": 4, "expected_schema_version": 4}
    assert all(body["directories"].values())
    assert body["safety"]["manual_approval_required"] is True
    assert body["safety"]["automatic_submission_enabled"] is False
    assert body["safety"]["ai_enabled"] is False
    assert body["safety"]["match_threshold"] == 75
    assert body["safety"]["violations"] == []
    assert body["browser"]["playwright_installed"] is True


def test_health_never_exposes_secrets(tmp_path: Path) -> None:
    settings = Settings(
        project_root=tmp_path, config_directory=PROJECT_ROOT / "config", api_key="super-secret-key"
    )
    with TestClient(create_app(settings)) as client:
        assert "super-secret-key" not in client.get("/health").text


@pytest.mark.parametrize(
    "overrides",
    [{"manual_approval_required": False}, {"automatic_submission_enabled": True}],
)
def test_service_refuses_to_start_when_unsafe(tmp_path: Path, overrides: dict[str, bool]) -> None:
    settings = Settings(
        project_root=tmp_path, config_directory=PROJECT_ROOT / "config", **overrides
    )
    with pytest.raises(UnsafeConfigurationError), TestClient(create_app(settings)):
        pass


def test_startup_creates_directories_database_and_logs(
    client: TestClient, settings: Settings
) -> None:
    assert settings.database_file.is_file()
    for path in settings.required_directories().values():
        assert path.is_dir()
    client.get("/health")


def test_docs_do_not_break_startup(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
