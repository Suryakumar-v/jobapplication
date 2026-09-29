from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, load_settings


def test_defaults_are_safe() -> None:
    settings = Settings()
    assert settings.job_match_threshold == 75
    assert settings.manual_approval_required is True
    assert settings.automatic_submission_enabled is False
    assert settings.ai_enabled is False
    assert settings.playwright_headless is False
    assert settings.safety_violations() == []


def test_load_from_environment_mapping() -> None:
    env = {"JOB_MATCH_THRESHOLD": "80", "PLAYWRIGHT_HEADLESS": "true", "API_KEY": "  abc  "}
    settings = load_settings(env, use_dotenv=False)
    assert settings.job_match_threshold == 80
    assert settings.playwright_headless is True
    assert settings.api_key == "abc"


def test_blank_environment_values_use_defaults() -> None:
    settings = load_settings({"JOB_MATCH_THRESHOLD": ""}, use_dotenv=False)
    assert settings.job_match_threshold == 75


@pytest.mark.parametrize(
    ("env", "fragment"),
    [
        ({"MANUAL_APPROVAL_REQUIRED": "false"}, "MANUAL_APPROVAL_REQUIRED"),
        ({"AUTOMATIC_SUBMISSION_ENABLED": "true"}, "AUTOMATIC_SUBMISSION_ENABLED"),
        ({"API_HOST": "0.0.0.0"}, "API_KEY"),
    ],
)
def test_safety_violations(env: dict[str, str], fragment: str) -> None:
    violations = load_settings(env, use_dotenv=False).safety_violations()
    assert any(fragment in v for v in violations)


def test_non_loopback_with_api_key_is_allowed() -> None:
    env = {"API_HOST": "0.0.0.0", "API_KEY": "k" * 32}
    assert load_settings(env, use_dotenv=False).safety_violations() == []


def test_invalid_threshold_rejected() -> None:
    with pytest.raises(ValidationError):
        load_settings({"JOB_MATCH_THRESHOLD": "150"}, use_dotenv=False)


def test_relative_paths_resolve_from_project_root(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path)
    assert settings.database_file == tmp_path / "data" / "job_tracker.db"
    assert settings.generated_resumes_dir == tmp_path / "resumes" / "generated"
    absolute = tmp_path / "elsewhere"
    assert Settings(project_root=tmp_path, log_directory=absolute).logs_dir == absolute
