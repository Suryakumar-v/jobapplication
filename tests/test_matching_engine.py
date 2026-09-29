from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from app.config import PROJECT_ROOT, Settings
from app.models.candidate import CandidateProfile, Certification, Education
from app.models.job import JobPosting, SearchPreferences
from app.models.scoring import MatchResult, Recommendation
from app.services.config_loader import LoadedConfig, load_matching_config
from app.services.matching_engine import MatchingEngine

TODAY = date(2026, 9, 29)

STRONG = """\
Required Qualifications:
- 5+ years of experience in network security
- Firewall policy management
- RADIUS and TACACS+ authentication
- NAC using Aruba ClearPass
Preferred Qualifications:
- Check Point
- Splunk
- Python
"""


@pytest.fixture(scope="module")
def loaded() -> LoadedConfig:
    return load_matching_config(Settings(config_directory=PROJECT_ROOT / "config"))


def make_engine(
    loaded: LoadedConfig,
    *,
    candidate: CandidateProfile | None = None,
    preferences: SearchPreferences | None = None,
    daily_cap: int = 10,
    threshold: int = 75,
) -> MatchingEngine:
    return MatchingEngine(
        candidate or loaded.candidate,
        preferences or loaded.preferences,
        loaded.scoring.weights,
        loaded.vocabulary,
        threshold=threshold,
        daily_cap=daily_cap,
    )


def make_job(**overrides: Any) -> JobPosting:
    values: dict[str, Any] = {
        "company": "Synthetic Networks Ltd",
        "title": "Network Security Engineer",
        "description": STRONG,
        "location": "Springfield",
        "employment_type": "Full-time",
    }
    values.update(overrides)
    return JobPosting(**values)


def run(loaded: LoadedConfig, job: JobPosting, **kwargs: Any) -> MatchResult:
    engine_kwargs = {k: kwargs.pop(k) for k in ("candidate", "preferences") if k in kwargs}
    return make_engine(loaded, **engine_kwargs).evaluate(job, today=TODAY, **kwargs)


def test_strong_match_is_apply_with_full_score(loaded: LoadedConfig) -> None:
    result = run(loaded, make_job())
    assert result.overall_score == 100.0
    assert result.recommendation == Recommendation.APPLY
    assert result.mandatory_requirements_met is True
    assert set(result.required_skills_matched) == {
        "Network Security",
        "Firewall policy management",
        "RADIUS",
        "TACACS+",
        "NAC",
        "Aruba ClearPass",
    }
    assert result.required_skills_missing == []
    assert set(result.preferred_skills_matched) == {"Check Point", "Splunk", "Python automation"}
    assert "APPLY" in result.explanation


def test_result_is_deterministic(loaded: LoadedConfig) -> None:
    assert run(loaded, make_job()) == run(loaded, make_job())


def test_weights_drive_component_scores(loaded: LoadedConfig) -> None:
    description = "Required: RADIUS, Palo Alto.\nPreferred: Splunk, Zscaler."
    result = run(loaded, make_job(description=description, location="Elsewhere"))
    assert result.required_skills_score == 50.0
    assert result.preferred_skills_score == 50.0
    assert result.location_score == 50.0
    assert result.employment_type_score == 100.0
    # 35*.5 + 20*1 + 10*.5 + 10*1 + 10*1 + 10*.5 + 5*1
    assert result.overall_score == 72.5
    assert result.recommendation == Recommendation.SKIP


def test_missing_required_certification_blocks_apply_even_with_high_score(
    loaded: LoadedConfig,
) -> None:
    result = run(loaded, make_job(description=STRONG + "\nCISSP certification required"))
    assert result.overall_score >= 75
    assert result.recommendation == Recommendation.INELIGIBLE
    assert result.mandatory_requirements_met is False
    assert result.certifications_missing == ["CISSP"]
    assert any("CISSP" in failure for failure in result.mandatory_failures)
    assert "CISSP" in result.missing_requirements[-1]


def test_preferred_certification_is_only_a_score_penalty(loaded: LoadedConfig) -> None:
    result = run(loaded, make_job(description=STRONG + "\nCISSP is a plus"))
    assert result.certifications_missing == ["CISSP"]
    assert result.mandatory_failures == []
    assert result.recommendation == Recommendation.APPLY
    assert result.certifications_score == 0.0


def test_alternative_certifications_satisfied_by_any_held(loaded: LoadedConfig) -> None:
    text = STRONG + "\nCISSP or CCNP required"
    without = run(loaded, make_job(description=text))
    assert without.recommendation == Recommendation.INELIGIBLE
    assert without.certifications_missing == ["CCNP or CISSP"]
    holder = loaded.candidate.model_copy(
        update={"certifications": [*loaded.candidate.certifications, Certification(name="CISSP")]}
    )
    with_cert = run(loaded, make_job(description=text), candidate=holder)
    assert with_cert.recommendation == Recommendation.APPLY
    assert with_cert.certifications_matched == ["CISSP"]


