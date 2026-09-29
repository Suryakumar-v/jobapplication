"""Portal adapter interface. Adapters read pages; they never click, submit or sign in."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from playwright.sync_api import Page
from pydantic import BaseModel

from app.automation.form_mapping import FormField
from app.automation.scripts import DETECT_BLOCKERS_JS


class Blocker(BaseModel):
    """Something only a person can resolve: sign-in, CAPTCHA or a verification code."""

    kind: str
    detail: str


class BasePortal(ABC):
    name: ClassVar[str]

    @abstractmethod
    def extract_job(self, page: Page, url: str) -> dict[str, Any]:
        """Return a job record using the canonical field names of the importer."""

    @abstractmethod
    def discover_fields(self, page: Page) -> list[FormField]:
        """Return the visible, enabled form controls."""

    def detect_blockers(self, page: Page) -> list[Blocker]:
        raw: list[dict[str, str]] = page.evaluate(DETECT_BLOCKERS_JS)
        return [Blocker.model_validate(item) for item in raw]
