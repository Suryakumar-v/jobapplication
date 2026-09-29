"""Static checks of the Dockerfile and compose file; no Docker engine is needed."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.config import PROJECT_ROOT, Settings

COMPOSE = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
DOCKERFILE = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
SERVICES: dict[str, Any] = COMPOSE["services"]
SETTING_NAMES = {name.upper() for name in Settings.model_fields}


def test_ports_are_published_on_loopback_only() -> None:
    for name, service in SERVICES.items():
        for port in service["ports"]:
            assert str(port).startswith("127.0.0.1:"), (name, port)


def test_secrets_are_required_and_not_stored_in_the_file() -> None:
    assert SERVICES["job-automation-api"]["environment"]["API_KEY"].startswith("${API_KEY:?")
    assert SERVICES["n8n"]["environment"]["N8N_ENCRYPTION_KEY"].startswith("${N8N_ENCRYPTION_KEY:?")


def test_safety_defaults_are_safe_in_both_services() -> None:
    for name, service in SERVICES.items():
        env = service["environment"]
        assert env["MANUAL_APPROVAL_REQUIRED"] == "${MANUAL_APPROVAL_REQUIRED:-true}", name
        assert env["AUTOMATIC_SUBMISSION_ENABLED"] == "${AUTOMATIC_SUBMISSION_ENABLED:-false}"
        assert env["AI_ENABLED"] == "${AI_ENABLED:-false}", name


def test_container_browser_is_headless() -> None:
    assert SERVICES["job-automation-api"]["environment"]["PLAYWRIGHT_HEADLESS"] == "true"


def test_every_api_variable_is_a_real_setting() -> None:
    unknown = set(SERVICES["job-automation-api"]["environment"]) - SETTING_NAMES
    assert not unknown, unknown


def test_named_volumes_are_declared_and_dependencies_exist() -> None:
    declared = set(COMPOSE["volumes"])
    for service in SERVICES.values():
        for volume in service["volumes"]:
            source = str(volume).split(":")[0]
            if not source.startswith("."):
                assert source in declared, source
        for dependency in service.get("depends_on", {}):
            assert dependency in SERVICES


def test_workflows_and_config_are_mounted_read_only() -> None:
    assert "./n8n/workflows:/workflows:ro" in SERVICES["n8n"]["volumes"]
    assert "./config:/app/config:ro" in SERVICES["job-automation-api"]["volumes"]


def test_n8n_reaches_the_api_by_service_name_with_a_health_gate() -> None:
    assert SERVICES["n8n"]["environment"]["JOB_AUTOMATION_API_URL"] == (
        "http://job-automation-api:8000"
    )
    assert SERVICES["n8n"]["depends_on"]["job-automation-api"]["condition"] == "service_healthy"


def test_image_runs_as_a_non_root_user() -> None:
    lines = [line.strip() for line in DOCKERFILE.splitlines()]
    assert "USER appuser" in lines
    assert lines.index("USER appuser") > max(
        i for i, line in enumerate(lines) if line.startswith("RUN ")
    ), "USER must come after the last RUN"


def test_every_copied_path_exists() -> None:
    for match in re.finditer(r"^COPY\s+(.+?)\s+\./?\S*\s*$", DOCKERFILE, re.MULTILINE):
        for source in match.group(1).split():
            assert (PROJECT_ROOT / source).exists(), source


def test_personal_files_are_kept_out_of_the_image() -> None:
    for name in (
        ".env",
        "config/candidate_profile.yaml",
        "config/search_preferences.yaml",
        "config/safe_answers.yaml",
    ):
        assert name in DOCKERIGNORE, name


def test_dockerignore_matches_gitignore_for_personal_config() -> None:
    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for line in ignored:
        if line.startswith("config/") and line.endswith(".yaml"):
            assert line in DOCKERIGNORE, line


@pytest.mark.parametrize("folder", ["resumes", "exports", "config"])
def test_bind_mount_sources_exist(folder: str) -> None:
    assert Path(PROJECT_ROOT / folder).is_dir()


IMPORT_TO_REQUIREMENT = {
    "yaml": "pyyaml",
    "docx": "python-docx",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
}


def test_requirements_cover_every_runtime_import() -> None:
    """The image installs only requirements.txt, so a missing entry breaks it at start-up."""
    required = {
        re.split(r"[\[<>=~ ]", line, maxsplit=1)[0].lower()
        for line in (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    first_party = {"app", "scripts", "run", "tests"}
    missing: set[str] = set()
    for path in [*(PROJECT_ROOT / "app").rglob("*.py"), *(PROJECT_ROOT / "scripts").rglob("*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom) and node.level == 0
                else []
            )
            for name in names:
                top = name.split(".")[0]
                known = top in sys.stdlib_module_names | first_party
                if (
                    top
                    and not known
                    and IMPORT_TO_REQUIREMENT.get(top, top).lower() not in required
                ):
                    missing.add(f"{top} ({path.name})")
    assert not missing, sorted(missing)
