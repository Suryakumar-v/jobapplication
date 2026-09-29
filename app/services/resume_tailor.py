"""Truthful resume tailoring: reorder verified profile facts for one job, never add claims."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from pydantic import BaseModel

from app.models.candidate import (
    CandidateProfile,
    Certification,
    Education,
    Experience,
    PersonalInfo,
)
from app.models.scoring import MatchResult
from app.services.matching_engine import MatchingEngine
from app.utils.text_utils import normalize_text, normalize_whitespace

MAX_MASTER_BYTES = 10 * 1024 * 1024
SKILL_SEPARATOR = " \u2022 "
SECTIONS = ("Summary", "Skills", "Experience", "Certifications", "Education")


class MasterResumeError(ValueError):
    """The master resume is missing or unreadable."""


class ResumeValidation(BaseModel):
    passed: bool
    errors: list[str]
    warnings: list[str]
    checks_passed: list[str]


@dataclass(frozen=True)
class RoleContent:
    experience: Experience
    bullets: list[str]


@dataclass(frozen=True)
class VerifiedProfile:
    """Profile facts also present in the master resume; the only material a resume may use."""

    skills: list[str]
    certifications: list[Certification]
    education: list[Education]
    warnings: list[str]


@dataclass(frozen=True)
class ResumePlan:
    skills: list[str]
    certifications: list[Certification]
    education: list[Education]
    roles: list[RoleContent]
    promoted_skills: list[str]
    reordered_roles: int
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedResume:
    name: str
    contact: str
    summary: str
    skills: list[str]
    roles: list[tuple[str, list[str]]]
    certifications: list[str]
    education: list[str]
    unexpected: list[str]
    text: str


def read_master_text(path: Path) -> str:
    if path.suffix.lower() != ".docx":
        raise MasterResumeError("The master resume must be a .docx file")
    if not path.is_file():
        raise MasterResumeError(f"Master resume not found: {path}")
    if path.stat().st_size > MAX_MASTER_BYTES:
        raise MasterResumeError("The master resume is larger than 10 MB")
    try:
        document = Document(str(path))
    except (PackageNotFoundError, zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise MasterResumeError("The master resume is not a readable .docx file") from exc
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


def _in_text(norm_text: str, term: str) -> bool:
    needle = normalize_text(term)
    if not needle:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", norm_text) is not None


def _any_in_text(norm_text: str, terms: list[str]) -> bool:
    return any(_in_text(norm_text, term) for term in terms)


def verify_profile(profile: CandidateProfile, master_text: str) -> VerifiedProfile:
    """Keep only skills, certifications and education that the master resume also shows."""
    norm = normalize_text(master_text)
    warnings: list[str] = []
    skills: list[str] = []
    for skill in profile.skills:
        if _any_in_text(norm, [skill.name, *skill.aliases]):
            skills.append(skill.name)
        else:
            warnings.append(f"Skill left out (not found in master resume): {skill.name}")
    certifications: list[Certification] = []
    for cert in profile.certifications:
        if _any_in_text(norm, [cert.name, *cert.aliases]):
            certifications.append(cert)
        else:
            warnings.append(f"Certification left out (not found in master resume): {cert.name}")
    education: list[Education] = []
    for entry in profile.education:
        if _in_text(norm, entry.institution):
            education.append(entry)
        else:
            warnings.append(f"Education left out (not found in master resume): {entry.institution}")
    return VerifiedProfile(skills, certifications, education, warnings)


def contact_line(personal: PersonalInfo) -> str:
    urls = [personal.linkedin_url, personal.portfolio_url, personal.github_url]
    parts = [
        str(personal.email),
        personal.phone,
        f"{personal.city}, {personal.country}",
        *(str(url) for url in urls if url),
    ]
    return " | ".join(parts)


def role_header(role: Experience) -> str:
    end = role.end_date.strftime("%b %Y") if role.end_date else "Present"
    parts = [role.title, role.employer]
    if role.location:
        parts.append(role.location)
    parts.append(f"{role.start_date.strftime('%b %Y')} \u2013 {end}")
    return " | ".join(parts)


def certification_line(cert: Certification) -> str:
    text = cert.name
    if cert.issuer:
        text += f" \u2014 {cert.issuer}"
    if cert.year:
        text += f" ({cert.year})"
    return text


def education_line(entry: Education) -> str:
    text = entry.degree
    if entry.field_of_study:
        text += f", {entry.field_of_study}"
    text += f" \u2014 {entry.institution}"
    if entry.graduation_year:
        text += f" ({entry.graduation_year})"
    return text


def full_name(personal: PersonalInfo) -> str:
    return f"{personal.first_name} {personal.last_name}"


def build_plan(
    profile: CandidateProfile, master_text: str, match: MatchResult, engine: MatchingEngine
) -> ResumePlan:
    verified = verify_profile(profile, master_text)
    required = {name.lower() for name in match.required_skills_matched}
    preferred = {name.lower() for name in match.preferred_skills_matched}

    def skill_rank(name: str) -> int:
        skill = next(s for s in profile.skills if s.name == name)
        names = {skill.name.lower(), *(alias.lower() for alias in skill.aliases)}
        return 0 if names & required else 1 if names & preferred else 2

    skills = sorted(verified.skills, key=skill_rank)
    promoted = [name for name in skills if skill_rank(name) < 2]

    def bullet_weight(bullet: str) -> int:
        found = {name.lower() for name in engine.skills_in(bullet)}
        return 2 * len(found & required) + len(found & preferred)

    roles: list[RoleContent] = []
    reordered = 0
    for role in profile.experience:
        ordered = sorted(role.bullets, key=lambda b: -bullet_weight(b))
        reordered += ordered != role.bullets
        roles.append(RoleContent(role, ordered))
    return ResumePlan(
        skills=skills,
        certifications=verified.certifications,
        education=verified.education,
        roles=roles,
        promoted_skills=promoted,
        reordered_roles=reordered,
        warnings=verified.warnings,
    )


def describe_plan(plan: ResumePlan, match: MatchResult) -> str:
    parts = [
        f"Moved {len(plan.promoted_skills)} job-relevant skills to the front",
        f"reordered bullets in {plan.reordered_roles} role(s)",
        "nothing added, rewritten or removed",
    ]
    text = "; ".join(parts) + "."
    gaps = [
        *match.required_skills_missing,
        *match.preferred_skills_missing,
        *match.certifications_missing,
    ]
    if gaps:
        text += " Not claimed (not in profile): " + ", ".join(gaps) + "."
    if plan.warnings:
        text += (
            f" {len(plan.warnings)} item(s) left out because the master resume does not show them."
        )
    return text


def render_docx(profile: CandidateProfile, plan: ResumePlan, path: Path) -> None:
    name = full_name(profile.personal)
    document = Document()
    document.core_properties.author = name
    document.core_properties.title = f"Resume - {name}"
    document.add_paragraph(name, style="Title")
    document.add_paragraph(contact_line(profile.personal))
    summary = profile.summary.strip()
    if summary:
        document.add_paragraph("Summary", style="Heading 1")
        document.add_paragraph(summary)
    if plan.skills:
        document.add_paragraph("Skills", style="Heading 1")
        document.add_paragraph(SKILL_SEPARATOR.join(plan.skills))
    document.add_paragraph("Experience", style="Heading 1")
    for role in plan.roles:
        document.add_paragraph(role_header(role.experience), style="Heading 2")
        for bullet in role.bullets:
            document.add_paragraph(bullet, style="List Bullet")
    if plan.certifications:
        document.add_paragraph("Certifications", style="Heading 1")
        for cert in plan.certifications:
            document.add_paragraph(certification_line(cert), style="List Bullet")
    if plan.education:
        document.add_paragraph("Education", style="Heading 1")
        for entry in plan.education:
            document.add_paragraph(education_line(entry))
    document.save(str(path))


def parse_docx(path: Path) -> ParsedResume:
    document = Document(str(path))
    name = contact = ""
    summary: list[str] = []
    skills: list[str] = []
    roles: list[tuple[str, list[str]]] = []
    certifications: list[str] = []
    education: list[str] = []
    unexpected: list[str] = []
    lines: list[str] = []
    section: str | None = None
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        lines.append(text)
        style = paragraph.style.name if paragraph.style is not None else ""
        if style == "Title":
            name = text
        elif style == "Heading 1":
            section = text
            if text not in SECTIONS:
                unexpected.append(f"Unexpected section: {text}")
        elif section is None:
            contact = f"{contact}\n{text}".strip()
        elif section == "Summary":
            summary.append(text)
        elif section == "Skills":
            skills.extend(text.split(SKILL_SEPARATOR))
        elif section == "Experience" and style == "Heading 2":
            roles.append((text, []))
        elif section == "Experience" and style == "List Bullet" and roles:
            roles[-1][1].append(text)
        elif section == "Certifications":
            certifications.append(text)
        elif section == "Education":
            education.append(text)
        else:
            unexpected.append(f"Unexpected content in {section}: {text[:60]}")
    return ParsedResume(
        name=name,
        contact=contact,
        summary=" ".join(summary),
        skills=skills,
        roles=roles,
        certifications=certifications,
        education=education,
        unexpected=unexpected,
        text="\n".join(lines),
    )


def validate_resume(
    parsed: ParsedResume,
    profile: CandidateProfile,
    master_text: str,
    engine: MatchingEngine,
) -> ResumeValidation:
    """Re-check the written document against the profile and master, independent of the plan."""
    errors: list[str] = []
    passed: list[str] = []
    norm_master = normalize_text(master_text)
    verified = verify_profile(profile, master_text)
    warnings = list(verified.warnings)

    def check(name: str, problems: list[str]) -> None:
        if problems:
            errors.extend(problems)
        else:
            passed.append(name)

    check("structure", parsed.unexpected)
    identity = []
    if parsed.name != full_name(profile.personal):
        identity.append("Name does not match the profile")
    if parsed.contact != contact_line(profile.personal):
        identity.append("Contact details do not match the profile")
    check("identity", identity)
    check(
        "summary",
        []
        if normalize_whitespace(parsed.summary) == normalize_whitespace(profile.summary)
        else ["Summary differs from the profile"],
    )

    allowed_skills = {name.lower(): name for name in verified.skills}
    skill_problems = [
        f"Skill not supported by profile and master: {s}"
        for s in parsed.skills
        if s.lower() not in allowed_skills
    ]
    if len({s.lower() for s in parsed.skills}) != len(parsed.skills):
        skill_problems.append("Duplicate skills listed")
    check("skills", skill_problems)

    for label, found, expected in (
        (
            "certifications",
            parsed.certifications,
            [certification_line(c) for c in verified.certifications],
        ),
        ("education", parsed.education, [education_line(e) for e in verified.education]),
    ):
        check(
            label,
            [f"Unsupported {label} entry: {line}" for line in found if line not in expected],
        )

    experience_problems: list[str] = []
    expected_headers = [role_header(role) for role in profile.experience]
    if [header for header, _ in parsed.roles] != expected_headers:
        experience_problems.append("Roles differ from the profile (added, removed or changed)")
    else:
        for role, (header, bullets) in zip(profile.experience, parsed.roles, strict=True):
            if sorted(map(normalize_whitespace, bullets)) != sorted(
                map(normalize_whitespace, role.bullets)
            ):
                experience_problems.append(f"Bullets differ from the profile for: {header}")
    for role in profile.experience:
        for label, value in (("employer", role.employer), ("title", role.title)):
            if not _in_text(norm_master, value):
                experience_problems.append(f"Role {label} not found in the master resume: {value}")
    check("experience", experience_problems)

    baseline = "\n".join(
        [profile.summary, *(b for role in profile.experience for b in role.bullets), master_text]
    )
    unsupported = engine.unheld_terms(parsed.text) - engine.unheld_terms(baseline)
    check(
        "unsupported_terms",
        [f"Mentions terms the candidate does not hold: {', '.join(sorted(unsupported))}"]
        if unsupported
        else [],
    )

    unverified_bullets = [
        b
        for role in profile.experience
        for b in role.bullets
        if normalize_text(b) not in norm_master
    ]
    if unverified_bullets:
        warnings.append(
            f"{len(unverified_bullets)} bullet(s) are worded differently in the master resume;"
            " profile wording was used"
        )
    return ResumeValidation(
        passed=not errors, errors=errors, warnings=warnings, checks_passed=passed
    )
