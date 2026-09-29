"""Load validated YAML configuration, falling back to *.example.yaml where allowed."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from app.config import Settings, load_yaml
from app.models.application import PortalSettings
from app.models.candidate import CandidateProfile
from app.models.job import SearchPreferences
from app.models.question import SafeAnswers, SensitiveQuestions
from app.models.scoring import ScoringConfig, SkillVocabulary

ModelT = TypeVar("ModelT", bound=BaseModel)


class ConfigError(RuntimeError):
    """A required configuration file is missing or invalid."""


@dataclass(frozen=True)
class LoadedConfig:
    candidate: CandidateProfile
    preferences: SearchPreferences
    scoring: ScoringConfig
    vocabulary: SkillVocabulary
    example_files: tuple[str, ...]

    @property
    def using_example_data(self) -> bool:
        return bool(self.example_files)


def _load(
    config_dir: Path, stem: str, model: type[ModelT], allow_example: bool
) -> tuple[ModelT, bool]:
    candidates = [(config_dir / f"{stem}.yaml", False)]
    if allow_example:
        candidates.append((config_dir / f"{stem}.example.yaml", True))
    for path, is_example in candidates:
        if not path.is_file():
            continue
        try:
            return model.model_validate(load_yaml(path) or {}), is_example
        except (yaml.YAMLError, ValidationError, OSError) as exc:
            detail = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
            raise ConfigError(f"{path.name} is invalid: {detail}") from exc
    raise ConfigError(f"Missing configuration file {stem}.yaml in {config_dir}")


def load_matching_config(settings: Settings) -> LoadedConfig:
    config_dir = settings.config_dir
    candidate, candidate_example = _load(config_dir, "candidate_profile", CandidateProfile, True)
    preferences, preferences_example = _load(
        config_dir, "search_preferences", SearchPreferences, True
    )
    scoring, _ = _load(config_dir, "scoring_weights", ScoringConfig, False)
    vocabulary, _ = _load(config_dir, "skill_vocabulary", SkillVocabulary, False)
    examples = tuple(
        stem
        for stem, flag in (
            ("candidate_profile", candidate_example),
            ("search_preferences", preferences_example),
        )
        if flag
    )
    return LoadedConfig(candidate, preferences, scoring, vocabulary, examples)


@dataclass(frozen=True)
class FormConfig:
    portals: PortalSettings
    answers: SafeAnswers
    sensitive: SensitiveQuestions
    using_example_answers: bool


def load_form_config(settings: Settings) -> FormConfig:
    config_dir = settings.config_dir
    portals, _ = _load(config_dir, "portal_settings", PortalSettings, False)
    answers, example = _load(config_dir, "safe_answers", SafeAnswers, True)
    sensitive, _ = _load(config_dir, "sensitive_questions", SensitiveQuestions, False)
    return FormConfig(portals, answers, sensitive, example)
