"""Decide, for every form field, whether it may be filled and with what.

Pure logic with no browser dependency. Only approved SAFE_PROFILE / SAFE_JOB_SPECIFIC answers are
ever used. Sensitive, legal and unknown questions are left blank for the applicant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field

from app.models.question import QuestionCategory, SafeAnswer, SafeAnswers, SensitiveQuestions
from app.utils.text_utils import normalize_text

RESUME_KEY = "resume"
TEXT_INPUT_TYPES = frozenset({"text", "email", "tel", "url", "search", ""})
MANUAL_ONLY_TYPES = frozenset({"radio", "checkbox"})
# A field about someone else (a referee, a manager) must never receive the applicant's own data.
THIRD_PARTY_TERMS = frozenset(
    {
        "reference",
        "references",
        "referee",
        "referral",
        "referrer",
        "emergency",
        "supervisor",
        "manager",
        "spouse",
        "partner",
        "parent",
        "guardian",
        "recruiter",
        "colleague",
        "friend",
        "employer",
        "previous",
        "former",
        "school",
        "university",
    }
)
AUTOCOMPLETE_KEYS = {
    "given-name": "first_name",
    "family-name": "last_name",
    "email": "email",
    "tel": "phone",
    "tel-national": "phone",
    "address-level2": "city",
}
_TYPE_KEY_HINT = {"email": "email", "tel": "phone"}
_RESUME_PATTERNS = (("resume",), ("cv",), ("curriculum", "vitae"))
_NOT_RESUME = frozenset({"cover", "letter"})
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class FieldStatus(StrEnum):
    FILLED = "FILLED"
    UPLOADED = "UPLOADED"
    FILL_FAILED = "FILL_FAILED"
    LEFT_BLANK_SENSITIVE = "LEFT_BLANK_SENSITIVE"
    LEFT_BLANK_UNKNOWN = "LEFT_BLANK_UNKNOWN"
    LEFT_BLANK_LOW_CONFIDENCE = "LEFT_BLANK_LOW_CONFIDENCE"
    LEFT_BLANK_AMBIGUOUS = "LEFT_BLANK_AMBIGUOUS"
    LEFT_BLANK_HAS_VALUE = "LEFT_BLANK_HAS_VALUE"
    LEFT_BLANK_UNSUPPORTED = "LEFT_BLANK_UNSUPPORTED"
    PLAN_FILL = "PLAN_FILL"
    PLAN_UPLOAD = "PLAN_UPLOAD"


class FormField(BaseModel):
    """One visible form control as read from the page."""

    index: int
    tag: str
    input_type: str = ""
    name: str = ""
    element_id: str = ""
    autocomplete: str = ""
    label: str = ""
    aria_label: str = ""
    placeholder: str = ""
    group_label: str = ""
    options: list[str] = Field(default_factory=list)
    required: bool = False
    has_value: bool = False

    @property
    def display_label(self) -> str:
        order: tuple[str, ...] = (self.label, self.group_label, self.aria_label, self.placeholder)
        if self.input_type == "radio":
            order = (self.group_label, *order)
        for text in order:
            if text.strip():
                return " ".join(text.split())[:200]
        return self.name or self.element_id or f"(unlabelled {self.input_type or self.tag})"


class FieldReport(BaseModel):
    """Decision for one field. Never contains the answer value."""

    index: int
    label: str
    input_type: str
    required: bool
    category: QuestionCategory
    status: FieldStatus
    confidence: float = 0.0
    answer_key: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class PlannedField:
    field: FormField
    report: FieldReport
    value: str | None = None


def _identifier_text(text: str) -> str:
    return normalize_text(_CAMEL.sub(" ", text))


def descriptor_text(field: FormField) -> str:
    """Everything that describes the field, normalised, for sensitive-question detection."""
    parts = (
        field.label,
        field.group_label,
        field.aria_label,
        field.placeholder,
        _identifier_text(field.name),
        _identifier_text(field.element_id),
        _identifier_text(field.autocomplete),
    )
    return normalize_text(" ".join(parts))


class SensitiveMatcher:
    """Whole-word keyword matching, so short keywords such as 'age' never hit 'manager'."""

    def __init__(self, config: SensitiveQuestions) -> None:
        self._patterns: list[tuple[QuestionCategory, str, re.Pattern[str]]] = []
        for category, keywords in config.categories.items():
            for keyword in keywords:
                tokens = normalize_text(keyword).split()
                if not tokens:
                    continue
                body = r"\s+".join(re.escape(t) for t in tokens)
                suffix = r"\w*" if len(tokens[-1]) >= 5 else ""
                self._patterns.append(
                    (category, keyword, re.compile(rf"\b{body}{suffix}\b", re.IGNORECASE))
                )

    def classify(self, text: str) -> list[tuple[QuestionCategory, str]]:
        normalized = normalize_text(text)
        return [(c, k) for c, k, pattern in self._patterns if pattern.search(normalized)]


def _tokens(text: str) -> list[str]:
    return normalize_text(text).split()


def _identifier_tokens(text: str) -> list[list[str]]:
    """camelCase identifiers are tried both split ('linked In') and joined ('linkedin')."""
    return [_identifier_text(text).split(), _tokens(text)]


def _contains(haystack: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return n > 0 and any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


def score_tokens(descriptor: list[str], pattern: list[str]) -> float:
    """1.0 for an exact match; each extra word in the descriptor lowers confidence."""
    if not descriptor or not pattern:
        return 0.0
    if descriptor == pattern:
        return 1.0
    if _contains(descriptor, pattern):
        return max(0.5, 0.92 - 0.03 * (len(descriptor) - len(pattern)))
    return 0.0


def _signals(field: FormField) -> list[tuple[list[str], float]]:
    """Descriptor token lists with a weight reflecting how visible-to-a-human each source is."""
    signals = [
        (_tokens(field.label), 1.0),
        (_tokens(field.aria_label), 1.0),
        (_tokens(field.placeholder), 0.95),
    ]
    signals += [(t, 0.95) for t in _identifier_tokens(field.name)]
    signals += [(t, 0.95) for t in _identifier_tokens(field.element_id)]
    return signals


def _confidence(field: FormField, patterns: list[list[str]]) -> float:
    best = 0.0
    for tokens, weight in _signals(field):
        for pattern in patterns:
            best = max(best, score_tokens(tokens, pattern) * weight)
    return best


def _all_tokens(field: FormField) -> set[str]:
    return {token for tokens, _ in _signals(field) for token in tokens}


def _answer_confidence(field: FormField, answer: SafeAnswer) -> float:
    hint = _TYPE_KEY_HINT.get(field.input_type)
    if hint is not None and hint != answer.key:
        return 0.0
    if AUTOCOMPLETE_KEYS.get(field.autocomplete.lower().strip()) == answer.key:
        return 0.98
    patterns = [_tokens(p) for p in answer.patterns]
    return _confidence(field, patterns)


def _option_for(value: str, options: list[str]) -> str | None:
    wanted = normalize_text(value)
    for option in options:
        if normalize_text(option) == wanted:
            return option
    return None


def _report(
    field: FormField,
    category: QuestionCategory,
    status: FieldStatus,
    reason: str,
    *,
    confidence: float = 0.0,
    answer_key: str | None = None,
) -> FieldReport:
    return FieldReport(
        index=field.index,
        label=field.display_label,
        input_type=field.input_type or field.tag,
        required=field.required,
        category=category,
        status=status,
        confidence=round(confidence, 3),
        answer_key=answer_key,
        reason=reason,
    )


def _blank(
    field: FormField,
    status: FieldStatus,
    reason: str,
    category: QuestionCategory = QuestionCategory.UNKNOWN,
    *,
    confidence: float = 0.0,
) -> PlannedField:
    return PlannedField(field, _report(field, category, status, reason, confidence=confidence))


def _plan_file(field: FormField, threshold: float, resume_available: bool) -> PlannedField:
    if not _all_tokens(field).isdisjoint(_NOT_RESUME):
        return _blank(field, FieldStatus.LEFT_BLANK_UNKNOWN, "file field is not a resume")
    confidence = _confidence(field, [list(p) for p in _RESUME_PATTERNS])
    if confidence == 0.0:
        return _blank(field, FieldStatus.LEFT_BLANK_UNKNOWN, "file field is not recognised")
    if confidence < threshold:
        return _blank(
            field,
            FieldStatus.LEFT_BLANK_LOW_CONFIDENCE,
            f"resume match confidence {confidence:.2f} below {threshold:.2f}",
            confidence=confidence,
        )
    if not resume_available:
        return _blank(field, FieldStatus.LEFT_BLANK_UNSUPPORTED, "no tailored resume available")
    report = _report(
        field,
        QuestionCategory.SAFE_PROFILE,
        FieldStatus.PLAN_UPLOAD,
        "tailored resume",
        confidence=confidence,
        answer_key=RESUME_KEY,
    )
    return PlannedField(field, report)


def _plan_answer(field: FormField, answers: list[SafeAnswer], threshold: float) -> PlannedField:
    scored = [(a, _answer_confidence(field, a)) for a in answers]
    scored = [(a, c) for a, c in scored if c > 0.0]
    if not scored:
        return _blank(field, FieldStatus.LEFT_BLANK_UNKNOWN, "no approved answer matches")
    best = max(c for _, c in scored)
    top = [a for a, c in scored if abs(c - best) < 1e-9]
    if len({a.key for a in top}) > 1:
        return _blank(
            field,
            FieldStatus.LEFT_BLANK_AMBIGUOUS,
            "matches several answers: " + ", ".join(sorted(a.key for a in top)),
            confidence=best,
        )
    answer = top[0]
    if not _all_tokens(field).isdisjoint(THIRD_PARTY_TERMS) and (
        answer.category is QuestionCategory.SAFE_PROFILE
    ):
        return _blank(
            field,
            FieldStatus.LEFT_BLANK_LOW_CONFIDENCE,
            "field appears to be about another person",
            confidence=best,
        )
    if best < threshold:
        return _blank(
            field,
            FieldStatus.LEFT_BLANK_LOW_CONFIDENCE,
            f"confidence {best:.2f} below {threshold:.2f}",
            confidence=best,
        )
    if field.has_value:
        return _blank(
            field,
            FieldStatus.LEFT_BLANK_HAS_VALUE,
            "field already has a value; not overwritten",
            confidence=best,
        )
    value = answer.value
    if field.tag == "select":
        option = _option_for(answer.value, field.options)
        if option is None:
            return _blank(
                field,
                FieldStatus.LEFT_BLANK_UNKNOWN,
                "no dropdown option equals the approved answer",
                confidence=best,
            )
        value = option
    elif field.tag != "textarea" and field.input_type not in TEXT_INPUT_TYPES:
        return _blank(field, FieldStatus.LEFT_BLANK_UNSUPPORTED, f"input type '{field.input_type}'")
    report = _report(
        field,
        answer.category,
        FieldStatus.PLAN_FILL,
        "approved answer",
        confidence=best,
        answer_key=answer.key,
    )
    return PlannedField(field, report, value)


def plan_fields(
    fields: list[FormField],
    answers: SafeAnswers,
    matcher: SensitiveMatcher,
    *,
    threshold: float,
    resume_available: bool,
) -> list[PlannedField]:
    """One decision per field. Sensitive detection always runs first and always wins."""
    usable = [a for a in answers.answers if a.approved and not a.manual_only]
    planned: list[PlannedField] = []
    for field in fields:
        hits = matcher.classify(descriptor_text(field))
        if hits:
            category, keyword = hits[0]
            planned.append(
                _blank(
                    field,
                    FieldStatus.LEFT_BLANK_SENSITIVE,
                    f"{category.value.lower()} question (matched '{keyword}'); answer manually",
                    category,
                )
            )
        elif field.input_type in MANUAL_ONLY_TYPES:
            planned.append(
                _blank(
                    field,
                    FieldStatus.LEFT_BLANK_UNSUPPORTED,
                    f"{field.input_type} choices are never selected automatically",
                )
            )
        elif field.input_type == "file":
            planned.append(_plan_file(field, threshold, resume_available))
        else:
            planned.append(_plan_answer(field, usable, threshold))
    return planned
