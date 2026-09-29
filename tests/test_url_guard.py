from __future__ import annotations

import pytest

from app.automation.url_guard import (
    UrlNotAllowedError,
    check_portal_enabled,
    check_url,
    hostname_of,
)
from app.models.application import PortalSettings

LOCAL = frozenset({"127.0.0.1", "localhost", "::1"})


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8123/apply.html", "http://localhost/apply", "http://[::1]:9000/x"],
)
def test_loopback_urls_are_allowed(url: str) -> None:
    assert check_url(url, LOCAL) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.example.com/apply",
        "file:///C:/secret.txt",
        "javascript:alert(1)",
        "ftp://127.0.0.1/x",
        "http://user:pass@127.0.0.1/x",
        "http://127.0.0.1.evil.example/x",
        "not a url",
        "",
    ],
)
def test_other_urls_are_refused(url: str) -> None:
    with pytest.raises(UrlNotAllowedError):
        check_url(url, LOCAL)


def test_opted_in_hosts_are_allowed() -> None:
    hosts = LOCAL | {"careers.example.com"}
    assert check_url("https://careers.example.com/a", hosts)
    with pytest.raises(UrlNotAllowedError):
        check_url("https://other.example.com/a", hosts)


def test_hostname_is_lowercased() -> None:
    assert hostname_of("HTTP://LocalHost:80/x") == "localhost"
    assert hostname_of("nonsense") is None


def portals(**enabled: bool) -> PortalSettings:
    return PortalSettings.model_validate(
        {
            "portals": {
                name: {"enabled": flag, "support_level": "experimental"}
                for name, flag in enabled.items()
            }
        }
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.wd5.myworkdayjobs.com/en-US/careers",
        "https://boards.greenhouse.io/acme/jobs/1",
        "https://jobs.lever.co/acme/1",
    ],
)
def test_disabled_portals_are_routed_to_manual_review(url: str) -> None:
    config = portals(workday=False, greenhouse=False, lever=False, generic=True)
    with pytest.raises(UrlNotAllowedError, match="manual review"):
        check_portal_enabled(url, config)


def test_enabled_portal_and_unknown_hosts_pass() -> None:
    check_portal_enabled("https://boards.greenhouse.io/a", portals(greenhouse=True))
    check_portal_enabled("http://127.0.0.1/x", portals(workday=False))
