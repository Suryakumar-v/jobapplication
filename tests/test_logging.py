from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.utils.logging_config import audit, get_logger, setup_logging, shutdown_logging


@pytest.fixture
def log_dir(tmp_path: Path) -> Iterator[Path]:
    directory = tmp_path / "logs"
    setup_logging(directory)
    yield directory
    shutdown_logging()


def _lines(path: Path) -> list[dict[str, str]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_application_log_written_and_secrets_redacted(log_dir: Path) -> None:
    get_logger("app.test").info(
        "login password=hunter2 for user@example.com",
        extra={"operation": "test", "url": "https://x.example.com/p?token=abc"},
    )
    shutdown_logging()
    record = _lines(log_dir / "application.log")[0]
    assert "hunter2" not in record["message"]
    assert "user@example.com" not in record["message"]
    assert record["url"] == "https://x.example.com/p"
    assert record["operation"] == "test"


def test_browser_error_and_audit_routing(log_dir: Path) -> None:
    get_logger("app.browser").info("navigated")
    get_logger("app.services").error("boom")
    audit("approval", "rejected", application_id="APP-20260929-0001")
    shutdown_logging()
    assert [r["message"] for r in _lines(log_dir / "browser.log")] == ["navigated"]
    assert [r["message"] for r in _lines(log_dir / "error.log")] == ["boom"]
    audit_records = _lines(log_dir / "audit.log")
    assert audit_records[0]["operation"] == "approval"
    assert audit_records[0]["result"] == "rejected"
    application_messages = [r["message"] for r in _lines(log_dir / "application.log")]
    assert "approval" not in application_messages


def test_setup_is_idempotent(tmp_path: Path) -> None:
    directory = tmp_path / "logs"
    setup_logging(directory)
    setup_logging(directory)
    get_logger("app.test").info("once")
    shutdown_logging()
    assert len(_lines(directory / "application.log")) == 1
