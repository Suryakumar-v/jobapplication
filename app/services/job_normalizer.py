"""Job normalisation and deterministic fingerprinting."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

from bs4 import BeautifulSoup

from app.models.job import JobPosting, WorkplaceType
from app.utils.hashing import sha256_text, stable_fingerprint
from app.utils.text_utils import normalize_text, normalize_whitespace

MAX_DESCRIPTION_CHARS = 50_000
MIN_HASHABLE_DESCRIPTION_CHARS = 50

TRACKING_PARAMS = frozenset(
    {
        "gclid",
        "fbclid",
        "msclkid",
        "ref",
        "referrer",
        "source",
        "src",
        "trk",
        "trackingid",
        "refid",
        "cmpid",
        "campaign",
        "gh_src",
        "lever-source",
        "lever-origin",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
    }
)
TRACKING_PREFIXES = ("utm_", "hsa_", "pk_")

_LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "company",
        "gmbh",
        "plc",
        "ag",
        "sa",
        "bv",
        "pty",
        "lp",
        "llp",
        "srl",
        "oy",
        "ab",
    }
)
_TITLE_ABBREVIATIONS = {
    "sr": "senior",
    "jr": "junior",
    "eng": "engineer",
    "engr": "engineer",
    "mgr": "manager",
    "ii": "2",
    "iii": "3",
    "iv": "4",
}
_TITLE_NOISE_WORDS = frozenset(
    {"remote", "hybrid", "onsite", "on", "site", "m", "f", "d", "w", "x", "mfd", "fmd"}
    | {"contract", "temporary", "full", "time", "part", "us", "usa", "uk", "emea", "apac"}
)
_BRACKETED = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
_TRAILING_WORKPLACE = re.compile(r"\s+[-\u2013\u2014|]\s+(remote|hybrid|onsite|on-site)\s*$", re.I)
_HTML_TAG = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")
_BLANK_LINES = re.compile(r"\n{3,}")


def normalize_url(url: str | None) -> str | None:
    """Canonical form for duplicate checks; None when missing or not a valid http(s) URL."""
    if not url or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    host = parts.hostname.lower().removeprefix("www.")
    if port is not None and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith(TRACKING_PREFIXES)
    )
    suffix = f"?{urlencode(query)}" if query else ""
    return f"https://{host}{path}{suffix}"


def normalize_company(name: str) -> str:
    tokens = normalize_text(name.replace("&", " and ")).split()
    if len(tokens) > 1 and tokens[0] == "the":
        tokens = tokens[1:]
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_title(title: str) -> str:
    def drop_noise(match: re.Match[str]) -> str:
        tokens = set(normalize_text(match.group(1)).split())
        return " " if tokens and tokens <= _TITLE_NOISE_WORDS else match.group(0)

    cleaned = _TRAILING_WORKPLACE.sub("", _BRACKETED.sub(drop_noise, title))
    tokens = normalize_text(cleaned).split()
    return " ".join(_TITLE_ABBREVIATIONS.get(token, token) for token in tokens)


def normalize_location(location: str | None) -> str:
    return normalize_text(location) if location else ""


def normalize_external_id(external_id: str | None) -> str | None:
    if external_id is None:
        return None
    cleaned = external_id.strip().casefold()
    return cleaned or None


def normalize_employment_type(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    text = normalize_text(raw)
    tokens = set(text.split())
    if "intern" in tokens or "internship" in tokens:
        return "Internship"
    if "part" in tokens:
        return "Part-time"
    if tokens & {"contract", "contractor", "temporary", "temp", "freelance"}:
        return "Contract"
    if "full" in tokens or "permanent" in tokens or "fulltime" in tokens:
        return "Full-time"
    return normalize_whitespace(raw)[:64]


def infer_workplace_type(
    declared: WorkplaceType, location: str | None, title: str | None
) -> WorkplaceType:
    """Use the declared value; otherwise look only at location and title (not the description)."""
    if declared is not WorkplaceType.UNKNOWN:
        return declared
    haystack = normalize_text(f"{location or ''} {title or ''}")
    tokens = set(haystack.split())
    if "hybrid" in tokens:
        return WorkplaceType.HYBRID
    if "remote" in tokens:
        return WorkplaceType.REMOTE
    if "onsite" in tokens or "on site" in haystack:
        return WorkplaceType.ONSITE
    return WorkplaceType.UNKNOWN


def clean_description(text: str) -> str:
    """Strip HTML, normalise whitespace, cap the length."""
    if not text:
        return ""
    if _HTML_TAG.search(text):
        text = BeautifulSoup(text, "html.parser").get_text(separator="\n")
    lines = [normalize_whitespace(line) for line in text.replace("\r", "\n").split("\n")]
    joined = _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()
    return joined[:MAX_DESCRIPTION_CHARS]


def description_hash(description: str) -> str | None:
    """Hash of the normalised description; None when it is too short to be identifying."""
    normalized = normalize_text(description)
    if len(normalized) < MIN_HASHABLE_DESCRIPTION_CHARS:
        return None
    return sha256_text(normalized)


@dataclass(frozen=True)
class NormalizedJob:
    posting: JobPosting
    normalized_url: str | None
    external_id: str | None
    normalized_company: str
    normalized_title: str
    normalized_location: str
    company_title_key: str
    description_hash: str | None
    fingerprint: str
    job_id: str


def normalize_job(posting: JobPosting) -> NormalizedJob:
    """Clean a posting and derive its deterministic fingerprint."""
    company = normalize_whitespace(posting.company)
    title = normalize_whitespace(posting.title)
    location = normalize_whitespace(posting.location) if posting.location else None
    url = posting.job_url.strip() if posting.job_url else None
    description = clean_description(posting.description)

    cleaned = posting.model_copy(
        update={
            "company": company,
            "title": title,
            "location": location or None,
            "job_url": url or None,
            "external_id": (posting.external_id or "").strip() or None,
            "employment_type": normalize_employment_type(posting.employment_type),
            "workplace_type": infer_workplace_type(posting.workplace_type, location, title),
            "description": description,
        }
    )
    norm_company = normalize_company(company)
    norm_title = normalize_title(title)
    norm_location = normalize_location(location)
    norm_url = normalize_url(url)
    external_id = normalize_external_id(cleaned.external_id)
    desc_hash = description_hash(description)
    identity = external_id or norm_url or desc_hash or ""
    fingerprint = stable_fingerprint(norm_company, norm_title, norm_location, identity)
    return NormalizedJob(
        posting=cleaned,
        normalized_url=norm_url,
        external_id=external_id,
        normalized_company=norm_company,
        normalized_title=norm_title,
        normalized_location=norm_location,
        company_title_key=f"{norm_company}|{norm_title}",
        description_hash=desc_hash,
        fingerprint=fingerprint,
        job_id=f"JOB-{fingerprint[:12].upper()}",
    )
