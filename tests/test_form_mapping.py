from __future__ import annotations

import json

import pytest
import yaml

from app.automation.form_mapping import (
    FieldStatus,
    FormField,
    PlannedField,
    SensitiveMatcher,
    plan_fields,
)
from app.config import PROJECT_ROOT
from app.models.question import QuestionCategory, SafeAnswers, SensitiveQuestions

THRESHOLD = 0.85


@pytest.fixture(scope="module")
def matcher() -> SensitiveMatcher:
    data = yaml.safe_load((PROJECT_ROOT / "config/sensitive_questions.yaml").read_text("utf-8"))
    return SensitiveMatcher(SensitiveQuestions.model_validate(data))


@pytest.fixture(scope="module")
def answers() -> SafeAnswers:
    data = yaml.safe_load((PROJECT_ROOT / "config/safe_answers.example.yaml").read_text("utf-8"))
    return SafeAnswers.model_validate(data)


def field(label: str = "", index: int = 0, **kwargs: object) -> FormField:
    values: dict[str, object] = {"tag": "input", "input_type": "text", "label": label}
    values.update(kwargs)
    return FormField.model_validate({"index": index, **values})


def plan_one(
    matcher: SensitiveMatcher,
    answers: SafeAnswers,
    item: FormField,
    *,
    resume: bool = True,
) -> PlannedField:
    (planned,) = plan_fields([item], answers, matcher, threshold=THRESHOLD, resume_available=resume)
    return planned


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("Date of birth", QuestionCategory.SENSITIVE),
        ("Please state your age", QuestionCategory.SENSITIVE),
        ("Are you legally authorized to work in the US?", QuestionCategory.WORK_AUTHORIZATION),
        ("Will you require visa sponsorship?", QuestionCategory.SPONSORSHIP),
        ("Salary expectations", QuestionCategory.SALARY),
        ("Gender", QuestionCategory.DEMOGRAPHIC),
        ("Are you a protected veteran?", QuestionCategory.DEMOGRAPHIC),
        ("Would you relocate?", QuestionCategory.RELOCATION),
        ("I agree to the privacy policy", QuestionCategory.LEGAL),
        ("Do you hold a security clearance?", QuestionCategory.SECURITY_CLEARANCE),
        ("date_of_birth", QuestionCategory.SENSITIVE),
        ("Self-identification", QuestionCategory.DEMOGRAPHIC),
    ],
)
def test_sensitive_questions_are_detected(
    matcher: SensitiveMatcher, text: str, category: QuestionCategory
) -> None:
    assert matcher.classify(text)[0][0] is category


@pytest.mark.parametrize(
    "text",
    ["Language proficiency", "Manager name", "Message", "Average tenure", "Embrace change", "Page"],
)
def test_short_keywords_only_match_whole_words(matcher: SensitiveMatcher, text: str) -> None:
    assert matcher.classify(text) == []


def test_camel_case_identifiers_are_split_for_sensitive_detection(
    matcher: SensitiveMatcher, answers: SafeAnswers
) -> None:
    planned = plan_one(matcher, answers, field("", name="dateOfBirth"))
    assert planned.report.status is FieldStatus.LEFT_BLANK_SENSITIVE


def test_safe_fields_are_planned_with_high_confidence(
    matcher: SensitiveMatcher, answers: SafeAnswers
) -> None:
    for label, key in [
        ("First name *", "first_name"),
        ("Last name", "last_name"),
        ("Email address", "email"),
        ("Phone", "phone"),
        ("City", "city"),
        ("LinkedIn profile URL", "linkedin"),
    ]:
        planned = plan_one(matcher, answers, field(label))
        assert planned.report.status is FieldStatus.PLAN_FILL, label
        assert planned.report.answer_key == key
        assert planned.report.confidence >= THRESHOLD
        assert planned.value


def test_reports_never_contain_answer_values(
    matcher: SensitiveMatcher, answers: SafeAnswers
) -> None:
    planned = plan_one(matcher, answers, field("Email address", input_type="email"))
    assert planned.value == "alex.example@example.com"
    assert "alex.example" not in json.dumps(planned.report.model_dump(mode="json"))


def test_identifier_only_field_is_matched_by_name(
    matcher: SensitiveMatcher, answers: SafeAnswers
) -> None:
    planned = plan_one(matcher, answers, field("", name="linkedIn"))
    assert planned.report.answer_key == "linkedin"
    assert planned.report.status is FieldStatus.PLAN_FILL


def test_autocomplete_hint_identifies_field(
    matcher: SensitiveMatcher, answers: SafeAnswers
) -> None:
    planned = plan_one(matcher, answers, field("Given", autocomplete="given-name"))
    assert planned.report.answer_key == "first_name"


def test_wordy_labels_fall_below_the_confidence_threshold(
    matcher: SensitiveMatcher, answers: SafeAnswers
) -> None:
    planned = plan_one(
        matcher, answers, field("Please provide your best email address for contact")
    )
    assert planned.report.status is FieldStatus.LEFT_BLANK_LOW_CONFIDENCE
    assert planned.value is None


