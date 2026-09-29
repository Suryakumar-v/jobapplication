"""Read a job posting in the browser and store it. Read-only: nothing is filled or submitted."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.automation.portals import Blocker, select_portal
from app.automation.tasks import extract_task
from app.automation.url_guard import check_portal_enabled, check_url
from app.automation.worker import BrowserWorker
from app.config import Settings
from app.models.job import JobSource
from app.services.config_loader import load_form_config
from app.services.job_importer import IngestionSummary, JobIngestionService


class ManualActionRequiredError(RuntimeError):
    def __init__(self, blockers: list[Blocker]) -> None:
        super().__init__("The page needs a person: " + ", ".join(b.kind for b in blockers))
        self.blockers = blockers


class ExtractionOutcome(IngestionSummary):
    extracted: dict[str, Any]


class JobExtractionService:
    def __init__(self, session: Session, settings: Settings, worker: BrowserWorker) -> None:
        self.session = session
        self.settings = settings
        self.worker = worker

    def extract(
        self, url: str, *, company: str | None = None, title: str | None = None
    ) -> ExtractionOutcome:
        safe_url = check_url(url, self.settings.browser_allowed_hosts)
        portals = load_form_config(self.settings).portals
        check_portal_enabled(safe_url, portals)
        page = self.worker.run(extract_task(safe_url, select_portal(portals)))
        if page.blockers:
            raise ManualActionRequiredError(page.blockers)
        record = dict(page.record)
        if company and company.strip():
            record["company"] = company.strip()
        if title and title.strip():
            record["title"] = title.strip()
        summary = JobIngestionService(self.session, self.settings).ingest_records(
            [record], JobSource.BROWSER
        )
        preview = {
            key: record.get(key) for key in ("company", "title", "location", "employment_type")
        }
        preview["description_length"] = len(str(record.get("description", "")))
        return ExtractionOutcome(extracted=preview, **summary.model_dump())
