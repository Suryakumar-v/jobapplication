"""Generic adapter: schema.org JSON-LD and heading based extraction, label based form discovery."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any, ClassVar

from playwright.sync_api import Page

from app.automation.form_mapping import FormField
from app.automation.portals.base import BasePortal
from app.automation.scripts import DISCOVER_FIELDS_JS, EXTRACT_JOB_JS, FIELD_ATTRIBUTE

_BLOCK_TAGS = frozenset({"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "tr"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: str) -> str:
    """Plain text from an HTML fragment; text that is already plain is returned unchanged."""
    if "<" not in value:
        return html.unescape(value).strip()
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in "".join(parser.parts).splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _named(value: Any) -> str | None:
    if isinstance(value, dict):
        return _text(value.get("name"))
    if isinstance(value, list) and value:
        return _named(value[0])
    return _text(value)


def _location(value: Any) -> str | None:
    if isinstance(value, list) and value:
        value = value[0]
    if not isinstance(value, dict):
        return _text(value)
    address = value.get("address", value)
    if isinstance(address, str):
        return _text(address)
    if not isinstance(address, dict):
        return None
    country = address.get("addressCountry")
    parts = [
        _text(address.get("addressLocality")),
        _text(address.get("addressRegion")),
        _named(country),
    ]
    return ", ".join(p for p in parts if p) or None


def _salary(base: Any, key: str) -> int | None:
    value = base.get("value") if isinstance(base, dict) else None
    number = value.get(key) if isinstance(value, dict) else None
    return int(number) if isinstance(number, int | float) and number >= 0 else None


def _employment_type(value: Any) -> str | None:
    if isinstance(value, list) and value:
        value = value[0]
    text = _text(value)
    return text.replace("_", " ").title() if text else None


def _identifier(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return _text(value)


def record_from_page_data(data: dict[str, Any], url: str) -> dict[str, Any]:
    """Build an importer record from what the page exposes. Missing facts stay missing."""
    ld = data.get("json_ld")
    record: dict[str, Any] = {"job_url": url}
    if isinstance(ld, dict):
        description = _text(ld.get("description"))
        record.update(
            {
                "title": _text(ld.get("title")),
                "company": _named(ld.get("hiringOrganization")),
                "description": html_to_text(description) if description else None,
                "location": _location(ld.get("jobLocation")),
                "employment_type": _employment_type(ld.get("employmentType")),
                "posted_date": _text(ld.get("datePosted")),
                "external_id": _identifier(ld.get("identifier")),
                "salary_min": _salary(ld.get("baseSalary"), "minValue"),
                "salary_max": _salary(ld.get("baseSalary"), "maxValue"),
            }
        )
        if str(ld.get("jobLocationType", "")).upper() == "TELECOMMUTE":
            record["workplace_type"] = "remote"
    record["title"] = (
        record.get("title")
        or _text(data.get("heading"))
        or _text(data.get("og_title"))
        or _text(data.get("page_title"))
    )
    record["company"] = record.get("company") or _text(data.get("site_name"))
    record["description"] = record.get("description") or _text(data.get("body_text"))
    return {key: value for key, value in record.items() if value is not None}


class GenericPortal(BasePortal):
    name: ClassVar[str] = "generic"

    def extract_job(self, page: Page, url: str) -> dict[str, Any]:
        data: dict[str, Any] = page.evaluate(EXTRACT_JOB_JS)
        return record_from_page_data(data, url)

    def discover_fields(self, page: Page) -> list[FormField]:
        raw: list[dict[str, Any]] = page.evaluate(DISCOVER_FIELDS_JS, FIELD_ATTRIBUTE)
        return [FormField.model_validate(item) for item in raw]
