"""Application lifecycle statuses and portal settings."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ApplicationStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    DUPLICATE = "DUPLICATE"
    ANALYZED = "ANALYZED"
    SKIPPED = "SKIPPED"
    INELIGIBLE = "INELIGIBLE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    PREPARED = "PREPARED"
    FORM_STARTED = "FORM_STARTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"
    WITHDRAWN = "WITHDRAWN"


TERMINAL_STATUSES = frozenset(
    {
        ApplicationStatus.DUPLICATE,
        ApplicationStatus.SKIPPED,
        ApplicationStatus.INELIGIBLE,
        ApplicationStatus.SUBMITTED,
        ApplicationStatus.WITHDRAWN,
    }
)


class PortalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    support_level: str = Field(pattern="^(experimental|not_validated|unsupported)$")
    validated: bool = False
    notes: str = ""


class PortalSettings(BaseModel):
    """config/portal_settings.yaml."""

    model_config = ConfigDict(extra="forbid")

    field_mapping_confidence_threshold: float = Field(default=0.85, ge=0, le=1)
    allow_login_automation: bool = False
    allow_captcha_or_mfa_bypass: bool = False
    portals: dict[str, PortalConfig]

    @field_validator("allow_captcha_or_mfa_bypass")
    @classmethod
    def _never_bypass(cls, value: bool) -> bool:
        if value:
            raise ValueError("CAPTCHA/MFA bypass is not supported and must stay false")
        return value
