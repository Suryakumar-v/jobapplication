"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api import (
    ai,
    applications,
    approvals,
    browser,
    exports,
    health,
    jobs,
    matching,
    resumes,
)
from app.automation.worker import BrowserWorker
from app.config import Settings, load_settings
from app.database.migrations import initialize_database
from app.database.session import create_db_engine, create_session_factory
from app.utils.file_utils import ensure_directories
from app.utils.logging_config import get_logger, setup_logging, shutdown_logging


class UnsafeConfigurationError(RuntimeError):
    pass


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        violations = resolved.safety_violations()
        if violations:
            raise UnsafeConfigurationError("Refusing to start: " + " ".join(violations))
        ensure_directories(resolved.required_directories().values())
        setup_logging(resolved.logs_dir)
        engine = create_db_engine(resolved.database_file)
        initialize_database(engine)
        app.state.settings = resolved
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        app.state.browser = BrowserWorker(resolved)
        get_logger("app.main").info("service started", extra={"operation": "startup"})
        try:
            yield
        finally:
            app.state.browser.shutdown()
            engine.dispose()
            shutdown_logging()

    app = FastAPI(
        title="Job Application Automation API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(browser.jobs_router)
    app.include_router(matching.router)
    app.include_router(resumes.router)
    app.include_router(applications.router)
    app.include_router(approvals.router)
    app.include_router(exports.router)
    app.include_router(ai.router)
    app.include_router(browser.applications_router)
    return app


def app_factory() -> FastAPI:
    """Uvicorn factory entry point: ``uvicorn app.main:app_factory --factory``."""
    return create_app()
