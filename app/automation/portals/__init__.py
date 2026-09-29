"""Portal adapters. Only the generic, label-based adapter exists; no live portal is validated."""

from __future__ import annotations

from app.automation.portals.base import BasePortal, Blocker
from app.automation.portals.generic import GenericPortal
from app.automation.url_guard import UrlNotAllowedError
from app.models.application import PortalSettings

__all__ = ["BasePortal", "Blocker", "GenericPortal", "select_portal"]


def select_portal(portals: PortalSettings) -> BasePortal:
    config = portals.portals.get(GenericPortal.name)
    if config is None or not config.enabled:
        raise UrlNotAllowedError("The generic portal adapter is disabled in portal_settings.yaml")
    return GenericPortal()
