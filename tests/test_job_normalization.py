from __future__ import annotations

import pytest

from app.models.job import JobPosting, WorkplaceType
from app.services.job_normalizer import (
    clean_description,
    description_hash,
    infer_workplace_type,
    normalize_company,
    normalize_employment_type,
    normalize_job,
    normalize_title,
    normalize_url,
)


def posting(**overrides: object) -> JobPosting:
    data: dict[str, object] = {
        "company": "Synthetic Networks Ltd",
        "title": "Network Security Engineer",
        "job_url": "https://jobs.example.com/openings/12345",
        "location": "Springfield",
    }
    data.update(overrides)
    return JobPosting.model_validate(data)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://www.Jobs.Example.com/a/b/?utm_source=x&id=7#frag",
            "https://jobs.example.com/a/b?id=7",
        ),
        ("http://jobs.example.com:80/a//b/", "https://jobs.example.com/a/b"),
        ("https://jobs.example.com:8443/a", "https://jobs.example.com:8443/a"),
        ("https://jobs.example.com/a?b=2&a=1&gclid=zzz", "https://jobs.example.com/a?a=1&b=2"),
        ("https://user:pw@jobs.example.com/a", "https://jobs.example.com/a"),
        ("https://jobs.example.com", "https://jobs.example.com"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "  ",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "ftp://x.example.com/a",
        "not a url",
        "https://x.example.com:99999/a",
    ],
)
def test_invalid_urls_rejected(raw: str | None) -> None:
    assert normalize_url(raw) is None


def test_url_variants_normalize_identically() -> None:
    a = normalize_url("https://jobs.example.com/openings/12345?utm_campaign=a")
    b = normalize_url("http://www.jobs.example.com/openings/12345/#apply")
    assert a == b


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Synthetic Networks Ltd.", "synthetic networks"),
        ("The Example Corporation, Inc.", "example"),
        ("AT&T", "at and t"),
        ("Acme Co", "acme"),
        ("Inc", "inc"),
    ],
)
def test_normalize_company(raw: str, expected: str) -> None:
    assert normalize_company(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sr. Network Security Engineer", "senior network security engineer"),
        ("Network Engineer (Remote)", "network engineer"),
        ("Network Engineer (m/f/d)", "network engineer"),
        ("Network Engineer - Hybrid", "network engineer"),
        ("Network Engineer (Security)", "network engineer security"),
        ("Network Engineer II", "network engineer 2"),
        ("NETWORK   engr", "network engineer"),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Full Time", "Full-time"),
        ("full-time", "Full-time"),
        ("Permanent", "Full-time"),
        ("Part time", "Part-time"),
        ("Contractor", "Contract"),
        ("Internship", "Internship"),
        ("Seasonal", "Seasonal"),
        (None, None),
        ("  ", None),
    ],
)
def test_normalize_employment_type(raw: str | None, expected: str | None) -> None:
    assert normalize_employment_type(raw) == expected


def test_workplace_inference_uses_location_and_title_only() -> None:
    unknown = WorkplaceType.UNKNOWN
    assert infer_workplace_type(unknown, "Remote - US", "Engineer") is WorkplaceType.REMOTE
    assert infer_workplace_type(unknown, "Springfield", "Hybrid Engineer") is WorkplaceType.HYBRID
    assert infer_workplace_type(unknown, "Springfield", "Engineer") is WorkplaceType.UNKNOWN
    assert infer_workplace_type(WorkplaceType.ONSITE, "Remote", "x") is WorkplaceType.ONSITE


def test_clean_description_strips_html_and_caps_length() -> None:
    cleaned = clean_description(
        "<p>Hello <b>world</b></p><script>x()</script><ul><li>One</li></ul>"
    )
    assert "<" not in cleaned
    assert "Hello" in cleaned
    assert len(clean_description("a" * 100_000)) == 50_000
    assert clean_description("") == ""


def test_description_hash_ignores_formatting_and_short_text() -> None:
    long_text = "Required: network security, RADIUS and firewall policy management experience."
    assert description_hash(long_text) == description_hash("  " + long_text.upper() + "!!")
    assert description_hash("short") is None


def test_fingerprint_is_deterministic_and_normalization_stable() -> None:
    first = normalize_job(posting())
    second = normalize_job(
        posting(
            company="synthetic networks",
            title="Network Security Engineer (Remote)",
            job_url="http://www.jobs.example.com/openings/12345/?utm_source=a",
            location="  springfield ",
        )
    )
    assert first.fingerprint == second.fingerprint
    assert first.job_id == f"JOB-{first.fingerprint[:12].upper()}"
    assert len(first.fingerprint) == 32


def test_fingerprint_differs_for_different_jobs() -> None:
    base = normalize_job(posting()).fingerprint
    assert normalize_job(posting(title="Firewall Engineer")).fingerprint != base
    assert normalize_job(posting(location="Shelbyville")).fingerprint != base
    assert (
        normalize_job(posting(job_url="https://jobs.example.com/openings/999")).fingerprint != base
    )


def test_external_id_takes_priority_over_url_in_fingerprint() -> None:
    a = normalize_job(posting(external_id="REQ-1", job_url="https://a.example.com/1"))
    b = normalize_job(posting(external_id="req-1 ", job_url="https://b.example.com/2"))
    assert a.fingerprint == b.fingerprint


def test_normalized_job_cleans_posting_fields() -> None:
    job = normalize_job(
        posting(employment_type="full time", location="Remote", description="<p>x</p>")
    )
    assert job.posting.employment_type == "Full-time"
    assert job.posting.workplace_type is WorkplaceType.REMOTE
    assert job.posting.description == "x"
    assert job.company_title_key == "synthetic networks|network security engineer"