@pytest.mark.parametrize("label", ["Reference email", "Manager phone", "Emergency contact phone"])
def test_fields_about_other_people_are_not_filled(
    matcher: SensitiveMatcher, answers: SafeAnswers, label: str
) -> None:
    planned = plan_one(matcher, answers, field(label))
    assert planned.report.status is FieldStatus.LEFT_BLANK_LOW_CONFIDENCE
    assert planned.value is None


def test_sensitive_detection_beats_a_matching_safe_answer(matcher: SensitiveMatcher) -> None:
    risky = SafeAnswers.model_validate(
        {
            "answers": [
                {
                    "key": "pay",
                    "patterns": ["salary"],
                    "value": "100000",
                    "category": "SAFE_JOB_SPECIFIC",
                    "source": "user_config",
                    "approved": True,
                }
            ]
        }
    )
    planned = plan_one(matcher, risky, field("Salary"))
    assert planned.report.status is FieldStatus.LEFT_BLANK_SENSITIVE
    assert planned.report.category is QuestionCategory.SALARY
    assert planned.value is None


def test_unapproved_and_manual_only_answers_are_ignored(matcher: SensitiveMatcher) -> None:
    def entry(key: str, **flags: bool) -> dict[str, object]:
        return {
            "key": key,
            "patterns": [key],
            "value": "x",
            "category": "SAFE_PROFILE",
            "source": "user_config",
            **flags,
        }

    config = SafeAnswers.model_validate(
        {"answers": [entry("nickname"), entry("hobby", approved=True, manual_only=True)]}
    )
    for label in ("Nickname", "Hobby"):
        planned = plan_one(matcher, config, field(label))
        assert planned.report.status is FieldStatus.LEFT_BLANK_UNKNOWN


def test_conflicting_answers_are_ambiguous(matcher: SensitiveMatcher) -> None:
    def entry(key: str) -> dict[str, object]:
        return {
            "key": key,
            "patterns": ["contact"],
            "value": key,
            "category": "SAFE_PROFILE",
            "source": "user_config",
            "approved": True,
        }

    config = SafeAnswers.model_validate({"answers": [entry("a"), entry("b")]})
    planned = plan_one(matcher, config, field("Contact"))
    assert planned.report.status is FieldStatus.LEFT_BLANK_AMBIGUOUS
    assert planned.value is None


def test_existing_values_are_not_overwritten(
    matcher: SensitiveMatcher, answers: SafeAnswers
) -> None:
    planned = plan_one(matcher, answers, field("City", has_value=True))
    assert planned.report.status is FieldStatus.LEFT_BLANK_HAS_VALUE


def test_unknown_questions_are_left_blank(matcher: SensitiveMatcher, answers: SafeAnswers) -> None:
    planned = plan_one(
        matcher, answers, field("Why do you want to work here?", tag="textarea", input_type="")
    )
    assert planned.report.status is FieldStatus.LEFT_BLANK_UNKNOWN


def test_select_uses_only_an_exactly_matching_option(
    matcher: SensitiveMatcher, answers: SafeAnswers
) -> None:
    label = "How did you hear about us?"
    match = plan_one(
        matcher,
        answers,
        field(label, tag="select", input_type="", options=["Job board", "Company careers page"]),
    )
    assert match.report.status is FieldStatus.PLAN_FILL
    assert match.value == "Company careers page"
    other = plan_one(
        matcher, answers, field(label, tag="select", input_type="", options=["Job board"])
    )
    assert other.report.status is FieldStatus.LEFT_BLANK_UNKNOWN


@pytest.mark.parametrize("input_type", ["radio", "checkbox"])
def test_radio_and_checkbox_are_never_selected(
    matcher: SensitiveMatcher, answers: SafeAnswers, input_type: str
) -> None:
    planned = plan_one(matcher, answers, field("City", input_type=input_type))
    assert planned.report.status is FieldStatus.LEFT_BLANK_UNSUPPORTED


def test_only_resume_file_fields_receive_the_resume(
    matcher: SensitiveMatcher, answers: SafeAnswers
) -> None:
    assert (
        plan_one(matcher, answers, field("Resume/CV", input_type="file")).report.status
        is FieldStatus.PLAN_UPLOAD
    )
    for label in ("Cover letter", "Portfolio", "Upload your resume and cover letter"):
        planned = plan_one(matcher, answers, field(label, input_type="file"))
        assert planned.report.status is not FieldStatus.PLAN_UPLOAD, label
    no_resume = plan_one(matcher, answers, field("Resume", input_type="file"), resume=False)
    assert no_resume.report.status is FieldStatus.LEFT_BLANK_UNSUPPORTED


def test_number_and_date_inputs_are_not_filled(
    matcher: SensitiveMatcher, answers: SafeAnswers
) -> None:
    planned = plan_one(matcher, answers, field("City", input_type="number"))
    assert planned.report.status is FieldStatus.LEFT_BLANK_UNSUPPORTED


def test_email_typed_field_labelled_phone_is_not_trusted(
    matcher: SensitiveMatcher, answers: SafeAnswers
) -> None:
    planned = plan_one(matcher, answers, field("Phone", input_type="email"))
    assert planned.report.status is not FieldStatus.PLAN_FILL