def test_candidate_certification_alias_counts(loaded: LoadedConfig) -> None:
    result = run(loaded, make_job(description=STRONG + "\nENSC required"))
    assert result.certifications_matched == ["Example Network Security Certification"]
    assert result.recommendation == Recommendation.APPLY


def test_years_shortfall_is_ineligible(loaded: LoadedConfig) -> None:
    result = run(loaded, make_job(description=STRONG.replace("5+", "15+")))
    assert result.recommendation == Recommendation.INELIGIBLE
    assert any("15+ years" in failure for failure in result.mandatory_failures)
    assert result.experience_score == 60.0


def test_years_near_miss_needs_review(loaded: LoadedConfig) -> None:
    result = run(loaded, make_job(description=STRONG.replace("5+", "10")))
    assert result.recommendation == Recommendation.REVIEW
    assert result.mandatory_requirements_met is False
    assert any("near miss" in reason for reason in result.review_reasons)


def test_years_in_non_experience_sentence_ignored(loaded: LoadedConfig) -> None:
    result = run(loaded, make_job(description=STRONG + "\nFounded 25 years ago."))
    assert result.mandatory_failures == []


@pytest.mark.parametrize(
    "sentence",
    [
        "Active Secret clearance required.",
        "Must be a U.S. citizen.",
        "We are unable to offer visa sponsorship.",
    ],
)
def test_unverifiable_mandatory_items_force_review(loaded: LoadedConfig, sentence: str) -> None:
    result = run(loaded, make_job(description=f"{STRONG}\n{sentence}"))
    assert result.overall_score >= 75
    assert result.recommendation == Recommendation.REVIEW
    assert result.mandatory_requirements_met is False
    assert result.mandatory_unverified
    assert result.review_reasons == result.mandatory_unverified


def test_preferred_clearance_does_not_force_review(loaded: LoadedConfig) -> None:
    result = run(loaded, make_job(description=STRONG + "\nSecurity clearance is a plus."))
    assert result.recommendation == Recommendation.APPLY


def test_degree_above_profile_is_ineligible(loaded: LoadedConfig) -> None:
    result = run(loaded, make_job(description=STRONG + "\nMaster's degree required."))
    assert result.recommendation == Recommendation.INELIGIBLE


def test_degree_met_or_waived_is_fine(loaded: LoadedConfig) -> None:
    met = run(loaded, make_job(description=STRONG + "\nBachelor's degree required."))
    waived = run(
        loaded,
        make_job(description=STRONG + "\nMaster's degree or equivalent experience."),
    )
    assert met.recommendation == waived.recommendation == Recommendation.APPLY


def test_degree_requirement_with_unknown_profile_education_needs_review(
    loaded: LoadedConfig,
) -> None:
    unknown = loaded.candidate.model_copy(update={"education": []})
    result = run(
        loaded, make_job(description=STRONG + "\nBachelor's degree required."), candidate=unknown
    )
    assert result.recommendation == Recommendation.REVIEW
    odd = loaded.candidate.model_copy(
        update={"education": [Education(institution="X", degree="Diploma")]}
    )
    assert (
        run(loaded, make_job(description=STRONG + "\nBS degree required."), candidate=odd)
    ).recommendation in {Recommendation.APPLY, Recommendation.REVIEW}


def test_preferred_and_required_are_told_apart(loaded: LoadedConfig) -> None:
    result = run(loaded, make_job(description="Must have RADIUS. Cisco ISE is a plus."))
    assert result.required_skills_matched == ["RADIUS"]
    assert result.required_skills_missing == []
    assert result.preferred_skills_missing == ["Cisco ISE"]


def test_unstructured_mentions_count_as_required(loaded: LoadedConfig) -> None:
    result = run(loaded, make_job(description="We run Palo Alto firewalls with RADIUS logins."))
    assert result.required_skills_missing == ["Palo Alto"]
    assert result.required_skills_matched == ["RADIUS"]


def test_candidate_alias_matches_and_absent_skills_are_never_claimed(
    loaded: LoadedConfig,
) -> None:
    result = run(loaded, make_job(description="Required: Checkpoint, Fortinet and laws."))
    assert result.required_skills_matched == ["Check Point"]
    assert result.required_skills_missing == ["Fortinet"]
    assert "AWS" not in result.required_skills_missing


def test_no_recognisable_skills_needs_review(loaded: LoadedConfig) -> None:
    empty = run(loaded, make_job(description=""))
    assert empty.recommendation == Recommendation.REVIEW
    assert empty.review_reasons
    vague = run(loaded, make_job(description="A great team and great benefits, join us today!"))
    assert vague.recommendation == Recommendation.REVIEW


