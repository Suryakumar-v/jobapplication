"""Approval and submission against the synthetic local site in a real browser."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from scripts.approve_application import approve

from .test_browser_forms import (  # noqa: F401
    MockSite,
    browser_channel,
    browser_settings,
    client,
    fastapi_app,
    in_page,
    prepared_application,
    site,
    start,
    worker_of,
)

FORM = "/mock_application.html"
CLICK_SUBMIT = "() => document.querySelector('#submit').click()"


def prepared_form(client: TestClient, site: MockSite) -> str:
    application_id = prepared_application(client, f"{site.base}{FORM}")
    response = start(client, application_id)
    assert response.status_code == 200, response.text
    assert response.json()["application_status"] == "AWAITING_APPROVAL"
    return application_id


def request_approval(client: TestClient, application_id: str) -> dict[str, str]:
    response = client.post(f"/applications/{application_id}/approval/request")
    assert response.status_code == 200, response.text
    body: dict[str, str] = response.json()
    return body


def test_approved_submission_sends_exactly_one_request(client: TestClient, site: MockSite) -> None:
    application_id = prepared_form(client, site)
    challenge = request_approval(client, application_id)
    text = f"SUBMIT {application_id}"
    assert challenge["required_text"] == text

    # Neither a person clicking in the review browser nor a wrong text can submit.
    in_page(client, application_id, CLICK_SUBMIT)
    time.sleep(0.5)
    assert site.posts == []
    wrong = client.post(
        f"/applications/{application_id}/approval/confirm",
        json={"approval_token": challenge["approval_token"], "approval_text": "SUBMIT"},
    )
    assert wrong.status_code == 403
    assert site.posts == []

    response = client.post(
        f"/applications/{application_id}/approval/confirm",
        json={"approval_token": challenge["approval_token"], "approval_text": text},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["submitted"] is True
    assert body["http_status"] == 200
    assert body["confirmation_number"] == "SYN-12345"
    assert body["confirmation_text_found"] is True
    assert site.posts == ["/submit"]
    assert client.get(f"/applications/{application_id}").json()["application_status"] == "SUBMITTED"
    assert client.get(f"/applications/{application_id}/form").json()["live_session"] is False
    assert not worker_of(client).has_session(application_id)
    settings = fastapi_app(client).state.settings
    names = sorted(p.name for p in (settings.screenshots_dir / application_id).iterdir())
    assert any(name.endswith("before-submit.png") for name in names)
    assert any(name.endswith("after-submit.png") for name in names)

    replay = client.post(
        f"/applications/{application_id}/approval/confirm",
        json={"approval_token": challenge["approval_token"], "approval_text": text},
    )
    assert replay.status_code == 403
    assert site.posts == ["/submit"]


def test_missing_required_field_blocks_the_request_until_fixed(
    client: TestClient, site: MockSite
) -> None:
    application_id = prepared_form(client, site)
    in_page(client, application_id, "() => { document.getElementById('email').value = ''; }")
    response = client.post(f"/applications/{application_id}/approval/request")
    assert response.status_code == 409
    assert "Email address" in " ".join(response.json()["detail"]["reasons"])
    in_page(client, application_id, "() => { document.getElementById('email').value = 'a@b.co'; }")
    request_approval(client, application_id)
    assert site.posts == []


def test_closed_review_browser_blocks_the_request(client: TestClient, site: MockSite) -> None:
    application_id = prepared_form(client, site)
    assert client.post(f"/applications/{application_id}/form/close").json()["closed"] is True
    response = client.post(f"/applications/{application_id}/approval/request")
    assert response.status_code == 409
    assert "review browser is not open" in response.json()["detail"]["reasons"][0]


def test_second_submit_button_blocks_the_request(client: TestClient, site: MockSite) -> None:
    application_id = prepared_form(client, site)
    in_page(
        client,
        application_id,
        "() => { const b = document.createElement('button'); b.type = 'submit';"
        " b.textContent = 'Save draft'; document.getElementById('application').appendChild(b); }",
    )
    response = client.post(f"/applications/{application_id}/approval/request")
    assert response.status_code == 409
    assert "More than one submit button" in " ".join(response.json()["detail"]["reasons"])


def test_server_error_marks_the_application_failed_and_the_guard_is_back_on(
    client: TestClient, site: MockSite
) -> None:
    application_id = prepared_form(client, site)
    site.post_status = 500
    challenge = request_approval(client, application_id)
    response = client.post(
        f"/applications/{application_id}/approval/confirm",
        json={
            "approval_token": challenge["approval_token"],
            "approval_text": f"SUBMIT {application_id}",
        },
    )
    body = response.json()
    assert body["submitted"] is False
    assert body["application_status"] == "FAILED"
    assert site.posts == ["/submit"]
    assert worker_of(client).has_session(application_id)
    blocked = in_page(
        client,
        application_id,
        "() => fetch('/submit', {method: 'POST'}).then(() => 'sent').catch(() => 'blocked')",
    )
    assert blocked == "blocked"
    assert site.posts == ["/submit"]


def test_command_line_approval_needs_the_typed_text(client: TestClient, site: MockSite) -> None:
    application_id = prepared_form(client, site)
    lines: list[str] = []
    assert approve(client, application_id, lambda _: "yes", lines.append) == 1
    assert site.posts == []
    assert any("Cancelled" in line for line in lines)

    lines.clear()
    typed = f"SUBMIT {application_id}"
    assert approve(client, application_id, lambda _: typed, lines.append) == 0
    assert site.posts == ["/submit"]
    assert any("SYN-12345" in line for line in lines)
