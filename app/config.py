"""Application settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class Settings(BaseModel):
    """Runtime settings. Field ``foo_bar`` is read from the environment variable ``FOO_BAR``."""

    model_config = ConfigDict(frozen=True)

    project_root: Path = PROJECT_ROOT

    job_automation_api_url: str = "http://127.0.0.1:8000"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_key: str = ""

    job_match_threshold: int = Field(default=75, ge=0, le=100)
    max_jobs_per_run: int = Field(default=50, ge=1)
    max_applications_per_day: int = Field(default=10, ge=1)
    manual_approval_required: bool = True
    automatic_submission_enabled: bool = False
    approval_token_expiry: int = Field(default=3600, ge=60)
    # 0 disables fuzzy description matching; typical enabled value is 0.9.
    fuzzy_duplicate_threshold: float = Field(default=0.0, ge=0, le=1)

    ai_enabled: bool = False
    ai_provider: Literal["none", "mock", "openai_compatible"] = "none"
    # Default is a local Ollama server; only job text is ever sent, never profile data.
    ai_base_url: str = "http://127.0.0.1:11434/v1"
    ai_model: str = ""
    ai_api_key: str = Field(default="", repr=False)
    ai_timeout: int = Field(default=60, ge=5, le=600)
    ai_max_input_chars: int = Field(default=8000, ge=500, le=50000)
    # Must be true before job text may be sent to a non-loopback AI endpoint.
    ai_allow_remote: bool = False

    database_path: Path = Path("data/job_tracker.db")
    screenshot_directory: Path = Path("screenshots")
    resume_directory: Path = Path("resumes")
    export_directory: Path = Path("exports")
    log_directory: Path = Path("logs")
    config_directory: Path = Path("config")

    playwright_headless: bool = False
    playwright_slow_mo: int = Field(default=250, ge=0)
    playwright_timeout: int = Field(default=30000, ge=1000)
    playwright_session_timeout: int = Field(default=900, ge=60)
    playwright_browser_channel: Literal["chromium", "chrome", "msedge"] = "chromium"
    # Comma-separated hosts the browser may open in addition to loopback. Empty = local only.
    playwright_allowed_hosts: str = ""

    @field_validator("api_key", "ai_api_key", mode="before")
    @classmethod
    def _strip_key(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @property
    def browser_allowed_hosts(self) -> frozenset[str]:
        extra = {h.strip().lower() for h in self.playwright_allowed_hosts.split(",") if h.strip()}
        return LOOPBACK_HOSTS | extra

    def resolve(self, path: Path) -> Path:
        return path if path.is_absolute() else self.project_root / path

    @property
    def database_file(self) -> Path:
        return self.resolve(self.database_path)

    @property
    def screenshots_dir(self) -> Path:
        return self.resolve(self.screenshot_directory)

    @property
    def resumes_dir(self) -> Path:
        return self.resolve(self.resume_directory)

    @property
    def source_resumes_dir(self) -> Path:
        return self.resumes_dir / "source"

    @property
    def generated_resumes_dir(self) -> Path:
        return self.resumes_dir / "generated"

    @property
    def exports_dir(self) -> Path:
        return self.resolve(self.export_directory)

    @property
    def logs_dir(self) -> Path:
        return self.resolve(self.log_directory)

    @property
    def config_dir(self) -> Path:
        return self.resolve(self.config_directory)

    def required_directories(self) -> dict[str, Path]:
        return {
            "data": self.database_file.parent,
            "screenshots": self.screenshots_dir,
            "resumes_source": self.source_resumes_dir,
            "resumes_generated": self.generated_resumes_dir,
            "exports": self.exports_dir,
            "logs": self.logs_dir,
            "config": self.config_dir,
        }

    def safety_violations(self) -> list[str]:
        """Reasons the service must refuse to start. Empty when safe defaults are intact."""
        problems: list[str] = []
        if not self.manual_approval_required:
            problems.append("MANUAL_APPROVAL_REQUIRED must be true.")
        if self.automatic_submission_enabled:
            problems.append("AUTOMATIC_SUBMISSION_ENABLED must be false.")
        if self.api_host not in LOOPBACK_HOSTS and not self.api_key:
            problems.append("API_KEY is required when API_HOST is not a loopback address.")
        problems.extend(self.ai_violations())
        return problems

    def ai_violations(self) -> list[str]:
        """Misconfigurations that could send job text somewhere unintended. Empty when AI is off."""
        if not self.ai_enabled:
            return []
        if self.ai_provider == "none":
            return ["AI_ENABLED=true needs AI_PROVIDER other than none."]
        if self.ai_provider != "openai_compatible":
            return []
        problems: list[str] = []
        parts = urlsplit(self.ai_base_url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
            return ["AI_BASE_URL must be an http(s) URL without credentials."]
        if not self.ai_model.strip():
            problems.append("AI_MODEL is required for the openai_compatible provider.")
        if host not in LOOPBACK_HOSTS:
            if not self.ai_allow_remote:
                problems.append(
                    "AI_BASE_URL is not local; set AI_ALLOW_REMOTE=true to send job text there."
                )
            if parts.scheme != "https":
                problems.append("A non-local AI_BASE_URL must use https.")
        return problems


def load_settings(env: Mapping[str, str] | None = None, *, use_dotenv: bool = True) -> Settings:
    """Build settings from ``env`` (default: process environment after loading ``.env``)."""
    if env is None:
        if use_dotenv:
            load_dotenv(PROJECT_ROOT / ".env", override=False)
        env = os.environ
    values: dict[str, Any] = {}
    for name in Settings.model_fields:
        raw = env.get(name.upper())
        if raw is not None and raw != "":
            values[name] = raw
    return Settings(**values)


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)