def test_score_below_threshold_is_skip(loaded: LoadedConfig) -> None:
    job = make_job(
        title="Cloud DevOps Engineer",
        description="Required: Kubernetes, Terraform, Azure",
        location="Elsewhere",
        employment_type="Contract",
    )
    result = run(loaded, job)
    assert result.overall_score < 75
    assert result.recommendation == Recommendation.SKIP


def test_threshold_is_configurable(loaded: LoadedConfig) -> None:
    job = make_job(
        description="Required: RADIUS, Palo Alto.\nPreferred: Splunk, Zscaler.",
        location="Elsewhere",
    )
    assert run(loaded, job).recommendation == Recommendation.SKIP
    lenient = make_engine(loaded, threshold=60).evaluate(job, today=TODAY)
    assert lenient.overall_score == run(loaded, job).overall_score
    assert lenient.recommendation == Recommendation.APPLY


def test_exclusion_filters_skip_regardless_of_score(loaded: LoadedConfig) -> None:
    excluded_company = run(loaded, make_job(company="Example Staffing Spam Inc."))
    excluded_title = run(loaded, make_job(title="Network Security Intern"))
    assert excluded_company.recommendation == Recommendation.SKIP
    assert excluded_company.overall_score >= 75
    assert excluded_company.filter_reasons
    assert excluded_title.recommendation == Recommendation.SKIP
    assert "Intern" in excluded_title.filter_reasons[0]


def test_short_excluded_company_needs_whole_word(loaded: LoadedConfig) -> None:
    preferences = loaded.preferences.model_copy(update={"excluded_companies": ["EA"]})
    result = run(loaded, make_job(company="Seattle Networks"), preferences=preferences)
    assert result.filter_reasons == []


def test_stale_posting_is_skipped(loaded: LoadedConfig) -> None:
    old = run(loaded, make_job(posted_date=TODAY - timedelta(days=45)))
    fresh = run(loaded, make_job(posted_date=TODAY - timedelta(days=5)))
    assert old.recommendation == Recommendation.SKIP
    assert "45 days" in old.filter_reasons[0]
    assert fresh.recommendation == Recommendation.APPLY


def test_salary_floor_filter(loaded: LoadedConfig) -> None:
    preferences = loaded.preferences.model_copy(update={"minimum_salary": 100_000})
    low = run(loaded, make_job(salary_max=80_000), preferences=preferences)
    unknown = run(loaded, make_job(), preferences=preferences)
    assert low.recommendation == Recommendation.SKIP
    assert unknown.recommendation == Recommendation.APPLY


def test_daily_cap_downgrades_apply_to_review(loaded: LoadedConfig) -> None:
    within = make_engine(loaded, daily_cap=3).evaluate(
        make_job(), today=TODAY, applications_today=2
    )
    reached = make_engine(loaded, daily_cap=3).evaluate(
        make_job(), today=TODAY, applications_today=3
    )
    assert within.recommendation == Recommendation.APPLY
    assert reached.recommendation == Recommendation.REVIEW
    assert "limit of 3" in reached.review_reasons[0]


def test_remote_preference_scoring(loaded: LoadedConfig) -> None:
    remote_only = loaded.preferences.model_copy(update={"remote_preference": "remote"})
    remote = run(loaded, make_job(location="Remote - US"), preferences=remote_only)
    onsite = run(
        loaded, make_job(location="Springfield", workplace_type="ONSITE"), preferences=remote_only
    )
    assert remote.location_score == 100.0
    assert onsite.location_score == 50.0


def test_title_similarity(loaded: LoadedConfig) -> None:
    exact = run(loaded, make_job(title="Sr. Network Security Engineer"))
    related = run(loaded, make_job(title="Network Engineer"))
    unrelated = run(loaded, make_job(title="Pastry Chef"))
    assert exact.title_score == 100.0
    assert 0 < related.title_score < 100
    assert unrelated.title_score == 0.0


def test_employment_type_scoring(loaded: LoadedConfig) -> None:
    assert run(loaded, make_job(employment_type="Full Time")).employment_type_score == 100.0
    assert run(loaded, make_job(employment_type="Part-time")).employment_type_score == 0.0
    assert run(loaded, make_job(employment_type=None)).employment_type_score == 50.0


def test_explanation_lists_reasons(loaded: LoadedConfig) -> None:
    result = run(loaded, make_job(description=STRONG + "\nPalo Alto required.\nCISSP required."))
    assert "Palo Alto" in result.explanation
    assert "Mandatory failures" in result.explanation
    assert result.explanation.endswith("Recommendation: INELIGIBLE.")
