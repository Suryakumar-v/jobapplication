"""Validate environment settings and every YAML configuration file."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from pydantic import BaseModel, ValidationError
from rich.console import Console
from rich.table import Table

from app.config import Settings, load_settings, load_yaml
from app.models.application import PortalSettings
from app.models.candidate import CandidateProfile
from app.models.job import SearchPreferences
from app.models.question import SafeAnswers, SensitiveQuestions
from app.models.scoring import ScoringConfig, SkillVocabulary

# (config file stem, model, whether an *.example.yaml may stand in for a missing real file)
CONFIG_FILES: tuple[tuple[str, type[BaseModel], bool], ...] = (
    ("candidate_profile", CandidateProfile, True),
    ("search_preferences", SearchPreferences, True),
    ("safe_answers", SafeAnswers, True),
    ("sensitive_questions", SensitiveQuestions, False),
    ("scoring_weights", ScoringConfig, False),
    ("skill_vocabulary", SkillVocabulary, False),
    ("portal_settings", PortalSettings, False),
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    warning: bool = False


def _resolve_file(config_dir: Path, stem: str, allow_example: bool) -> tuple[Path | None, bool]:
    real = config_dir / f"{stem}.yaml"
    if real.is_file():
        return real, False
    example = config_dir / f"{stem}.example.yaml"
    if allow_example and example.is_file():
        return example, True
    return None, False


def check_config_files(config_dir: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    for stem, model, allow_example in CONFIG_FILES:
        path, is_example = _resolve_file(config_dir, stem, allow_example)
        if path is None:
            results.append(CheckResult(stem, False, f"missing {stem}.yaml"))
            continue
        try:
            data = load_yaml(path)
            model.model_validate(data if data is not None else {})
        except (yaml.YAMLError, ValidationError, OSError) as exc:
            first_line = (
                str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            )
            results.append(CheckResult(stem, False, f"{path.name}: {first_line}"))
            continue
        note = f"{path.name} valid"
        if is_example:
            note += " (EXAMPLE data - copy to a real file and edit before use)"
        results.append(CheckResult(stem, True, note, warning=is_example))
    return results


def check_settings(settings: Settings, *, require_directories: bool) -> list[CheckResult]:
    results: list[CheckResult] = []
    violations = settings.safety_violations()
    if violations:
        results.append(CheckResult("safety_flags", False, " ".join(violations)))
    else:
        results.append(CheckResult("safety_flags", True, "manual approval on, auto-submit off"))
    if require_directories:
        for name, path in settings.required_directories().items():
            ok = path.is_dir()
            results.append(CheckResult(f"dir:{name}", ok, str(path) if ok else f"missing {path}"))
    return results


def check_consistency(settings: Settings, config_dir: Path) -> list[CheckResult]:
    path = config_dir / "scoring_weights.yaml"
    if not path.is_file():
        return []
    try:
        minimum = ScoringConfig.model_validate(load_yaml(path)).minimum_score
    except (yaml.YAMLError, ValidationError, OSError):
        return []
    if minimum != settings.job_match_threshold:
        return [
            CheckResult(
                "threshold_consistency",
                True,
                f"JOB_MATCH_THRESHOLD={settings.job_match_threshold} differs from "
                f"scoring_weights.yaml minimum_score={minimum}",
                warning=True,
            )
        ]
    return []


def run_checks(settings: Settings, *, require_directories: bool = True) -> list[CheckResult]:
    return [
        *check_settings(settings, require_directories=require_directories),
        *check_config_files(settings.config_dir),
        *check_consistency(settings, settings.config_dir),
    ]


def render(results: list[CheckResult], console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title="Configuration validation")
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Detail", overflow="fold")
    for result in results:
        label = "WARN" if result.ok and result.warning else ("OK" if result.ok else "FAIL")
        style = "yellow" if label == "WARN" else ("green" if result.ok else "red")
        table.add_row(result.name, f"[{style}]{label}[/{style}]", result.detail)
    console.print(table)


def main(env: Mapping[str, str] | None = None) -> int:
    settings = load_settings(env)
    results = run_checks(settings)
    render(results)
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
