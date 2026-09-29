"""Deterministic, configurable job-to-candidate matching. No network, no AI, no randomness."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import IntEnum

from app.database.base import utcnow
from app.models.candidate import CandidateProfile
from app.models.job import JobPosting, SearchPreferences, WorkplaceType
from app.models.scoring import (
    MatchResult,
    Recommendation,
    ScoringWeights,
    SkillVocabulary,
)

MIN_DESCRIPTION_CHARS = 40
NEAR_MISS_YEARS = 1.0


class _Kind(IntEnum):
    NEUTRAL = 0
    PREFERRED = 1
    REQUIRED = 2


_HEADING_PREFIX = r"(?:(?:minimum|basic|required|key|core|technical|mandatory)\s+)*"
_REQUIRED_HEADING = re.compile(
    rf"{_HEADING_PREFIX}(?:qualifications|requirements|skills|experience)(?:\s+required)?"
    r"|requirements?|required|must[- ]haves?|what you(?:'ll| will)? need"
    r"|what we(?:'re| are) looking for|who you are|you have",
)
_PREFERRED_HEADING = re.compile(
    r"(?:(?:preferred|desired|additional|bonus|nice[- ]to[- ]have)\s+)+"
    r"(?:qualifications|requirements|skills|experience)?"
    r"|nice[- ]to[- ]haves?|bonus(?: points)?|a plus|pluses|preferred|desired",
)
_NEUTRAL_HEADING = re.compile(
    r"(?:key\s+)?responsibilities|what you(?:'ll| will) do"
    r"|about (?:the )?(?:role|us|team|company|job)"
    r"|overview|job description|benefits|perks|what we offer",
)
_PREFERRED_CUE = re.compile(
    r"\b(?:preferred|nice[- ]to[- ]have|a plus|is a plus|bonus|desired|ideally|advantageous"
    r"|beneficial|not (?:strictly )?(?:required|necessary))\b",
    re.IGNORECASE,
)
_REQUIRED_CUE = re.compile(r"\b(?:required|must|mandatory|minimum|essential)\b", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*\u2022\u00b7\u25aa\u2023\u25cf]|\d+[.)])\s*")
# Not after a single letter plus dot, so "U.S. citizen" and "e.g. x" stay whole.
_CLAUSE_SPLIT = re.compile(r"(?<=[.!?])(?<!\b[A-Za-z]\.)\s+|;\s*")
_ALTERNATIVE = re.compile(r"\bor\b|\bone of\b|\bany of\b|/", re.IGNORECASE)

_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|plus|or more)?(?:\s*(?:-|to)\s*\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)
_EXPERIENCE_WORD = re.compile(r"\bexperience\b|\bexp\b", re.IGNORECASE)
_AGO = re.compile(r"\bago\b", re.IGNORECASE)

# Job text: only unambiguous words (a bare "MS" could be milliseconds).
_DEGREE_LEVELS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (4, re.compile(r"\b(?:ph\.?d|doctorate|doctoral)\b", re.IGNORECASE)),
    (3, re.compile(r"\b(?:master(?:'?s|\u2019s)?|msc|m\.sc|mba|m\.?eng)\b", re.IGNORECASE)),
    (
        2,
        re.compile(
            r"\b(?:bachelor(?:'?s|\u2019s)?|bsc|b\.sc|b\.?eng|university degree|college degree)\b",
            re.IGNORECASE,
        ),
    ),
    (1, re.compile(r"\bassociate(?:'?s|\u2019s)?\b", re.IGNORECASE)),
)
# Candidate degree strings ("BSc", "M.S.", ...) are short, so abbreviations are safe here.
_CANDIDATE_DEGREE_LEVELS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (4, re.compile(r"\b(?:ph\.?d|doctor\w*|dphil)\b", re.IGNORECASE)),
    (3, re.compile(r"\b(?:master\w*|m\.?sc|mba|m\.?eng|m\.?s|m\.?a)(?![a-z])", re.IGNORECASE)),
    (2, re.compile(r"\b(?:bachelor\w*|b\.?sc|b\.?eng|b\.?s|b\.?a)(?![a-z])", re.IGNORECASE)),
    (1, re.compile(r"\b(?:associate\w*|a\.?a\.?s|a\.?s|a\.?a)(?![a-z])", re.IGNORECASE)),
)
_ANY_DEGREE = re.compile(r"\bdegree\b", re.IGNORECASE)
_DEGREE_WAIVER = re.compile(
    r"\bequivalent\b|\bor (?:related |relevant |work )?experience\b", re.IGNORECASE
)

_CLEARANCE = re.compile(
    r"\bsecurity clearance\b|\b(?:secret|top secret|ts/sci)\s+clearance\b"
    r"|\bclearance (?:is )?required\b",
    re.IGNORECASE,
)
_CITIZENSHIP = re.compile(
    r"\b(?:u\.?s\.?|united states)\s+citizen(?:ship)?\b|\bcitizenship (?:is )?required\b",
    re.IGNORECASE,
)
_SPONSORSHIP = re.compile(r"\bsponsorship\b", re.IGNORECASE)

_SENIORITY = frozenset(
    {"senior", "sr", "junior", "jr", "lead", "principal", "staff", "i", "ii", "iii"}
)
_REMOTE_IN_DESCRIPTION = re.compile(
    r"\b(?:fully|100%)\s+remote\b|\bremote[- ]first\b", re.IGNORECASE
)
_REMOTE_WORD = re.compile(r"\bremote\b", re.IGNORECASE)
_HYBRID_WORD = re.compile(r"\bhybrid\b", re.IGNORECASE)

_WORKPLACE_FIT: dict[str, dict[WorkplaceType, float]] = {
    "any": {
        WorkplaceType.REMOTE: 1.0,
        WorkplaceType.HYBRID: 1.0,
        WorkplaceType.ONSITE: 1.0,
        WorkplaceType.UNKNOWN: 1.0,
    },
    "remote": {
        WorkplaceType.REMOTE: 1.0,
        WorkplaceType.HYBRID: 0.4,
        WorkplaceType.ONSITE: 0.0,
        WorkplaceType.UNKNOWN: 0.5,
    },
    "hybrid": {
        WorkplaceType.REMOTE: 0.7,
        WorkplaceType.HYBRID: 1.0,
        WorkplaceType.ONSITE: 0.3,
        WorkplaceType.UNKNOWN: 0.5,
    },
    "onsite": {
        WorkplaceType.REMOTE: 0.5,
        WorkplaceType.HYBRID: 0.7,
        WorkplaceType.ONSITE: 1.0,
        WorkplaceType.UNKNOWN: 0.5,
    },
}


@dataclass(frozen=True)
class _Unit:
    text: str
    kind: _Kind


def _classify(description: str) -> tuple[list[_Unit], bool]:
    """Split a description into clauses tagged required / preferred / neutral."""
    units: list[_Unit] = []
    section = _Kind.NEUTRAL
    structured = False
    for raw in description.splitlines():
        line = _BULLET.sub("", raw).strip()
        if not line:
            continue
        is_bullet = line != raw.strip()
        heading = None if is_bullet else _heading_kind(line)
        if heading is not None:
            section = heading
            structured = structured or heading != _Kind.NEUTRAL
            continue
        for clause in _CLAUSE_SPLIT.split(line):
            clause = clause.strip()
            if not clause:
                continue
            if _REQUIRED_CUE.search(clause) and not _PREFERRED_CUE.search(clause):
                kind = _Kind.REQUIRED
            elif _PREFERRED_CUE.search(clause) and not _REQUIRED_CUE.search(clause):
                kind = _Kind.PREFERRED
            elif _REQUIRED_CUE.search(clause):
                kind = _Kind.REQUIRED
            else:
                kind = section
            structured = structured or kind != _Kind.NEUTRAL
            units.append(_Unit(clause, kind))
    return units, structured


def _heading_kind(line: str) -> _Kind | None:
    if len(line) > 60:
        return None
    normalised = line.rstrip(":").strip().lower().replace("\u2019", "'")
    for kind, pattern in (
        (_Kind.PREFERRED, _PREFERRED_HEADING),
        (_Kind.REQUIRED, _REQUIRED_HEADING),
        (_Kind.NEUTRAL, _NEUTRAL_HEADING),
    ):
        if pattern.fullmatch(normalised):
            return kind
    return None


def _resolve(kind: _Kind, structured: bool) -> _Kind:
    """Unlabelled mentions are soft when the posting has explicit sections, else required."""
    if kind == _Kind.NEUTRAL:
        return _Kind.PREFERRED if structured else _Kind.REQUIRED
    return kind


def _term_pattern(name: str) -> re.Pattern[str]:
    body = r"\s+".join(re.escape(part) for part in name.split())
    flags = 0 if len(name) <= 3 else re.IGNORECASE
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", flags)


class _Terms:
    """Canonical term groups with alias-aware detection."""

    def __init__(self, entries: Iterable[tuple[str, Sequence[str]]]) -> None:
        self.canonical_of: dict[str, str] = {}
        self.names: dict[str, list[str]] = {}
        for name, aliases in entries:
            all_names = [name, *aliases]
            canonical = next(
                (self.canonical_of[n.lower()] for n in all_names if n.lower() in self.canonical_of),
                name,
            )
            group = self.names.setdefault(canonical, [])
            for candidate_name in all_names:
                if candidate_name.lower() not in self.canonical_of:
                    self.canonical_of[candidate_name.lower()] = canonical
                    group.append(candidate_name)
        self._patterns = {
            canonical: [_term_pattern(n) for n in names] for canonical, names in self.names.items()
        }

    def find(self, text: str) -> set[str]:
        return {
            canonical
            for canonical, patterns in self._patterns.items()
            if any(p.search(text) for p in patterns)
        }

    def canonical_set(self, names: Iterable[str]) -> set[str]:
        return {self.canonical_of[n.lower()] for n in names if n.lower() in self.canonical_of}


def _ratio(hits: int, total: int) -> float:
    return 1.0 if total == 0 else hits / total


def _normalise(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def _title_tokens(title: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9+#]+", title.lower()) if t not in _SENIORITY}


def _candidate_degree_level(profile: CandidateProfile) -> int | None:
    levels: list[int] = []
    for education in profile.education:
        text = education.degree
        level = next(
            (lvl for lvl, pattern in _CANDIDATE_DEGREE_LEVELS if pattern.search(text)), None
        )
        if level is None:
            return None
        levels.append(level)
    return max(levels) if levels else None


def _required_degree_level(text: str) -> int | None:
    levels = [lvl for lvl, pattern in _DEGREE_LEVELS if pattern.search(text)]
    if levels:
        return min(levels)
    return 2 if _ANY_DEGREE.search(text) else None


def _max_required_years(text: str) -> float | None:
    if not _EXPERIENCE_WORD.search(text) or _AGO.search(text):
        return None
    years = [float(m.group(1)) for m in _YEARS.finditer(text)]
    return max(years) if years else None


class MatchingEngine:
    def __init__(
        self,
        candidate: CandidateProfile,
        preferences: SearchPreferences,
        weights: ScoringWeights,
        vocabulary: SkillVocabulary,
        *,
        threshold: int,
        daily_cap: int,
    ) -> None:
        self.candidate = candidate
        self.preferences = preferences
        self.weights = weights
        self.threshold = threshold
        self.daily_cap = daily_cap
        self._skills = _Terms(
            [
                *((s.name, s.aliases) for s in candidate.skills),
                *((name, []) for name in preferences.required_skills),
                *((name, []) for name in preferences.preferred_skills),
                *((e.name, e.aliases) for e in vocabulary.skills),
            ]
        )
        self._certs = _Terms(
            [
                *((c.name, c.aliases) for c in candidate.certifications),
                *((e.name, e.aliases) for e in vocabulary.certifications),
            ]
        )
        self._held_skills = self._skills.canonical_set(candidate.skill_names)
        held: list[str] = []
        for cert in candidate.certifications:
            held.extend([cert.name, *cert.aliases])
        self._held_certs = self._certs.canonical_set(held)

    def skills_in(self, text: str) -> set[str]:
        """Canonical vocabulary skills mentioned in text (held or not)."""
        return self._skills.find(text)

    def unheld_terms(self, text: str) -> set[str]:
        """Known skills or certifications mentioned in text that the candidate does not hold."""
        return (self._skills.find(text) - self._held_skills) | (
            self._certs.find(text) - self._held_certs
        )

    def evaluate(
        self, job: JobPosting, *, today: date | None = None, applications_today: int = 0
    ) -> MatchResult:
        today = today or utcnow().date()
        units, structured = _classify(job.description)
        skill_kinds = self._skill_kinds(units)
        required = sorted(
            t for t, k in skill_kinds.items() if _resolve(k, structured) == _Kind.REQUIRED
        )
        preferred = sorted(
            t for t, k in skill_kinds.items() if _resolve(k, structured) == _Kind.PREFERRED
        )
        req_hit = [t for t in required if t in self._held_skills]
        req_miss = [t for t in required if t not in self._held_skills]
        pref_hit = [t for t in preferred if t in self._held_skills]
        pref_miss = [t for t in preferred if t not in self._held_skills]

        cert_groups = self._cert_groups(units, structured)
        cert_hit: list[str] = []
        cert_miss: list[str] = []
        failures: list[str] = []
        satisfied_groups = 0
        for group, kind in cert_groups:
            held = sorted(group & self._held_certs)
            if held:
                satisfied_groups += 1
                cert_hit.extend(held)
                continue
            label = " or ".join(sorted(group))
            cert_miss.append(label)
            if kind == _Kind.REQUIRED:
                failures.append(f"Required certification not held: {label}")

        unverified: list[str] = []
        required_years = self._max_years(units)
        self._check_years(required_years, failures, unverified)
        self._check_degree(units, failures, unverified)
        self._check_restrictions(units, unverified)

        experience_fraction = self._experience_fraction(required_years, bool(req_hit or pref_hit))
        fractions = {
            "required_skills": _ratio(len(req_hit), len(required)),
            "preferred_skills": _ratio(len(pref_hit), len(preferred)),
            "certifications": _ratio(satisfied_groups, len(cert_groups)),
            "experience": experience_fraction,
            "title": self._title_fraction(job.title),
            "location": self._location_fraction(job),
            "employment_type": self._employment_fraction(job.employment_type),
        }
        weights = self.weights
        overall = round(
            fractions["required_skills"] * weights.required_skills
            + fractions["experience"] * weights.relevant_experience
            + fractions["preferred_skills"] * weights.preferred_skills
            + fractions["certifications"] * weights.certifications
            + fractions["title"] * weights.job_title_relevance
            + fractions["location"] * weights.location_and_remote
            + fractions["employment_type"] * weights.employment_type,
            1,
        )

        filters = self._filter_reasons(job, today)
        review: list[str] = []
        insufficient = not (required or preferred or cert_groups)
        if insufficient:
            review.append(
                "Description is empty or has no recognisable skills; cannot assess the match"
                if len(job.description.strip()) < MIN_DESCRIPTION_CHARS
                else "No known skills or certifications found in the description; review manually"
            )

        recommendation = self._decide(
            overall, filters, failures, insufficient, unverified, applications_today, review
        )
        if recommendation == Recommendation.REVIEW and not insufficient:
            review = [*unverified, *review]
        elif recommendation != Recommendation.REVIEW:
            review = []

        met = not failures and not unverified
        result = MatchResult(
            overall_score=overall,
            threshold=self.threshold,
            recommendation=recommendation,
            required_skills_matched=req_hit,
            required_skills_missing=req_miss,
            preferred_skills_matched=pref_hit,
            preferred_skills_missing=pref_miss,
            certifications_matched=sorted(set(cert_hit)),
            certifications_missing=cert_miss,
            required_skills_score=round(fractions["required_skills"] * 100, 1),
            preferred_skills_score=round(fractions["preferred_skills"] * 100, 1),
            certifications_score=round(fractions["certifications"] * 100, 1),
            experience_score=round(fractions["experience"] * 100, 1),
            title_score=round(fractions["title"] * 100, 1),
            location_score=round(fractions["location"] * 100, 1),
            employment_type_score=round(fractions["employment_type"] * 100, 1),
            mandatory_requirements_met=met,
            mandatory_failures=failures,
            mandatory_unverified=unverified,
            filter_reasons=filters,
            review_reasons=review,
            explanation="",
        )
        return result.model_copy(update={"explanation": self._explain(result, required_years)})

    def _decide(
        self,
        score: float,
        filters: list[str],
        failures: list[str],
        insufficient: bool,
        unverified: list[str],
        applications_today: int,
        review: list[str],
    ) -> Recommendation:
        if filters:
            return Recommendation.SKIP
        if failures:
            return Recommendation.INELIGIBLE
        if insufficient:
            return Recommendation.REVIEW
        if score < self.threshold:
            return Recommendation.SKIP
        if unverified:
            return Recommendation.REVIEW
        if applications_today >= self.daily_cap:
            review.append(f"Daily application limit of {self.daily_cap} already reached")
            return Recommendation.REVIEW
        return Recommendation.APPLY

    def _skill_kinds(self, units: list[_Unit]) -> dict[str, _Kind]:
        kinds: dict[str, _Kind] = {}
        for unit in units:
            for term in self._skills.find(unit.text):
                kinds[term] = max(kinds.get(term, _Kind.NEUTRAL), unit.kind)
        return kinds

    def _cert_groups(
        self, units: list[_Unit], structured: bool
    ) -> list[tuple[frozenset[str], _Kind]]:
        groups: dict[frozenset[str], _Kind] = {}
        for unit in units:
            found = self._certs.find(unit.text)
            if not found:
                continue
            kind = _resolve(unit.kind, structured)
            if len(found) > 1 and _ALTERNATIVE.search(unit.text):
                pieces = [frozenset(found)]
            else:
                pieces = [frozenset({term}) for term in found]
            for piece in pieces:
                groups[piece] = max(groups.get(piece, _Kind.NEUTRAL), kind)
        return sorted(groups.items(), key=lambda item: sorted(item[0]))

    @staticmethod
    def _max_years(units: list[_Unit]) -> float | None:
        values = [
            years
            for unit in units
            if unit.kind != _Kind.PREFERRED
            and (years := _max_required_years(unit.text)) is not None
        ]
        return max(values) if values else None

    def _check_years(
        self, required_years: float | None, failures: list[str], unverified: list[str]
    ) -> None:
        if required_years is None:
            return
        have = self.candidate.total_years_experience
        shortfall = required_years - have
        message = f"Requires {required_years:g}+ years of experience; profile lists {have:g}"
        if shortfall > NEAR_MISS_YEARS:
            failures.append(message)
        elif shortfall > 0:
            unverified.append(f"{message} (near miss)")

    def _check_degree(self, units: list[_Unit], failures: list[str], unverified: list[str]) -> None:
        needed: list[int] = []
        for unit in units:
            if unit.kind == _Kind.PREFERRED or _DEGREE_WAIVER.search(unit.text):
                continue
            level = _required_degree_level(unit.text)
            if level is not None:
                needed.append(level)
        if not needed:
            return
        required_level = max(needed)
        have = _candidate_degree_level(self.candidate)
        if have is None:
            unverified.append(
                "Degree required; education level in the profile could not be verified"
            )
        elif have < required_level:
            failures.append("Required degree level exceeds the education listed in the profile")

    @staticmethod
    def _check_restrictions(units: list[_Unit], unverified: list[str]) -> None:
        # A soft clearance mention is ignored; citizenship and sponsorship always need a human.
        hard = [u.text for u in units if u.kind != _Kind.PREFERRED]
        everything = [u.text for u in units]
        for pattern, message, texts in (
            (_CLEARANCE, "Security clearance mentioned; the profile does not state one", hard),
            (
                _CITIZENSHIP,
                "Citizenship requirement mentioned; confirm eligibility manually",
                everything,
            ),
            (
                _SPONSORSHIP,
                "Visa sponsorship is mentioned; confirm eligibility manually",
                everything,
            ),
        ):
            if any(pattern.search(text) for text in texts):
                unverified.append(message)

    def _experience_fraction(self, required_years: float | None, any_skill: bool) -> float:
        if required_years:
            return min(1.0, self.candidate.total_years_experience / required_years)
        return 1.0 if any_skill else 0.0

    def _title_fraction(self, title: str) -> float:
        references = [*self.preferences.preferred_titles]
        if self.candidate.personal.current_job_title:
            references.append(self.candidate.personal.current_job_title)
        job_tokens = _title_tokens(title)
        best = 0.0
        for reference in references:
            ref_tokens = _title_tokens(reference)
            union = job_tokens | ref_tokens
            if union:
                best = max(best, len(job_tokens & ref_tokens) / len(union))
        return best

    @staticmethod
    def _workplace(job: JobPosting) -> WorkplaceType:
        if job.workplace_type != WorkplaceType.UNKNOWN:
            return job.workplace_type
        header = f"{job.location or ''} {job.title}"
        if _REMOTE_WORD.search(header) or _REMOTE_IN_DESCRIPTION.search(job.description):
            return WorkplaceType.REMOTE
        if _HYBRID_WORD.search(header):
            return WorkplaceType.HYBRID
        return WorkplaceType.UNKNOWN

    def _location_fraction(self, job: JobPosting) -> float:
        workplace = self._workplace(job)
        preference = self.preferences.remote_preference
        workplace_fit = _WORKPLACE_FIT[preference][workplace]
        wanted = [loc.lower() for loc in self.preferences.preferred_locations]
        if not wanted or (workplace == WorkplaceType.REMOTE and preference != "onsite"):
            place_fit = 1.0
        elif not job.location:
            place_fit = 0.5
        else:
            here = job.location.lower()
            place_fit = 1.0 if any(w in here or here in w for w in wanted) else 0.0
        return (workplace_fit + place_fit) / 2

    def _employment_fraction(self, employment_type: str | None) -> float:
        wanted = [re.sub(r"[^a-z]", "", t.lower()) for t in self.preferences.employment_types]
        if not wanted:
            return 1.0
        if not employment_type:
            return 0.5
        actual = re.sub(r"[^a-z]", "", employment_type.lower())
        if not actual:
            return 0.5
        return 1.0 if any(w and (w in actual or actual in w) for w in wanted) else 0.0

    def _filter_reasons(self, job: JobPosting, today: date) -> list[str]:
        preferences = self.preferences
        reasons: list[str] = []
        company = f" {_normalise(job.company)} "
        for excluded in preferences.excluded_companies:
            wanted = _normalise(excluded)
            if wanted and f" {wanted} " in company:
                reasons.append(f"Company is on the exclusion list: {excluded}")
        for excluded in preferences.excluded_titles:
            if excluded.strip() and re.search(
                rf"(?<![A-Za-z0-9]){re.escape(excluded.strip())}(?![A-Za-z0-9])",
                job.title,
                re.IGNORECASE,
            ):
                reasons.append(f"Title contains an excluded term: {excluded}")
        if job.posted_date is not None:
            age = (today - job.posted_date).days
            if age > preferences.maximum_job_age_days:
                reasons.append(
                    f"Posted {age} days ago; limit is {preferences.maximum_job_age_days} days"
                )
        minimum = preferences.minimum_salary
        if minimum is not None and job.salary_max is not None and job.salary_max < minimum:
            reasons.append(f"Maximum salary {job.salary_max} is below the minimum {minimum}")
        return reasons

    def _explain(self, result: MatchResult, required_years: float | None) -> str:
        required_total = len(result.required_skills_matched) + len(result.required_skills_missing)
        preferred_total = len(result.preferred_skills_matched) + len(
            result.preferred_skills_missing
        )
        text = (
            f"Score {result.overall_score:g}/100 (threshold {result.threshold}). "
            f"Required skills {len(result.required_skills_matched)}/{required_total}; "
            f"preferred skills {len(result.preferred_skills_matched)}/{preferred_total}; "
            f"title {result.title_score:g}%, location {result.location_score:g}%, "
            f"employment type {result.employment_type_score:g}%."
        )
        if required_years is not None:
            text += f" Stated experience: {required_years:g}+ years."
        if result.required_skills_missing:
            text += " Missing required: " + ", ".join(result.required_skills_missing) + "."
        for label, items in (
            ("Mandatory failures", result.mandatory_failures),
            ("Needs verification", result.mandatory_unverified),
            ("Filtered", result.filter_reasons),
        ):
            if items:
                text += f" {label}: " + "; ".join(items) + "."
        if result.recommendation == Recommendation.REVIEW and result.review_reasons:
            text += " Review because: " + "; ".join(result.review_reasons) + "."
        return f"{text} Recommendation: {result.recommendation.value}."
