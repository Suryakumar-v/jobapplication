"""Browser tasks. They run on the worker thread and return plain data, never touching the database.

Nothing here clicks, presses Enter or otherwise submits, except submit_task, which the approval
service runs only after a valid, single-use approval. Fields are filled and files attached; the
page is then left open (or closed) for a person to review.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Locator, Page, Response
from playwright.sync_api import Error as PlaywrightError
from pydantic import BaseModel, Field

from app.automation.form_mapping import (
    FieldReport,
    FieldStatus,
    PlannedField,
    SensitiveMatcher,
    plan_fields,
)
from app.automation.portals.base import BasePortal, Blocker
from app.automation.scripts import (
    CHECK_SUBMIT_JS,
    FIELD_ATTRIBUTE,
    SUBMIT_ATTRIBUTE,
    SUBMIT_GUARD_JS,
)
from app.automation.worker import BrowserHost, GuardState, LiveSession
from app.models.question import SafeAnswers


class PageExtraction(BaseModel):
    record: dict[str, Any]
    blockers: list[Blocker]


class FillResult(BaseModel):
    final_url: str
    blockers: list[Blocker]
    fields: list[FieldReport]
    screenshots: list[str]
    session_kept: bool = False


@dataclass(frozen=True)
class FillRequest:
    application_id: str
    url: str
    portal: BasePortal
    answers: SafeAnswers
    matcher: SensitiveMatcher
    threshold: float
    resume_path: Path
    screenshot_dir: Path


def _open(host: BrowserHost, url: str, guard: GuardState) -> tuple[BrowserContext, Page]:
    context = host.new_context(guard)
    try:
        page = context.new_page()
        page.add_init_script(SUBMIT_GUARD_JS)
        page.goto(url, wait_until="domcontentloaded")
        _settle(page)
    except Exception:
        context.close()
        raise
    return context, page


def _settle(page: Page) -> None:
    """Give scripts a moment to render; a slow page is not an error."""
    try:
        page.wait_for_load_state("load", timeout=5000)
    except PlaywrightError:
        return


def extract_task(url: str, portal: BasePortal) -> Callable[[BrowserHost], PageExtraction]:
    def task(host: BrowserHost) -> PageExtraction:
        context, page = _open(host, url, GuardState())
        try:
            blockers = portal.detect_blockers(page)
            record = {} if blockers else portal.extract_job(page, page.url)
            return PageExtraction(record=record, blockers=blockers)
        finally:
            context.close()

    return task


def _snapshot(page: Page, directory: Path, label: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"{stamp}-{label}.png"
    page.screenshot(path=str(target), full_page=True)
    return target.name


def _fill(locator: Locator, planned: PlannedField, resume_path: Path) -> tuple[FieldStatus, str]:
    value = planned.value
    try:
        if planned.report.status is FieldStatus.PLAN_UPLOAD:
            locator.set_input_files(str(resume_path))
            attached = locator.evaluate("(el) => el.files.length")
            if attached == 1:
                return FieldStatus.UPLOADED, planned.report.reason
            return FieldStatus.FILL_FAILED, "file was not attached"
        if value is None:
            return FieldStatus.FILL_FAILED, "no value"
        if planned.field.tag == "select":
            locator.select_option(label=value)
            chosen = locator.evaluate("(el) => el.options[el.selectedIndex].text.trim()")
            ok = chosen == value
        else:
            locator.fill(value)
            ok = locator.input_value() == value
    except PlaywrightError as exc:
        return FieldStatus.FILL_FAILED, type(exc).__name__
    if ok:
        return FieldStatus.FILLED, planned.report.reason
    return FieldStatus.FILL_FAILED, "value did not persist in the field"


def fill_task(request: FillRequest) -> Callable[[BrowserHost], FillResult]:
    def task(host: BrowserHost) -> FillResult:
        guard = GuardState()
        context, page = _open(host, request.url, guard)
        keep = False
        try:
            shots: list[str] = []
            blockers = request.portal.detect_blockers(page)
            if blockers:
                shots.append(_snapshot(page, request.screenshot_dir, "blocked"))
                return FillResult(
                    final_url=page.url, blockers=blockers, fields=[], screenshots=shots
                )

            planned = plan_fields(
                request.portal.discover_fields(page),
                request.answers,
                request.matcher,
                threshold=request.threshold,
                resume_available=True,
            )
            shots.append(_snapshot(page, request.screenshot_dir, "before"))
            reports: list[FieldReport] = []
            for item in planned:
                if item.report.status in (FieldStatus.PLAN_FILL, FieldStatus.PLAN_UPLOAD):
                    locator = page.locator(f'[{FIELD_ATTRIBUTE}="{item.field.index}"]')
                    status, reason = _fill(locator, item, request.resume_path)
                    reports.append(
                        item.report.model_copy(update={"status": status, "reason": reason})
                    )
                else:
                    reports.append(item.report)
            shots.append(_snapshot(page, request.screenshot_dir, "after"))
            host.keep(LiveSession(request.application_id, context, page, guard))
            keep = True
            return FillResult(
                final_url=page.url,
                blockers=[],
                fields=reports,
                screenshots=shots,
                session_kept=True,
            )
        finally:
            if not keep:
                context.close()

    return task


SUBMIT_SENT = "SUBMITTED"
SUBMIT_NOT_SENT = "NOT_SENT"
SUBMIT_REQUEST_FAILED = "REQUEST_FAILED"
SUBMIT_BLOCKED = "BLOCKED"

_READ_ONLY = frozenset({"GET", "HEAD"})
_THANKS = re.compile(
    r"thank you|(?:application|submission)\s+(?:has been\s+|was\s+)?(?:received|submitted|complete)"
    r"|successfully\s+submitted",
    re.IGNORECASE,
)
_NUMBER = re.compile(
    r"(?:confirmation|reference|application)\s*(?:number|no\.?|#|id)\s*[:#-]?\s*"
    r"([A-Z0-9][A-Z0-9-]{3,39})",
    re.IGNORECASE,
)


class SessionCheck(BaseModel):
    """State of a live review page. Contains field labels only, never values."""

    session_open: bool
    page_url: str = ""
    blockers: list[Blocker] = Field(default_factory=list)
    invalid_fields: list[str] = Field(default_factory=list)
    submit_controls: int = 0


class SubmitResult(BaseModel):
    outcome: str
    check: SessionCheck
    http_status: int | None = None
    confirmation_text_found: bool = False
    confirmation_number: str | None = None
    screenshots: list[str] = Field(default_factory=list)


def check_problems(check: SessionCheck) -> list[str]:
    """Reasons the page cannot be submitted; empty when it is ready."""
    if not check.session_open:
        return ["The review browser is not open; start the form again"]
    problems = [f"{blocker.kind}: {blocker.detail}" for blocker in check.blockers]
    if check.invalid_fields:
        problems.append("Required or invalid fields: " + ", ".join(check.invalid_fields[:10]))
    if check.submit_controls == 0:
        problems.append("No visible submit button was found")
    elif check.submit_controls > 1:
        problems.append("More than one submit button was found; submit this form yourself")
    return problems


def parse_confirmation(text: str) -> tuple[bool, str | None]:
    """Whether the page looks like a confirmation, and a reference number when it shows one."""
    found = bool(_THANKS.search(text))
    for match in _NUMBER.finditer(text):
        candidate = match.group(1)
        if any(char.isdigit() for char in candidate):
            return True, candidate
    return found, None


def _inspect(page: Page, portal: BasePortal) -> SessionCheck:
    try:
        raw: dict[str, Any] = page.evaluate(CHECK_SUBMIT_JS, SUBMIT_ATTRIBUTE)
        blockers = portal.detect_blockers(page)
    except PlaywrightError:
        return SessionCheck(session_open=False)
    invalid = list(dict.fromkeys(str(label) for label in raw["invalid"]))
    return SessionCheck(
        session_open=True,
        page_url=page.url,
        blockers=blockers,
        invalid_fields=invalid,
        submit_controls=int(raw["controls"]),
    )


def inspect_task(application_id: str, portal: BasePortal) -> Callable[[BrowserHost], SessionCheck]:
    def task(host: BrowserHost) -> SessionCheck:
        session = host.live_session(application_id)
        if session is None or session.page.is_closed():
            return SessionCheck(session_open=False)
        return _inspect(session.page, portal)

    return task


def submit_task(
    application_id: str, portal: BasePortal, screenshot_dir: Path
) -> Callable[[BrowserHost], SubmitResult]:
    """Click the single submit button of the live review page, once, and report what happened."""

    def task(host: BrowserHost) -> SubmitResult:
        session = host.live_session(application_id)
        if session is None or session.page.is_closed():
            return SubmitResult(outcome=SUBMIT_BLOCKED, check=SessionCheck(session_open=False))
        page = session.page
        check = _inspect(page, portal)
        if check_problems(check):
            return SubmitResult(outcome=SUBMIT_BLOCKED, check=check)

        shots = [_snapshot(page, screenshot_dir, "before-submit")]
        statuses: list[int] = []

        def on_response(response: Response) -> None:
            if response.request.method.upper() not in _READ_ONLY:
                statuses.append(response.status)

        page.on("response", on_response)
        guard = session.guard
        guard.submitted_requests = 0
        guard.submit_budget = 1
        guard.block_submissions = False
        try:
            with contextlib.suppress(PlaywrightError):
                page.evaluate("() => { window.__jaAllowSubmit = true; }")
                page.locator(f'[{SUBMIT_ATTRIBUTE}="1"]').click()
                _settle(page)
        finally:
            guard.block_submissions = True
            guard.submit_budget = 0
            with contextlib.suppress(PlaywrightError):
                page.evaluate("() => { window.__jaAllowSubmit = false; }")
            page.remove_listener("response", on_response)

        text = ""
        with contextlib.suppress(PlaywrightError):
            shots.append(_snapshot(page, screenshot_dir, "after-submit"))
        with contextlib.suppress(PlaywrightError):
            text = page.inner_text("body", timeout=5000)[:20000]
        found, number = parse_confirmation(text)
        if guard.submitted_requests == 0:
            outcome = SUBMIT_NOT_SENT
        elif statuses and statuses[0] < 400:
            outcome = SUBMIT_SENT
        else:
            outcome = SUBMIT_REQUEST_FAILED
        if outcome == SUBMIT_SENT:
            host.close_session(application_id)
        return SubmitResult(
            outcome=outcome,
            check=check,
            http_status=statuses[0] if statuses else None,
            confirmation_text_found=found,
            confirmation_number=number,
            screenshots=shots,
        )

    return task
