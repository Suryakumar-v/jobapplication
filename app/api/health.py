"""GET /health: service, database, directory and safety-flag status (no secrets)."""

from __future__ import annotations

import importlib.util
import os

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import Engine, text

from app import __version__
from app.config import Settings
from app.database.migrations import SCHEMA_VERSION, get_schema_version
from app.dependencies import SettingsDep

router = APIRouter(tags=["health"])


class DatabaseHealth(BaseModel):
    ok: bool
    schema_version: int | None = None
    expected_schema_version: int = SCHEMA_VERSION


class SafetyFlags(BaseModel):
    manual_approval_required: bool
    automatic_submission_enabled: bool
    ai_enabled: bool
    match_threshold: int
    violations: list[str]


class BrowserHealth(BaseModel):
    playwright_installed: bool
    headless: bool


class HealthResponse(BaseModel):
    status: str
    startup_ok: bool
    version: str
    database: DatabaseHealth
    directories: dict[str, bool]
    safety: SafetyFlags
    browser: BrowserHealth


def _database_health(engine: Engine) -> DatabaseHealth:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return DatabaseHealth(ok=True, schema_version=get_schema_version(engine))
    except Exception:
        return DatabaseHealth(ok=False)


def _directory_health(settings: Settings) -> dict[str, bool]:
    return {
        name: path.is_dir() and os.access(path, os.W_OK)
        for name, path in settings.required_directories().items()
    }


@router.get("/health", response_model=HealthResponse)
def health(request: Request, settings: SettingsDep) -> HealthResponse:
    database = _database_health(request.app.state.engine)
    directories = _directory_health(settings)
    violations = settings.safety_violations()
    browser = BrowserHealth(
        playwright_installed=importlib.util.find_spec("playwright") is not None,
        headless=settings.playwright_headless,
    )
    startup_ok = (
        database.ok
        and database.schema_version == SCHEMA_VERSION
        and all(directories.values())
        and not violations
        and browser.playwright_installed
    )
    return HealthResponse(
        status="ok" if startup_ok else "degraded",
        startup_ok=startup_ok,
        version=__version__,
        database=database,
        directories=directories,
        safety=SafetyFlags(
            manual_approval_required=settings.manual_approval_required,
            automatic_submission_enabled=settings.automatic_submission_enabled,
            ai_enabled=settings.ai_enabled,
            match_threshold=settings.job_match_threshold,
            violations=violations,
        ),
        browser=browser,
    )
