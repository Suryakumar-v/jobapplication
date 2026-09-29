"""Decide which URLs the browser may open."""

from __future__ import annotations

from collections.abc import Collection
from urllib.parse import urlsplit

from app.models.application import PortalSettings

# Host suffixes of known portals; a portal that is disabled in portal_settings.yaml is refused.
PORTAL_HOSTS: dict[str, tuple[str, ...]] = {
    "workday": ("myworkdayjobs.com", "workday.com"),
    "greenhouse": ("greenhouse.io",),
    "lever": ("lever.co",),
}


class UrlNotAllowedError(ValueError):
    pass


def hostname_of(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def check_url(url: str, allowed_hosts: Collection[str]) -> str:
    """Return the URL when it is plain http(s), credential-free and on an allowed host."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError as exc:
        raise UrlNotAllowedError("Invalid URL") from exc
    if parts.scheme not in {"http", "https"} or not host:
        raise UrlNotAllowedError("Only http(s) URLs can be opened")
    if parts.username or parts.password:
        raise UrlNotAllowedError("URLs containing credentials are not accepted")
    if host.lower() not in allowed_hosts:
        raise UrlNotAllowedError(
            f"Host '{host.lower()}' is not allowed; add it to PLAYWRIGHT_ALLOWED_HOSTS to opt in"
        )
    return url.strip()


def check_portal_enabled(url: str, portals: PortalSettings) -> None:
    """Refuse URLs that belong to a known portal whose adapter is disabled."""
    host = hostname_of(url)
    if host is None:
        return
    for name, suffixes in PORTAL_HOSTS.items():
        if any(host == s or host.endswith(f".{s}") for s in suffixes):
            config = portals.portals.get(name)
            if config is None or not config.enabled:
                level = config.support_level if config else "unsupported"
                raise UrlNotAllowedError(
                    f"Portal '{name}' is {level}; route this job to manual review"
                )
