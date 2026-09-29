"""A single thread that owns Playwright and any browser sessions kept open for human review.

Playwright's sync API is bound to the thread that started it, so API request threads hand work
to this thread through a queue instead of touching the browser directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any, TypeVar

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.automation.url_guard import hostname_of
from app.config import LOOPBACK_HOSTS, Settings
from app.utils.logging_config import get_logger

T = TypeVar("T")

MAX_LIVE_SESSIONS = 3
TASK_TIMEOUT_SECONDS = 180.0
_IDLE_POLL_SECONDS = 0.25
_READ_ONLY_METHODS = frozenset({"GET", "HEAD"})
INSTALL_HINT = (
    "Run 'python scripts/install_playwright.py', or set PLAYWRIGHT_BROWSER_CHANNEL to an "
    "installed 'msedge' or 'chrome'."
)


class BrowserUnavailableError(RuntimeError):
    pass


class BrowserTimeoutError(RuntimeError):
    pass


class SessionLimitError(RuntimeError):
    pass


@dataclass
class GuardState:
    """Non-GET requests are aborted unless block_submissions is false and budget remains."""

    block_submissions: bool = True
    submit_budget: int = 0
    submitted_requests: int = 0


@dataclass
class LiveSession:
    application_id: str
    context: BrowserContext
    page: Page
    guard: GuardState
    expires_at: float = field(default=0.0)


def _route_guard(allowed_hosts: frozenset[str], guard: GuardState) -> Callable[[Route], None]:
    def handler(route: Route) -> None:
        request = route.request
        host = hostname_of(request.url)
        if request.method.upper() not in _READ_ONLY_METHODS:
            allowed = (
                not guard.block_submissions and guard.submit_budget > 0 and host in allowed_hosts
            )
            if not allowed:
                route.abort("blockedbyclient")
                return
            guard.submit_budget -= 1
            guard.submitted_requests += 1
        top_level = request.is_navigation_request() and request.frame.parent_frame is None
        if top_level and host not in allowed_hosts:
            route.abort("blockedbyclient")
            return
        route.continue_()

    return handler


class BrowserHost:
    """Runs on the worker thread only."""

    def __init__(self, playwright: Playwright, settings: Settings) -> None:
        self.playwright = playwright
        self.settings = settings
        self.sessions: dict[str, LiveSession] = {}
        self._browser: Browser | None = None
        self.log = get_logger("app.automation.browser")

    def _launch(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        channel = self.settings.playwright_browser_channel
        # Local-only runs need no proxy; system proxy auto-detection delayed each page by seconds.
        local_only = self.settings.browser_allowed_hosts == LOOPBACK_HOSTS
        try:
            self._browser = self.playwright.chromium.launch(
                headless=self.settings.playwright_headless,
                slow_mo=self.settings.playwright_slow_mo,
                channel=None if channel == "chromium" else channel,
                args=["--no-proxy-server"] if local_only else [],
            )
        except PlaywrightError as exc:
            raise BrowserUnavailableError(f"Could not start the browser. {INSTALL_HINT}") from exc
        return self._browser

    def new_context(self, guard: GuardState) -> BrowserContext:
        context = self._launch().new_context(
            accept_downloads=False,
            service_workers="block",
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        context.set_default_timeout(self.settings.playwright_timeout)
        context.set_default_navigation_timeout(self.settings.playwright_timeout)
        context.route("**/*", _route_guard(self.settings.browser_allowed_hosts, guard))
        return context

    def keep(self, session: LiveSession) -> None:
        """Retain a session for review, replacing any earlier one for the same application."""
        self.close_session(session.application_id)
        self.reap_expired()
        if len(self.sessions) >= MAX_LIVE_SESSIONS:
            raise SessionLimitError(
                f"{MAX_LIVE_SESSIONS} review sessions are already open; close one first"
            )
        session.expires_at = time.monotonic() + self.settings.playwright_session_timeout
        self.sessions[session.application_id] = session

    def has_session(self, application_id: str) -> bool:
        self.reap_expired()
        return application_id in self.sessions

    def live_session(self, application_id: str) -> LiveSession | None:
        """Return the open review session and restart its expiry clock."""
        self.reap_expired()
        session = self.sessions.get(application_id)
        if session is not None:
            session.expires_at = time.monotonic() + self.settings.playwright_session_timeout
        return session

    def close_session(self, application_id: str) -> bool:
        session = self.sessions.pop(application_id, None)
        if session is None:
            return False
        self._close_context(session.context)
        return True

    @staticmethod
    def _close_context(context: BrowserContext) -> None:
        with contextlib.suppress(PlaywrightError):
            context.close()

    def reap_expired(self) -> None:
        now = time.monotonic()
        for application_id, session in list(self.sessions.items()):
            if session.expires_at <= now or session.page.is_closed():
                self.close_session(application_id)
                self.log.info("review session closed", extra={"application_id": application_id})

    def pump(self) -> None:
        """Let Playwright process events (for example a window closed by the user)."""
        for session in self.sessions.values():
            if not session.page.is_closed():
                with contextlib.suppress(PlaywrightError):
                    session.page.wait_for_timeout(1)
            break

    def close_all(self) -> None:
        for application_id in list(self.sessions):
            self.close_session(application_id)
        if self._browser is not None:
            with contextlib.suppress(PlaywrightError):
                self._browser.close()
            self._browser = None


class BrowserWorker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._queue: queue.Queue[tuple[Callable[[BrowserHost], Any], Future[Any]] | None] = (
            queue.Queue()
        )
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread is None:
                self._ready.clear()
                self._startup_error = None
                self._thread = threading.Thread(
                    target=self._run, name="browser-worker", daemon=True
                )
                self._thread.start()
        self._ready.wait()
        if self._startup_error is not None:
            error, self._startup_error = self._startup_error, None
            with self._lock:
                self._thread = None
            raise BrowserUnavailableError(f"Playwright could not start. {INSTALL_HINT}") from error

    def _run(self) -> None:
        try:
            if sys.platform == "win32":
                # Playwright spawns a subprocess, which a selector event loop cannot do on Windows.
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            playwright = sync_playwright().start()
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            return
        host = BrowserHost(playwright, self.settings)
        self._ready.set()
        while True:
            try:
                item = self._queue.get(timeout=_IDLE_POLL_SECONDS)
            except queue.Empty:
                host.reap_expired()
                host.pump()
                continue
            if item is None:
                break
            task, future = item
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(task(host))
                except BaseException as exc:
                    future.set_exception(exc)
            host.reap_expired()
        host.close_all()
        playwright.stop()

    def run(self, task: Callable[[BrowserHost], T], timeout: float = TASK_TIMEOUT_SECONDS) -> T:
        self._ensure_started()
        future: Future[T] = Future()
        self._queue.put((task, future))
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            raise BrowserTimeoutError("The browser did not finish in time") from exc

    def has_session(self, application_id: str) -> bool:
        if self._thread is None:
            return False
        return self.run(lambda host: host.has_session(application_id), timeout=30)

    def close_session(self, application_id: str) -> bool:
        if self._thread is None:
            return False
        return self.run(lambda host: host.close_session(application_id), timeout=30)

    def shutdown(self) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is None:
            return
        self._queue.put(None)
        thread.join(timeout=20)
