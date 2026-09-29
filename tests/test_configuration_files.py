from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.config import PROJECT_ROOT, Settings
from app.models.application import PortalSettings
from app.models.candidate import CandidateProfile
from app.models.question import SafeAnswer, SafeAnswers, SensitiveQuestions
from app.models.scoring import ScoringConfig, ScoringWeights
from scripts.validate_configuration import (
    check_config_files,
    check_settings,
    run_checks,
)

from .conftest import FIXTURES

CONFIG = PROJECT_ROOT / "config"


def test_repository_config_files_all_valid() -> None:
    results = check_config_files(CONFIG)
    assert all(r.ok for r in results), [r for r in results if not r.ok]
    assert {r.name for r in results} == {
        "candidate_profile",
        "search_preferences",
        "safe_answers",
        "sensitive_questions",
        "scoring_weights",
        "skill_vocabulary",
        "portal_settings",
    }


def test_example_files_are_flagged_as_examples() -> None:
    by_name = {r.name: r for r in check_config_files(CONFIG)}
    assert by_name["candidate_profile"].warning is True
    assert by_name["scoring_weights"].warning is False


def test_missing_config_directory_reports_failures(tmp_path: Path) -> None:
    results = check_config_files(tmp_path)
    assert results
    assert not any(r.ok for r in results)


def test_invalid_yaml_content_reported(tmp_path: Path) -> None:
    (tmp_path / "scoring_weights.yaml").write_text(
        "weights: {required_skills: 99}", encoding="utf-8"
    )
    result = next(r for r in check_config_files(tmp_path) if r.name == "scoring_weights")
    assert not result.ok


def test_synthetic_candidate_fixture_valid() -> None:
    data = yaml.safe_load((FIXTURES / "mock_candidate.yaml").read_text(encoding="utf-8"))
    profile = CandidateProfile.model_validate(data)
    assert "radius" in profile.skill_names


def test_mock_job_fixture_is_valid_json() -> None:
    data: dict[str, Any] = json.loads((FIXTURES / "mock_job.json").read_text(encoding="utf-8"))
    from app.models.job import JobPosting

    assert JobPosting.model_validate(data).company == "Synthetic Networks Ltd"


def test_scoring_weights_default_sum_to_100_and_threshold_75() -> None:
    config = ScoringConfig.model_validate(
        yaml.safe_load((CONFIG / "scoring_weights.yaml").read_text())
    )
    assert config.minimum_score == 75
    assert config.weights.required_skills == 35
    with pytest.raises(ValueError, match="sum to 100"):
        ScoringWeights(
            required_skills=50,
            relevant_experience=20,
            preferred_skills=10,
            certifications=10,
            job_title_relevance=10,
            location_and_remote=10,
            employment_type=5,
        )


@pytest.mark.parametrize(
    "category",
    ["WORK_AUTHORIZATION", "SPONSORSHIP", "SALARY", "DEMOGRAPHIC", "LEGAL", "UNKNOWN"],
)
def test_sensitive_categories_can_never_be_safe_answers(category: str) -> None:
    with pytest.raises(ValueError, match="never be pre-approved"):
        SafeAnswer(key="k", patterns=["p"], value="v", category=category, source="s")


def test_safe_answers_example_is_all_autofill_safe() -> None:
    answers = SafeAnswers.model_validate(
        yaml.safe_load((CONFIG / "safe_answers.example.yaml").read_text(encoding="utf-8"))
    )
    assert answers.answers
    assert {a.category.value for a in answers.answers} <= {"SAFE_PROFILE", "SAFE_JOB_SPECIFIC"}


def test_sensitive_questions_requires_every_category() -> None:
    with pytest.raises(ValueError, match="missing sensitive categories"):
        SensitiveQuestions.model_validate({"categories": {"SALARY": ["salary"]}})


def test_portal_settings_reject_bypass_and_validate_nothing_yet() -> None:
    data = yaml.safe_load((CONFIG / "portal_settings.yaml").read_text(encoding="utf-8"))
    portals = PortalSettings.model_validate(data)
    assert not any(p.validated for p in portals.portals.values())
    data["allow_captcha_or_mfa_bypass"] = True
    with pytest.raises(ValueError, match="bypass"):
        PortalSettings.model_validate(data)


def test_settings_check_reports_unsafe_flags(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, automatic_submission_enabled=True)
    result = check_settings(settings, require_directories=False)[0]
    assert not result.ok
    assert "AUTOMATIC_SUBMISSION_ENABLED" in result.detail


def test_run_checks_flags_missing_directories(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, config_directory=CONFIG)
    failures = [r for r in run_checks(settings) if not r.ok]
    assert any(r.name.startswith("dir:") for r in failures)
