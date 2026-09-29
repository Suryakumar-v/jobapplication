"""Run the matching engine over stored jobs and persist the outcome."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database.base import utcnow
from app.database.repositories import JobRepository
from app.database.tables import JobRecord
from app.models.application import ApplicationStatus
from app.models.job import JobPosting, JobSource, WorkplaceType
from app.models.scoring import MatchResult, Recommendation
from app.services.config_loader import LoadedConfig, load_matching_config
from app.services.matching_engine import MatchingEngine
from app.utils.logging_config import get_logger

_STATUS_FOR: dict[Recommendation, ApplicationStatus] = {
    Recommendation.APPLY: ApplicationStatus.ANALYZED,
    Recommendation.REVIEW: ApplicationStatus.REVIEW_REQUIRED,
    Recommendation.SKIP: ApplicationStatus.SKIPPED,
    Recommendation.INELIGIBLE: ApplicationStatus.INELIGIBLE,
}
# Jobs already past analysis keep their lifecycle status.
_REANALYSABLE = frozenset(
    {
        ApplicationStatus.DISCOVERED,
        ApplicationStatus.ANALYZED,
        ApplicationStatus.REVIEW_REQUIRED,
        ApplicationStatus.SKIPPED,
        ApplicationStatus.INELIGIBLE,
    }
)


class JobNotFoundError(LookupError):
    pass


class AnalysisNotAllowedError(ValueError):
    pass


class AnalysisOutcome(BaseModel):
    job_id: str
    company: str
    title: str
    status: str
    result: MatchResult


class BatchAnalysis(BaseModel):
    requested: int
    analyzed: int
    apply: int
    review: int
    skip: int
    ineligible: int
    not_allowed: list[str]
    not_found: list[str]
    using_example_config: bool
    outcomes: list[AnalysisOutcome]


def _to_posting(job: JobRecord) -> JobPosting:
    try:
        workplace = WorkplaceType(job.workplace_type or WorkplaceType.UNKNOWN)
    except ValueError:
        workplace = WorkplaceType.UNKNOWN
    return JobPosting(
        company=job.company,
        title=job.title,
        description=job.description or "",
        location=job.location,
        employment_type=job.employment_type,
        workplace_type=workplace,
        salary_min=job.salary_min,
        salary_max=job.salary_max,
        posted_date=job.posted_date,
        source=JobSource(job.source),
    )


class JobMatchingService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.jobs = JobRepository(session)
        self._config: LoadedConfig | None = None
        self._engine: MatchingEngine | None = None

    @property
    def config(self) -> LoadedConfig:
        if self._config is None:
            self._config = load_matching_config(self.settings)
        return self._config

    @property
    def engine(self) -> MatchingEngine:
        if self._engine is None:
            config = self.config
            # The stricter of the environment and file thresholds wins.
            threshold = max(self.settings.job_match_threshold, config.scoring.minimum_score)
            cap = min(
                self.settings.max_applications_per_day,
                config.preferences.max_applications_per_day,
            )
            self._engine = MatchingEngine(
                config.candidate,
                config.preferences,
                config.scoring.weights,
                config.vocabulary,
                threshold=threshold,
                daily_cap=cap,
            )
        return self._engine

    def _apply_recommendations_today(self, excluding: str) -> int:
        start = utcnow().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return (
            self.session.scalar(
                select(func.count())
                .select_from(JobRecord)
                .where(
                    JobRecord.recommendation == Recommendation.APPLY.value,
                    JobRecord.analyzed_at >= start,
                    JobRecord.job_id != excluding,
                )
            )
            or 0
        )

    def analyze_job(self, job_id: str) -> AnalysisOutcome:
        job = self.jobs.get_by_job_id(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        if ApplicationStatus(job.status) not in _REANALYSABLE:
            raise AnalysisNotAllowedError(f"Job {job_id} is {job.status}; it cannot be re-analysed")
        result = self.engine.evaluate(
            _to_posting(job), applications_today=self._apply_recommendations_today(job_id)
        )
        moment: datetime = utcnow()
        job.match_score = result.overall_score
        job.recommendation = result.recommendation.value
        job.matching_explanation = result.explanation
        job.missing_requirements = result.missing_requirements
        job.matching_details = result.model_dump(mode="json")
        job.analyzed_at = moment
        job.status = _STATUS_FOR[result.recommendation].value
        self.session.flush()
        get_logger("app.matching").info(
            "job analysed",
            extra={
                "operation": "analyze_job",
                "job_id": job_id,
                "recommendation": result.recommendation.value,
            },
        )
        return AnalysisOutcome(
            job_id=job.job_id,
            company=job.company,
            title=job.title,
            status=job.status,
            result=result,
        )

    def analyze_batch(self, job_ids: list[str] | None, limit: int) -> BatchAnalysis:
        """Analyse the given jobs, or the newest not-yet-analysed jobs (oldest first)."""
        cap = min(limit, self.settings.max_jobs_per_run, self.config.preferences.max_jobs_per_run)
        if job_ids is None:
            pending = self.session.scalars(
                select(JobRecord.job_id)
                .where(JobRecord.status == ApplicationStatus.DISCOVERED.value)
                .order_by(JobRecord.id)
                .limit(cap)
            )
            targets = list(pending)
        else:
            targets = list(dict.fromkeys(job_ids))[:cap]
        outcomes: list[AnalysisOutcome] = []
        not_found: list[str] = []
        not_allowed: list[str] = []
        for job_id in targets:
            try:
                outcomes.append(self.analyze_job(job_id))
            except JobNotFoundError:
                not_found.append(job_id)
            except AnalysisNotAllowedError:
                not_allowed.append(job_id)
        counts = {rec: 0 for rec in Recommendation}
        for outcome in outcomes:
            counts[outcome.result.recommendation] += 1
        return BatchAnalysis(
            requested=len(targets),
            analyzed=len(outcomes),
            apply=counts[Recommendation.APPLY],
            review=counts[Recommendation.REVIEW],
            skip=counts[Recommendation.SKIP],
            ineligible=counts[Recommendation.INELIGIBLE],
            not_allowed=not_allowed,
            not_found=not_found,
            using_example_config=self.config.using_example_data,
            outcomes=outcomes,
        )
