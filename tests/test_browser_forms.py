"""End-to-end tests against the synthetic local site. They need an installed browser.

The bundled Chromium is used when present, otherwise Microsoft Edge or Chrome; the tests are
skipped when none can be launched.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from docx import Document
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from app.automation.worker import BrowserHost, BrowserWorker
from app.config import PROJECT_ROOT, Settings
from app.database.repositories import ApplicationRepository
from app.main import create_app
from app.models.application import ApplicationStatus
from app.models.candidate import CandidateProfile

from .test_matching_api import import_jobs, job
from .test_resume_tailoring import MASTER, build_master

FIXTURES = Path(__file__).parent / "fixtures"
PAGES = {
    "mock_application.html",
    "mock_job_page.html",
    "mock_login.html",
    "mock_captcha.html",
}


@pytest.fixture(scope="session")
def browser_channel() -> str:
    with sync_playwright() as playwright:
        for channel in ("chrome", "chromium", "msedge"):
            try:
                playwright.chromium.launch(
                    headless=True, channel=None if channel == "chromium" else channel
                ).close()
            except PlaywrightError:
                continue
            return channel
    pytest.skip("no launchable browser (install Chromium, Edge or Chrome)")


class MockSite(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.posts: list[str] = []
        self.post_status = 200

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        name = self.path.split("?")[0].rsplit("/", 1)[-1]
        if name not in PAGES:
            self.send_error(404)
            return
        body = (FIXTURES / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        site = cast(MockSite, self.server)
        site.posts.append(self.path)
        body = (
            b"<html><body><h1>Thank you</h1>"
            b"<p>Your application has been received. Confirmation number: SYN-12345</p>"
            b"</body></html>"
        )
        self.send_response(site.post_status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def site() -> Iterator[MockSite]:
    server = MockSite()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def browser_settings(tmp_path: Path, browser_channel: str) -> Settings:
    config = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", config)
    shutil.copy(config / "candidate_profile.example.yaml", config / "candidate_profile.yaml")
    shutil.copy(config / "safe_answers.example.yaml", config / "safe_answers.yaml")
    return Settings.model_validate(
        {
            "project_root": tmp_path,
            "config_directory": Path("config"),
            "playwright_headless": True,
            "playwright_slow_mo": 0,
            "playwright_timeout": 10000,
            "playwright_browser_channel": browser_channel,
        }
    )


@pytest.fixture
def client(browser_settings: Settings) -> Iterator[TestClient]:
    path = browser_settings.config_dir / "candidate_profile.yaml"
    profile = CandidateProfile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    build_master(browser_settings.project_root / MASTER, profile)
    with TestClient(create_app(browser_settings)) as test_client:
        yield test_client


def prepared_application(client: TestClient, url: str) -> str:
    (job_id,) = import_jobs(client, job("Acme Networks", job_url=url))
    assert client.post(f"/matching/analyze/{job_id}").json()["result"]["recommendation"] == "APPLY"
    tailored = client.post(f"/resumes/tailor/{job_id}")
    assert tailored.status_code == 200, tailored.text
    return str(tailored.json()["application_id"])


def fastapi_app(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


def worker_of(client: TestClient) -> BrowserWorker:
    worker: BrowserWorker = fastapi_app(client).state.browser
    return worker


def in_page(client: TestClient, application_id: str, script: str, arg: Any = None) -> Any:
    def task(host: BrowserHost) -> Any:
        return host.sessions[application_id].page.evaluate(script, arg)

    return worker_of(client).run(task)


def start(client: TestClient, application_id: str, **body: Any) -> Any:
    return client.post(f"/applications/{application_id}/form/start", json=body)


# ---- job extraction -------------------------------------------------------------------------


def test_extract_reads_schema_org_posting_and_stores_it(client: TestClient, site: MockSite) -> None:
    response = client.post("/jobs/extract", json={"url": f"{site.base}/mock_job_page.html"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == 1
    assert body["extracted"]["company"] == "Synthetic Networks Ltd"
    assert body["extracted"]["title"] == "Network Security Engineer"
    assert body["extracted"]["location"] == "Springfield, IL, US"
    assert body["extracted"]["employment_type"] == "Full Time"
    stored = client.get(f"/jobs/{body['results'][0]['job_id']}").json()
    assert stored["source"] == "BROWSER"
    assert stored["status"] == "DISCOVERED"
    assert stored["description_length"] > 40


def test_extract_twice_reports_a_duplicate(client: TestClient, site: MockSite) -> None:
    url = f"{site.base}/mock_job_page.html"
    assert client.post("/jobs/extract", json={"url": url}).json()["created"] == 1
    assert client.post("/jobs/extract", json={"url": url}).json()["duplicates"] == 1


def test_extract_refuses_pages_that_need_sign_in(client: TestClient, site: MockSite) -> None:
    response = client.post("/jobs/extract", json={"url": f"{site.base}/mock_login.html"})
    assert response.status_code == 409
    assert response.json()["detail"]["manual_action_required"][0]["kind"] == "LOGIN"
    assert client.get("/jobs").json() == []


def test_extract_refuses_hosts_that_are_not_allowed(client: TestClient) -> None:
    response = client.post("/jobs/extract", json={"url": "https://jobs.example.com/1"})
    assert response.status_code == 422
    assert "PLAYWRIGHT_ALLOWED_HOSTS" in response.json()["detail"]


# ---- form preparation -----------------------------------------------------------------------


def test_form_is_filled_with_safe_fields_only_and_never_submitted(
    client: TestClient, site: MockSite, browser_settings: Settings
) -> None:
    application_id = prepared_application(client, f"{site.base}/mock_application.html")
    response = start(client, application_id)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["outcome"] == "READY_FOR_REVIEW"
    assert body["application_status"] == "AWAITING_APPROVAL"
    assert body["live_session"] is True
    assert body["blockers"] == []
    assert body["filled"] == 7 and body["uploaded"] == 1 and body["failed"] == 0

    values = in_page(
        client,
        application_id,
        """() => Object.fromEntries(
            Array.from(document.querySelectorAll('input, select, textarea'))
              .filter((el) => el.name && el.type !== 'hidden')
              .map((el) => [el.name, el.type === 'file' ? el.files.length
                : el.type === 'checkbox' || el.type === 'radio' ? el.checked : el.value]))""",
    )
    assert values["first_name"] == "Alex"
    assert values["last_name"] == "Example"
    assert values["email"] == "alex.example@example.com"
    assert values["phone"] == "+1-555-0100"
    assert values["city"] == "Springfield"
    assert values["linkedInUrl"] == "https://www.linkedin.com/in/alex-example"
    assert values["source"] == "careers"
    assert values["resume"] == 1
    for untouched in ("cover", "salary", "dob", "why", "language", "manager", "ref_email"):
        assert values[untouched] in ("", 0), untouched
    assert values["gender"] == "Select..."
    assert values["sponsor"] == "Select..."
    assert values["consent"] is False
    assert values["disabled_city"] == ""

    by_label = {f["label"]: f for f in body["fields"]}
    assert by_label["What are your salary expectations?"]["status"] == "LEFT_BLANK_SENSITIVE"
    assert by_label["Date of birth"]["category"] == "SENSITIVE"
    assert by_label["Gender"]["category"] == "DEMOGRAPHIC"
    legend = "Are you legally authorized to work in this country?"
    assert by_label[legend]["category"] == "WORK_AUTHORIZATION"
    sponsor = "Will you now or in the future require visa sponsorship?"
    assert by_label[sponsor]["category"] == "SPONSORSHIP"
    assert by_label["I agree to the privacy policy"]["status"] == "LEFT_BLANK_SENSITIVE"
    assert by_label["Language proficiency"]["status"] == "LEFT_BLANK_UNKNOWN"
    assert by_label["Reference email"]["status"] == "LEFT_BLANK_LOW_CONFIDENCE"
    assert "City (disabled)" not in by_label
    assert "alex.example" not in str(body["fields"])

    manual = {f["label"] for f in body["manual_fields"]}
    assert "What are your salary expectations?" in manual
    assert "First name *" not in manual

    assert site.posts == []
    shots = [browser_settings.project_root / p for p in body["screenshots"]]
    assert [s.name.split("-")[-1] for s in shots] == ["before.png", "after.png"]
    assert all(s.is_file() and s.stat().st_size > 1000 for s in shots)

    application = client.get(f"/applications/{application_id}/form").json()
    assert application["live_session"] is True
    assert application["fields"] == body["fields"]


def test_submission_is_blocked_even_if_the_page_is_clicked(
    client: TestClient, site: MockSite
) -> None:
    application_id = prepared_application(client, f"{site.base}/mock_application.html")
    assert start(client, application_id).status_code == 200

    def click_submit(host: BrowserHost) -> tuple[int, str]:
        page = host.sessions[application_id].page
        page.click("#submit", no_wait_after=True)
        blocked_by_page: int = page.evaluate("() => window.__jaBlockedSubmits || 0")
        outcome: str = page.evaluate(
            "() => fetch('/submit', {method: 'POST', body: 'x'})"
            ".then(() => 'sent', () => 'blocked')"
        )
        return blocked_by_page, outcome

    blocked_by_page, outcome = worker_of(client).run(click_submit)
    assert blocked_by_page >= 1
    assert outcome == "blocked"
    assert site.posts == []


def test_the_mock_site_records_a_post_when_no_guard_is_present(
    client: TestClient, site: MockSite
) -> None:
    """Guards the guard test: an unguarded context really does reach the server."""

    def unguarded_post(host: BrowserHost) -> None:
        context = host._launch().new_context()
        try:
            page = context.new_page()
            page.goto(f"{site.base}/mock_login.html")
            page.evaluate("() => fetch('/submit', {method: 'POST', body: 'x'})")
        finally:
            context.close()

    worker_of(client).run(unguarded_post)
    assert site.posts == ["/submit"]


def test_resume_is_the_tailored_copy_not_the_master(
    client: TestClient, site: MockSite, browser_settings: Settings
) -> None:
    application_id = prepared_application(client, f"{site.base}/mock_application.html")
    start(client, application_id)
    name = in_page(client, application_id, "() => document.getElementById('resume').files[0].name")
    assert name == "alex-example-resume.docx"
    generated = browser_settings.generated_resumes_dir / application_id / name
    assert Document(str(generated)).paragraphs
    assert name != Path(MASTER).name


@pytest.mark.parametrize(
    ("page", "kind"), [("mock_login.html", "LOGIN"), ("mock_captcha.html", "CAPTCHA")]
)
def test_login_and_captcha_pages_stop_the_run(
    client: TestClient, site: MockSite, browser_settings: Settings, page: str, kind: str
) -> None:
    application_id = prepared_application(client, f"{site.base}/{page}")
    body = start(client, application_id).json()
    assert body["outcome"] == "BLOCKED"
    assert body["blockers"][0]["kind"] == kind
    assert body["application_status"] == "FAILED"
    assert body["live_session"] is False
    assert body["fields"] == [] and body["filled"] == 0
    assert (browser_settings.project_root / body["screenshots"][0]).is_file()
    assert site.posts == []
    assert worker_of(client).has_session(application_id) is False


def test_status_history_records_each_step(client: TestClient, site: MockSite) -> None:
    application_id = prepared_application(client, f"{site.base}/mock_application.html")
    start(client, application_id)
    with fastapi_app(client).state.session_factory() as session:
        application = ApplicationRepository(session).get(application_id)
        assert application is not None
        trail = [h.to_status for h in application.history]
        assert trail == ["ANALYZED", "PREPARED", "FORM_STARTED", "AWAITING_APPROVAL"]
        assert application.portal == "generic"
        assert application.application_started_at is not None
        assert application.awaiting_approval_at is not None
        assert application.submitted_at is None


def test_starting_again_replaces_the_review_session(client: TestClient, site: MockSite) -> None:
    application_id = prepared_application(client, f"{site.base}/mock_application.html")
    assert start(client, application_id).status_code == 200
    second = start(client, application_id)
    assert second.status_code == 200
    assert second.json()["application_status"] == "AWAITING_APPROVAL"
    count = worker_of(client).run(lambda host: len(host.sessions))
    assert count == 1


def test_close_ends_the_review_session_without_changing_status(
    client: TestClient, site: MockSite
) -> None:
    application_id = prepared_application(client, f"{site.base}/mock_application.html")
    start(client, application_id)
    closed = client.post(f"/applications/{application_id}/form/close").json()
    assert closed == {"application_id": application_id, "closed": True}
    latest = client.get(f"/applications/{application_id}/form").json()
    assert latest["live_session"] is False
    assert latest["application_status"] == "AWAITING_APPROVAL"
    assert client.post(f"/applications/{application_id}/form/close").json()["closed"] is False


def test_idle_sessions_expire(client: TestClient, site: MockSite) -> None:
    application_id = prepared_application(client, f"{site.base}/mock_application.html")
    start(client, application_id)

    def expire(host: BrowserHost) -> bool:
        host.sessions[application_id].expires_at = 0.0
        return host.has_session(application_id)

    assert worker_of(client).run(expire) is False


def test_session_limit_is_enforced(client: TestClient, site: MockSite) -> None:
    ids: list[str] = []
    for index in range(4):
        record = job(f"Company {index}", job_url=f"{site.base}/c{index}/mock_application.html")
        (job_id,) = import_jobs(client, record)
        client.post(f"/matching/analyze/{job_id}")
        ids.append(client.post(f"/resumes/tailor/{job_id}").json()["application_id"])
    codes = [start(client, i).status_code for i in ids]
    assert codes == [200, 200, 200, 409]


def test_only_prepared_applications_can_be_opened(client: TestClient, site: MockSite) -> None:
    (job_id,) = import_jobs(
        client, job("Acme Networks", job_url=f"{site.base}/mock_application.html")
    )
    client.post(f"/matching/analyze/{job_id}")
    resume = client.post(f"/resumes/tailor/{job_id}").json()
    assert resume["status"] == "PREPARED"
    with fastapi_app(client).state.session_factory() as session:
        repo = ApplicationRepository(session)
        repo.set_status(resume["application_id"], ApplicationStatus.SUBMITTED, "test")
        session.commit()
    assert start(client, resume["application_id"]).status_code == 409


def test_unknown_application_and_missing_run(client: TestClient) -> None:
    assert start(client, "APP-20260101-0001").status_code == 404
    assert client.get("/applications/APP-20260101-0001/form").status_code == 404


def test_disallowed_application_url_is_rejected(client: TestClient, site: MockSite) -> None:
    application_id = prepared_application(client, f"{site.base}/mock_application.html")
    response = start(client, application_id, application_url="https://jobs.example.com/apply")
    assert response.status_code == 422
    assert client.get(f"/applications/{application_id}/form").status_code == 404


def test_example_answers_are_not_used_on_opted_in_hosts(
    browser_settings: Settings, tmp_path: Path
) -> None:
    (browser_settings.config_dir / "safe_answers.yaml").unlink()
    settings = browser_settings.model_copy(update={"playwright_allowed_hosts": "jobs.example.com"})
    profile_path = settings.config_dir / "candidate_profile.yaml"
    profile = CandidateProfile.model_validate(yaml.safe_load(profile_path.read_text("utf-8")))
    build_master(settings.project_root / MASTER, profile)
    with TestClient(create_app(settings)) as client:
        application_id = prepared_application(client, "https://jobs.example.com/apply")
        response = start(client, application_id)
        assert response.status_code == 409
        assert "safe_answers.yaml" in response.json()["detail"]


def test_endpoints_require_the_api_key(browser_settings: Settings) -> None:
    keyed = browser_settings.model_copy(update={"api_key": "k"})
    with TestClient(create_app(keyed)) as client:
        assert client.post("/jobs/extract", json={"url": "http://127.0.0.1/"}).status_code == 401
        assert client.post("/applications/X/form/start", json={}).status_code == 401
        assert client.get("/applications/X/form").status_code == 401
        assert client.post("/applications/X/form/close").status_code == 401


def test_a_browser_that_cannot_start_gives_a_clear_error(
    client: TestClient, site: MockSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    application_id = prepared_application(client, f"{site.base}/mock_application.html")

    def unavailable() -> None:
        raise RuntimeError("no driver")

    monkeypatch.setattr("app.automation.worker.sync_playwright", unavailable)
    response = start(client, application_id)
    assert response.status_code == 503
    assert "install_playwright" in response.json()["detail"]
    with fastapi_app(client).state.session_factory() as session:
        application = ApplicationRepository(session).get(application_id)
        assert application is not None
        assert application.application_status == "PREPARED"
