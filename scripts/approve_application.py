"""Human approval of one application: review the summary, then type the exact approval text."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


class ApiClient(Protocol):
    """The part of an HTTP client this script uses (httpx and the FastAPI test client fit)."""

    def post(self, url: str, *, json: Any = ...) -> Any: ...


def _error_lines(response: Any) -> list[str]:
    try:
        detail: Any = response.json().get("detail")
    except ValueError:
        return [response.text[:200]]
    if isinstance(detail, dict):
        reasons = detail.get("reasons")
        if isinstance(reasons, list):
            return [str(reason) for reason in reasons]
        return [str(detail.get("message", detail))]
    return [str(detail)]


def approve(
    client: ApiClient,
    application_id: str,
    ask: Callable[[str], str],
    say: Callable[[str], None],
) -> int:
    """Return 0 only when the application was submitted."""
    base = f"/applications/{application_id}/approval"
    response = client.post(f"{base}/request")
    if response.status_code != 200:
        say(f"Cannot approve {application_id} (HTTP {response.status_code}):")
        for line in _error_lines(response):
            say(f"  - {line}")
        return 1
    challenge = response.json()
    review = challenge["review"]
    say(f"Application {application_id}: {review['job_title']} at {review['company']}")
    say(f"Portal: {review['portal']}    Page: {review['url']}")
    say(
        f"Filled {review['filled']}, uploaded {review['uploaded']}, "
        f"left blank {review['left_blank']}."
    )
    if review["manual_fields"]:
        say("Fields that needed you (check that they are filled in the browser window):")
        for label in review["manual_fields"]:
            say(f"  - {label}")
    for shot in review["screenshots"]:
        say(f"Screenshot: {shot}")
    say(f"This approval expires in {challenge['expires_in_seconds'] // 60} minutes.")

    required = challenge["required_text"]
    typed = ask(f'Review the form in the browser. Type exactly "{required}" to submit')
    if typed != required:
        client.post(f"{base}/cancel")
        say("Cancelled. Nothing was submitted.")
        return 1

    response = client.post(
        f"{base}/confirm",
        json={"approval_token": challenge["approval_token"], "approval_text": typed},
    )
    if response.status_code != 200:
        say(f"Not submitted (HTTP {response.status_code}):")
        for line in _error_lines(response):
            say(f"  - {line}")
        return 1
    outcome = response.json()
    say(outcome["message"])
    if outcome["confirmation_number"]:
        say(f"Confirmation number: {outcome['confirmation_number']}")
    say(f"Status: {outcome['application_status']}")
    return 0 if outcome["submitted"] else 1
