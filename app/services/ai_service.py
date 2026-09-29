"""Advisory AI analysis of stored jobs.

The provider sees only the posting. Its output is validated, stored on its own table and compared
with the candidate profile locally. It never changes a job's score, recommendation or status,
the resume, or any form answer.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.providers import AIInvalidOutputError, AIProvider, AIProviderError, build_provider
from app.ai.schemas import JobInsights, JobRequest, SkillComparison
from app.config import Settings
from app.database.repositories import JobRepository
from app.database.tables import AIAnalysisRecord
from app.models.candidate import CandidateProfile
from app.services.config_loader import load_matching_config
from app.services.matching_engine import MIN_DESCRIPTION_CHARS
from app.services.matching_service import JobNotFoundError
from app.utils.hashing import sha256_text
from app.utils.logging_config import audit
from app.utils.text_utils import normalize_text

ADVISORY_NOTE = (
    "Advisory only. It does not change the match score, status, resume or any form answer."
)


class AIDisabledError(RuntimeError):
    pass


class AIInputError(ValueError):
    pass


class AIStatus(BaseModel):
    enabled: bool
    provider: str
    model: str
    sends_to_provider: str
    note: str


class AIJobAnalysis(BaseModel):
    job_id: str
    provider: str
    model: str
    cached: bool
    created_at: datetime
    insights: JobInsights
    comparison: SkillComparison
    using_example_config: bool
    note: str = ADVISORY_NOTE


def held_terms(profile: CandidateProfile) -> set[str]:
    """Normalised names and aliases of skills and certifications the profile lists."""
    names: list[str] = []
    for skill in profile.skills:
        names.extend([skill.name, *skill.aliases])
    for cert in profile.certifications:
        names.extend([cert.name, *cert.aliases])
    return {normalize_text(name) for name in names if normalize_text(name)}


def compare_skills(insights: JobInsights, held: set[str]) -> SkillComparison:
    """Exact match on normalised names only; anything the profile does not list is a gap."""

    def split(skills: list[str]) -> tuple[list[str], list[str]]:
        covered = [s for s in skills if normalize_text(s) in held]
        return covered, [s for s in skills if s not in covered]

    required_covered, required_gaps = split(insights.required_skills)
    preferred_covered, preferred_gaps = split(insights.preferred_skills)
    return SkillComparison(
        required_covered=required_covered,
        required_gaps=required_gaps,
        preferred_covered=preferred_covered,
        preferred_gaps=preferred_gaps,
    )


def describe_status(settings: Settings) -> AIStatus:
    enabled = settings.ai_enabled and settings.ai_provider != "none"
    model = {"mock": "deterministic-mock", "openai_compatible": settings.ai_model}.get(
        settings.ai_provider, ""
    )
    return AIStatus(
        enabled=enabled,
        provider=settings.ai_provider if enabled else "none",
        model=model if enabled else "",
        sends_to_provider=(
            "job title, company and description only"
            if settings.ai_provider == "openai_compatible" and enabled
            else "nothing (runs locally)"
            if enabled
            else "nothing"
        ),
        note=ADVISORY_NOTE,
    )


def _view(record: AIAnalysisRecord, *, cached: bool) -> AIJobAnalysis:
    return AIJobAnalysis(
        job_id=record.job_id,
        provider=record.provider,
        model=record.model,
        cached=cached,
        created_at=record.created_at,
        insights=JobInsights.model_validate(record.insights),
        comparison=SkillComparison.model_validate(record.comparison),
        using_example_config=record.using_example_config,
    )


class AIJobAnalysisService:
    def __init__(
        self, session: Session, settings: Settings, provider: AIProvider | None = None
    ) -> None:
        self.session = session
        self.settings = settings
        self.jobs = JobRepository(session)
        self._provider = provider

    def latest(self, job_id: str) -> AIJobAnalysis | None:
        record = self.session.scalar(
            select(AIAnalysisRecord)
            .where(AIAnalysisRecord.job_id == job_id)
            .order_by(AIAnalysisRecord.id.desc())
            .limit(1)
        )
        return _view(record, cached=True) if record else None

    def analyze(self, job_id: str, *, force: bool = False) -> AIJobAnalysis:
        if not self.settings.ai_enabled or self.settings.ai_provider == "none":
            raise AIDisabledError("AI is disabled. Set AI_ENABLED=true and choose AI_PROVIDER.")
        violations = self.settings.ai_violations()
        if violations:
            raise AIDisabledError(" ".join(violations))
        job = self.jobs.get_by_job_id(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        description = (job.description or "").strip()
        if len(description) < MIN_DESCRIPTION_CHARS:
            raise AIInputError("The job has no usable description to analyse.")

        config = load_matching_config(self.settings)
        provider = self._provider or build_provider(self.settings, config.vocabulary)
        if provider is None:
            raise AIDisabledError("AI is disabled.")
        request = JobRequest(
            company=job.company,
            title=job.title,
            description=description[: self.settings.ai_max_input_chars],
        )
        input_hash = sha256_text(
            "\n".join(
                [provider.name, provider.model, request.title, request.company, request.description]
            )
        )
        if not force:
            cached = self.session.scalar(
                select(AIAnalysisRecord)
                .where(AIAnalysisRecord.job_id == job_id, AIAnalysisRecord.input_hash == input_hash)
                .order_by(AIAnalysisRecord.id.desc())
                .limit(1)
            )
            if cached is not None:
                return _view(cached, cached=True)

        try:
            insights = JobInsights.model_validate(provider.extract_job_insights(request))
        except ValidationError as exc:
            audit("ai_analysis", "invalid_output", job_id=job_id, provider=provider.name)
            raise AIInvalidOutputError("The AI response did not match the expected shape") from exc
        except AIProviderError as exc:
            audit(
                "ai_analysis",
                "failed",
                job_id=job_id,
                provider=provider.name,
                reason=type(exc).__name__,
            )
            raise

        comparison = compare_skills(insights, held_terms(config.candidate))
        record = AIAnalysisRecord(
            job_id=job_id,
            provider=provider.name,
            model=provider.model,
            input_hash=input_hash,
            insights=insights.model_dump(mode="json"),
            comparison=comparison.model_dump(mode="json"),
            using_example_config=config.using_example_data,
        )
        self.session.add(record)
        self.session.flush()
        audit(
            "ai_analysis",
            "ok",
            job_id=job_id,
            provider=provider.name,
            model=provider.model,
            chars_sent=len(request.description),
        )
        return _view(record, cached=False)
