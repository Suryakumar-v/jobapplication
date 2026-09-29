"""Approval and submission logic with a fake browser worker (no real browser needed)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.automation.portals.base import Blocker
from app.automation.tasks import (
    SUBMIT_NOT_SENT,
    SUBMIT_REQUEST_FAILED,
    SUBMIT_SENT,
    SessionCheck,
    SubmitResult,
    check_problems,
    parse_confirmation,
)
from app.automation.worker import BrowserTimeoutError
from app.config import PROJECT_ROOT, Settings
from app.database.base import utcnow
from app.database.repositories import ApplicationRepository, JobRepository
from app.database.tables import ApplicationRecord, ApprovalRecord, FormRunRecord
from app.main import create_app
from app.models.application import ApplicationStatus
from app.services import approval_service

from .test_database import make_job

FORM_URL = "http://127.0.0.1:9/apply"


class FakeWorker:
    def __init__(self) -> None:
        self.check = SessionCheck(session_open=True, page_url=FORM_URL, submit_controls=1)
        self.result = SubmitResult(
            outcome=SUBMIT_SENT,
            check=self.check,
            http_status=200,
            confirmation_text_found=True,
            confirmation_number="SYN-12345",
            screenshots=["after.png"],
        )
        self.submits = 0
        self.times_out = False

    def run(self, task: Callable[..., Any], timeout: float = 180.0) -> Any:
        name = task.__qualname__
        if "inspect_task" in name:
            return self.check
        if "submit_task" in name:
            self.submits += 1
            if self.times_out:
                raise BrowserTimeoutError("The browser did not finish in time")
            return self.result
        raise AssertionError(name)

    def has_session(self, application_id: str) -> bool:
        return self.check.session_open

    def close_session(self, application_id: str) -> bool:
        return True

    def shutdown(self) -> None:
        return None


def seed(
    factory: sessionmaker[Session],
    suffix: str,
    *,
    status: ApplicationStatus = ApplicationStatus.AWAITING_APPROVAL,
    url: str = FORM_URL,
) -> str:
    with factory.begin() as session:
        job = JobRepository(session).add(make_job(suffix))
        applications = ApplicationRepository(session)
        application = applications.create_for_job(job)
        application.portal = "generic"
        applications.set_status(application.application_id, status)
        session.add(
            FormRunRecord(
                application_id=application.application_id,
                portal="generic",
                url=url,
                outcome="READY_FOR_REVIEW",
            )
        )
        return application.application_id


@pytest.fixture
def factory_and_client(
    settings: Settings, session_factory: sessionmaker[Session]
) -> Iterator[tuple[sessionmaker[Session], TestClient, FakeWorker]]:
    with TestClient(create_app(settings)) as test_client:
        fake = FakeWorker()
        cast(FastAPI, test_client.app).state.browser = fake
        yield session_factory, test_client, fake


def approval_url(application_id: str, action: str = "") -> str:
    return f"/applications/{application_id}/approval" + (f"/{action}" if action else "")


def ask(client: TestClient, application_id: str) -> dict[str, Any]:
    response = client.post(approval_url(application_id, "request"))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def confirm(client: TestClient, application_id: str, token: str, text: str | None = None) -> Any:
    return client.post(
        approval_url(application_id, "confirm"),
        json={
            "approval_token": token,
            "approval_text": approval_service.required_text(application_id)
            if text is None
            else text,
        },
    )


def status_of(factory: sessionmaker[Session], application_id: str) -> ApplicationRecord:
    with factory() as session:
        record = ApplicationRepository(session).get(application_id)
        assert record is not None
        return record


def outcomes(client: TestClient, application_id: str) -> list[str]:
    attempts = client.get(approval_url(application_id)).json()["attempts"]
    return [a["outcome"] for a in reversed(attempts)]


# ---- request ---------------------------------------------------------------------------------


def test_request_returns_exact_text_and_a_single_use_token(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
) -> None:
    factory, client, _ = factory_and_client
    application_id = seed(factory, "1")
    challenge = ask(client, application_id)
    assert challenge["required_text"] == f"SUBMIT {application_id}"
    assert challenge["expires_in_seconds"] == 3600
    assert len(challenge["approval_token"]) >= 40
    assert challenge["review"]["company"] == "Synthetic Networks Ltd"
    with factory() as session:
        (record,) = session.scalars(select(ApprovalRecord)).all()
        assert record.token_hash != challenge["approval_token"]
        assert len(record.token_hash) == 64
        assert record.consumed_at is None
        assert record.expires_at - record.created_at == timedelta(seconds=3600)


def test_request_is_refused_unless_awaiting_approval(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
) -> None:
    factory, client, _ = factory_and_client
    application_id = seed(factory, "1", status=ApplicationStatus.PREPARED)
    response = client.post(approval_url(application_id, "request"))
    assert response.status_code == 409
    assert "AWAITING_APPROVAL" in response.json()["detail"]["reasons"][0]
    assert client.post(approval_url("APP-19700101-0001", "request")).status_code == 404


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"session_open": False}, "review browser is not open"),
        ({"invalid_fields": ["Email address"]}, "Email address"),
        ({"blockers": [Blocker(kind="CAPTCHA", detail="A CAPTCHA widget is present")]}, "CAPTCHA"),
        ({"submit_controls": 0}, "No visible submit button"),
        ({"submit_controls": 2}, "More than one submit button"),
        ({"page_url": "http://localhost:9/apply"}, "no longer on the prepared form's site"),
    ],
)
def test_request_rechecks_the_live_page(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
    change: dict[str, Any],
    expected: str,
) -> None:
    factory, client, fake = factory_and_client
    application_id = seed(factory, "1")
    fake.check = fake.check.model_copy(update=change)
    response = client.post(approval_url(application_id, "request"))
    assert response.status_code == 409
    assert expected in " ".join(response.json()["detail"]["reasons"])
    with factory() as session:
        assert session.scalars(select(ApprovalRecord)).all() == []
    assert outcomes(client, application_id) == ["REQUEST_REFUSED"]


def test_unvalidated_portal_is_refused_off_the_local_machine(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
) -> None:
    factory, client, fake = factory_and_client
    url = "https://jobs.example.org/apply"
    application_id = seed(factory, "1", url=url)
    fake.check = fake.check.model_copy(update={"page_url": url})
    response = client.post(approval_url(application_id, "request"))
    assert response.status_code == 409
    assert "not marked validated" in response.json()["detail"]["reasons"][0]


def test_a_new_request_supersedes_the_previous_token(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
) -> None:
    factory, client, fake = factory_and_client
    application_id = seed(factory, "1")
    first = ask(client, application_id)["approval_token"]
    second = ask(client, application_id)["approval_token"]
    assert confirm(client, application_id, first).status_code == 403
    assert fake.submits == 0
    assert confirm(client, application_id, second).status_code == 200


# ---- confirm: accepted -----------------------------------------------------------------------


def test_confirm_submits_once_and_records_everything(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
) -> None:
    factory, client, fake = factory_and_client
    application_id = seed(factory, "1")
    token = ask(client, application_id)["approval_token"]
    response = confirm(client, application_id, token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["submitted"] is True
    assert body["application_status"] == "SUBMITTED"
    assert body["confirmation_number"] == "SYN-12345"
    assert body["screenshots"] == [f"screenshots/{application_id}/after.png"]
    assert fake.submits == 1
    record = status_of(factory, application_id)
    assert record.application_status == "SUBMITTED"
    assert record.approved_at is not None
    assert record.submitted_at is not None
    assert record.confirmation_number == "SYN-12345"
    assert "HTTP 200" in (record.notes or "")
    assert outcomes(client, application_id) == ["REQUESTED", "APPROVED", "SUBMITTED"]
    assert token not in client.get(approval_url(application_id)).text


# ---- confirm: rejected -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        str.lower,
        lambda text: text + " ",
        lambda text: " " + text,
        lambda text: text.replace(" ", "  "),
        lambda text: text[:-1] + "9",
        lambda text: "SUBMIT",
        lambda text: "yes",
        lambda text: "",
    ],
)
def test_only_the_exact_text_is_accepted(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
    mutate: Callable[[str], str],
) -> None:
    factory, client, fake = factory_and_client
    application_id = seed(factory, "1")
    token = ask(client, application_id)["approval_token"]
    wrong = mutate(approval_service.required_text(application_id))
    assert wrong != approval_service.required_text(application_id)
    response = confirm(client, application_id, token, wrong)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "text_mismatch"
    assert fake.submits == 0
    assert status_of(factory, application_id).application_status == "AWAITING_APPROVAL"
    assert confirm(client, application_id, token).status_code == 200


def test_three_wrong_texts_void_the_approval(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
) -> None:
    factory, client, fake = factory_and_client
    application_id = seed(factory, "1")
    token = ask(client, application_id)["approval_token"]
    for _ in range(approval_service.MAX_TEXT_ATTEMPTS):
        assert confirm(client, application_id, token, "nope").status_code == 403
    response = confirm(client, application_id, token)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "already_used"
    assert fake.submits == 0
    assert ask(client, application_id)["approval_token"] != token


def test_a_token_cannot_be_replayed(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
) -> None:
    factory, client, fake = factory_and_client
    application_id = seed(factory, "1")
    token = ask(client, application_id)["approval_token"]
    assert confirm(client, application_id, token).status_code == 200
    again = confirm(client, application_id, token)
    assert again.status_code == 403
    assert again.json()["detail"]["code"] == "already_used"
    assert fake.submits == 1
    assert "REJECTED_REPLAY" in outcomes(client, application_id)


def test_an_expired_approval_is_rejected(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
) -> None:
    factory, client, fake = factory_and_client
    application_id = seed(factory, "1")
    token = ask(client, application_id)["approval_token"]
    with factory.begin() as session:
        (record,) = session.scalars(select(ApprovalRecord)).all()
        record.expires_at = utcnow() - timedelta(seconds=1)
    response = confirm(client, application_id, token)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "expired"
    assert fake.submits == 0
    assert client.get(approval_url(application_id)).json()["pending"] is False


def test_a_token_only_works_for_its_own_application(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
) -> None:
    factory, client, fake = factory_and_client
    first = seed(factory, "1")
    second = seed(factory, "2")
    token = ask(client, first)["approval_token"]
    response = confirm(client, second, token)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "unknown_approval"
    assert confirm(client, second, "x" * 43).status_code == 403
    assert fake.submits == 0


def test_cancel_voids_the_pending_approval(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
) -> None:
    factory, client, fake = factory_and_client
    application_id = seed(factory, "1")
    token = ask(client, application_id)["approval_token"]
    assert client.post(approval_url(application_id, "cancel")).json()["cancelled"] == 1
    assert client.post(approval_url(application_id, "cancel")).json()["cancelled"] == 0
    assert confirm(client, application_id, token).status_code == 403
    assert fake.submits == 0


def test_confirm_is_blocked_without_consuming_when_the_page_is_not_ready(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
) -> None:
    factory, client, fake = factory_and_client
    application_id = seed(factory, "1")
    token = ask(client, application_id)["approval_token"]
    ready = fake.check
    fake.check = ready.model_copy(update={"invalid_fields": ["Email address"]})
    response = confirm(client, application_id, token)
    assert response.status_code == 409
    assert fake.submits == 0
    fake.check = ready
    assert confirm(client, application_id, token).status_code == 200


def test_a_submission_in_progress_blocks_a_second_confirm(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
) -> None:
    factory, client, fake = factory_and_client
    application_id = seed(factory, "1")
    token = ask(client, application_id)["approval_token"]
    lock = approval_service._lock_for(application_id)
    assert lock.acquire(blocking=False)
    try:
        assert confirm(client, application_id, token).status_code == 409
    finally:
        lock.release()
    assert fake.submits == 0


# ---- confirm: submission did not succeed -----------------------------------------------------


def test_click_without_a_request_leaves_the_application_awaiting_approval(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
) -> None:
    factory, client, fake = factory_and_client
    application_id = seed(factory, "1")
    fake.result = fake.result.model_copy(update={"outcome": SUBMIT_NOT_SENT, "http_status": None})
    token = ask(client, application_id)["approval_token"]
    body = confirm(client, application_id, token).json()
    assert body["submitted"] is False
    assert body["application_status"] == "AWAITING_APPROVAL"
    assert confirm(client, application_id, token).status_code == 403
    assert ask(client, application_id)["approval_token"] != token


def test_a_failed_request_marks_the_application_failed_and_keeps_it_closed(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
) -> None:
    factory, client, fake = factory_and_client
    application_id = seed(factory, "1")
    fake.result = fake.result.model_copy(
        update={"outcome": SUBMIT_REQUEST_FAILED, "http_status": 500}
    )
    token = ask(client, application_id)["approval_token"]
    body = confirm(client, application_id, token).json()
    assert body["submitted"] is False
    assert body["application_status"] == "FAILED"
    assert "HTTP 500" in body["message"]
    record = status_of(factory, application_id)
    assert record.submitted_at is None
    assert "Check the portal" in (record.failure_reason or "")
    assert client.post(approval_url(application_id, "request")).status_code == 409
    reopened = client.post(f"/applications/{application_id}/form/start", json={})
    assert reopened.status_code == 409
    assert "already attempted" in reopened.json()["detail"]


def test_a_browser_timeout_is_reported_as_unknown_not_as_submitted(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
) -> None:
    factory, client, fake = factory_and_client
    application_id = seed(factory, "1")
    fake.times_out = True
    token = ask(client, application_id)["approval_token"]
    body = confirm(client, application_id, token).json()
    assert body["submitted"] is False
    assert body["result"] == "UNKNOWN"
    assert body["application_status"] == "FAILED"
    assert "outcome unknown" in body["message"]


# ---- security --------------------------------------------------------------------------------


def test_approval_endpoints_require_the_api_key(tmp_path: Path) -> None:
    keyed = Settings(
        project_root=tmp_path, config_directory=PROJECT_ROOT / "config", api_key="test-key"
    )
    with TestClient(create_app(keyed)) as anonymous:
        base = "/applications/APP-19700101-0001/approval"
        assert anonymous.get(base).status_code == 401
        assert anonymous.post(f"{base}/request").status_code == 401
        assert anonymous.post(f"{base}/cancel").status_code == 401
        body = {"approval_token": "t" * 30, "approval_text": "SUBMIT APP-19700101-0001"}
        assert anonymous.post(f"{base}/confirm", json=body).status_code == 401


def test_short_tokens_are_rejected_by_validation(
    factory_and_client: tuple[sessionmaker[Session], TestClient, FakeWorker],
) -> None:
    factory, client, _ = factory_and_client
    application_id = seed(factory, "1")
    assert confirm(client, application_id, "short").status_code == 422


# ---- pure helpers ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "found", "number"),
    [
        ("Thank you! Your application has been received.", True, None),
        ("Confirmation number: SYN-12345", True, "SYN-12345"),
        ("Application ID #A1B2C3D4", True, "A1B2C3D4"),
        ("Reference no. 20260929-77 will be emailed", True, "20260929-77"),
        ("application number will follow", False, None),
        ("Error: something went wrong", False, None),
        ("", False, None),
    ],
)
def test_parse_confirmation(text: str, found: bool, number: str | None) -> None:
    assert parse_confirmation(text) == (found, number)


def test_check_problems_is_empty_only_for_a_ready_page() -> None:
    assert check_problems(SessionCheck(session_open=True, submit_controls=1)) == []
    assert check_problems(SessionCheck(session_open=False)) != []
    assert check_problems(SessionCheck(session_open=True, submit_controls=0)) != []
