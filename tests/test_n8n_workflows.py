from __future__ import annotations

import copy
import json
import re
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.main import create_app
from scripts.validate_n8n_workflows import DEFAULT_DIR, validate_directory, validate_workflow

EXPECTED_FILES = {
    "01_job_discovery.json",
    "02_job_normalization.json",
    "03_duplicate_check.json",
    "04_job_analysis.json",
    "05_resume_tailoring.json",
    "06_application_preparation.json",
    "07_manual_approval.json",
    "08_application_submission.json",
    "09_tracker_update.json",
    "10_error_handler.json",
    "master_job_application_workflow.json",
}


def _valid() -> dict[str, Any]:
    return {
        "name": "t",
        "active": False,
        "nodes": [
            {
                "id": "1",
                "name": "A",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [0, 0],
                "parameters": {},
            },
            {
                "id": "2",
                "name": "B",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [1, 0],
                "parameters": {},
            },
        ],
        "connections": {"A": {"main": [[{"node": "B", "type": "main", "index": 0}]]}},
    }


def _errors(data: dict[str, Any]) -> list[str]:
    return validate_workflow(data, json.dumps(data))


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DEFAULT_DIR / name).read_text("utf-8"))
    return data


def _all() -> dict[str, dict[str, Any]]:
    return {path.name: _load(path.name) for path in sorted(DEFAULT_DIR.glob("*.json"))}


def _nodes_of_type(data: dict[str, Any], node_type: str) -> list[dict[str, Any]]:
    return [n for n in data["nodes"] if n["type"] == node_type]


def _http_node() -> dict[str, Any]:
    return {
        "id": "3",
        "name": "Call",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [2, 0],
        "parameters": {
            "method": "GET",
            "url": "={{ $json.api_url }}/jobs",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
        },
        "credentials": {"httpHeaderAuth": {"id": "", "name": "Job Automation API Key"}},
    }


def _with_http(**changes: Any) -> dict[str, Any]:
    data = _valid()
    node = _http_node()
    node["parameters"].update(changes.pop("parameters", {}))
    node.update(changes)
    data["nodes"].append(node)
    data["connections"]["B"] = {"main": [[{"node": "Call", "type": "main", "index": 0}]]}
    return data


def test_http_node_with_credential_passes() -> None:
    assert _errors(_with_http()) == []


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"parameters": {"url": "http://10.0.0.5:8000/jobs"}}, "api_url"),
        (
            {"parameters": {"url": "={{ $json.api_url }}/applications/1/submit"}},
            "submit or approval",
        ),
        ({"parameters": {"url": "={{ $json.api_url }}/approvals/x"}}, "submit or approval"),
        ({"parameters": {"sendHeaders": True}}, "by hand"),
        ({"credentials": {}}, "credential"),
    ],
)
def test_http_node_rules(changes: dict[str, Any], message: str) -> None:
    assert any(message in e for e in _errors(_with_http(**changes)))


def test_health_call_needs_no_credential() -> None:
    data = _with_http(parameters={"url": "={{ $json.api_url }}/health"}, credentials={})
    assert _errors(data) == []


def test_active_workflow_is_rejected() -> None:
    data = _valid()
    data["active"] = True
    assert any("inactive" in e for e in _errors(data))


def _write_pair(tmp_path: Path, first: dict[str, Any], second: dict[str, Any]) -> None:
    (tmp_path / "a.json").write_text(json.dumps(first), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(second), encoding="utf-8")


def test_workflow_ids_are_required_and_unique(tmp_path: Path) -> None:
    first = _valid() | {"id": "same"}
    _write_pair(tmp_path, first, _valid() | {"id": "same"})
    assert any("also used by" in e for e in validate_directory(tmp_path)["b.json"])
    _write_pair(tmp_path, first, _valid())
    assert any("missing workflow 'id'" in e for e in validate_directory(tmp_path)["b.json"])


def test_unknown_workflow_reference_is_reported(tmp_path: Path) -> None:
    caller = _valid() | {"id": "one", "settings": {"errorWorkflow": "ghost"}}
    caller["nodes"].append(
        {
            "id": "9",
            "name": "Run",
            "type": "n8n-nodes-base.executeWorkflow",
            "typeVersion": 1.2,
            "position": [3, 0],
            "parameters": {"workflowId": {"__rl": True, "value": "missing", "mode": "id"}},
        }
    )
    caller["connections"]["B"] = {"main": [[{"node": "Run", "type": "main", "index": 0}]]}
    _write_pair(tmp_path, caller, _valid() | {"id": "two"})
    errors = validate_directory(tmp_path)["a.json"]
    assert any("'missing'" in e for e in errors)
    assert any("'ghost'" in e for e in errors)


STAGES = {
    "jaWf02Normalize": "02_job_normalization.json",
    "jaWf04Analysis": "04_job_analysis.json",
    "jaWf05Tailoring": "05_resume_tailoring.json",
    "jaWf06Preparation": "06_application_preparation.json",
    "jaWf07Approval": "07_manual_approval.json",
    "jaWf09Tracker": "09_tracker_update.json",
}


def test_master_runs_the_expected_stages_in_order() -> None:
    master = _load("master_job_application_workflow.json")
    runs = {n["name"]: n for n in _nodes_of_type(master, "n8n-nodes-base.executeWorkflow")}
    assert [runs[name]["parameters"]["workflowId"]["value"] for name in runs] == list(STAGES)
    for node in runs.values():
        assert node["alwaysOutputData"] is True
        assert node["parameters"]["options"]["waitForSubWorkflow"] is True
    chain = ["Startup Validation OK", *runs, "Final Summary"]
    for current, following in pairwise(chain):
        assert master["connections"][current]["main"][0][0]["node"] == following
    ids = {name: _load(name)["id"] for name in STAGES.values()}
    assert ids == {v: k for k, v in STAGES.items()}


def test_every_stage_uses_the_shared_error_handler() -> None:
    handler = _load("10_error_handler.json")
    assert handler["id"] == "jaWf10ErrHandler"
    assert _nodes_of_type(handler, "n8n-nodes-base.errorTrigger")
    for name, data in _all().items():
        if name != "10_error_handler.json":
            assert data["settings"]["errorWorkflow"] == handler["id"], name


def test_called_stages_accept_master_input_and_keep_one_item() -> None:
    for filename in STAGES.values():
        data = _load(filename)
        assert _nodes_of_type(data, "n8n-nodes-base.executeWorkflowTrigger"), filename
        assert _nodes_of_type(data, "n8n-nodes-base.limit"), filename


def test_submission_workflow_cannot_reach_the_api() -> None:
    data = _load("08_application_submission.json")
    forbidden = {"n8n-nodes-base.httpRequest", "n8n-nodes-base.executeWorkflow"}
    assert not {n["type"] for n in data["nodes"]} & forbidden
    master = _load("master_job_application_workflow.json")
    assert "jaWf08Submission" not in json.dumps(master)
    for name, other in _all().items():
        assert (
            "jaWf08Submission" not in json.dumps(other) or name == "08_application_submission.json"
        )


def test_no_workflow_calls_a_submit_or_approval_endpoint() -> None:
    for name, data in _all().items():
        for node in _nodes_of_type(data, "n8n-nodes-base.httpRequest"):
            url = node["parameters"]["url"]
            assert not re.search(r"/(submit|approv)", url, re.IGNORECASE), (name, node["name"])


def test_workflows_cannot_reach_the_approval_endpoints() -> None:
    for path in DEFAULT_DIR.glob("*.json"):
        text = path.read_text("utf-8")
        assert "/approval" not in text, path.name
        assert "approval_token" not in text, path.name


def test_workflows_use_only_the_credential_and_no_secret_headers() -> None:
    for name, data in _all().items():
        for node in _nodes_of_type(data, "n8n-nodes-base.httpRequest"):
            params = node["parameters"]
            assert "sendHeaders" not in params, (name, node["name"])
            if not params["url"].endswith("/health"):
                assert node["credentials"]["httpHeaderAuth"]["name"] == "Job Automation API Key"


def _normalise(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


def _called_endpoints(url: str) -> list[str]:
    quoted = re.findall(r"'(/[\w/{}-]+)'", url)
    if quoted:
        return quoted
    path = re.sub(r"^=\{\{[^}]*api_url \}\}", "", url)
    path = path.split("?", 1)[0]
    return [re.sub(r"\{\{[^}]*\}\}", "{}", path)]


def test_every_workflow_endpoint_exists_in_the_api(settings: Settings) -> None:
    spec = create_app(settings).openapi()
    routes = {
        (method.upper(), _normalise(path))
        for path, operations in spec["paths"].items()
        for method in operations
    }
    checked = 0
    for name, data in _all().items():
        for node in _nodes_of_type(data, "n8n-nodes-base.httpRequest"):
            method = node["parameters"]["method"]
            for path in _called_endpoints(node["parameters"]["url"]):
                assert (method, _normalise(path)) in routes, (name, node["name"], path)
                checked += 1
    assert checked >= 15


def test_discovery_maps_each_input_type_to_an_import_endpoint() -> None:
    node = next(n for n in _load("01_job_discovery.json")["nodes"] if n["name"] == "Import Jobs")
    paths = re.findall(r"'(/[\w/]+)'", node["parameters"]["url"])
    assert paths == [
        "/jobs/import/url",
        "/jobs/import/text",
        "/jobs/import/json",
        "/jobs/import/csv",
        "/jobs/webhook",
        "/jobs/extract",
    ]


def test_discovery_webhook_requires_authentication() -> None:
    webhook = _nodes_of_type(_load("01_job_discovery.json"), "n8n-nodes-base.webhook")[0]
    assert webhook["parameters"]["authentication"] == "headerAuth"


def test_analysis_stops_on_apply_below_threshold() -> None:
    data = _load("04_job_analysis.json")
    names = {n["name"] for n in data["nodes"]}
    assert {"IF No APPLY Below Threshold", "Stop: APPLY Below Threshold"} <= names
    assert data["connections"]["IF No APPLY Below Threshold"]["main"][1][0]["node"] == (
        "Stop: APPLY Below Threshold"
    )


def test_preparation_takes_at_most_three_applications_and_never_submits() -> None:
    data = _load("06_application_preparation.json")
    lister = next(n for n in data["nodes"] if n["name"] == "List Prepared Applications")
    assert "status=PREPARED&limit=3" in lister["parameters"]["url"]
    urls = [n["parameters"]["url"] for n in _nodes_of_type(data, "n8n-nodes-base.httpRequest")]
    assert any(url.endswith("/form/start") for url in urls)
    assert not any("/form/close" in url for url in urls)


def test_tracker_workflow_exports_after_the_status_snapshot() -> None:
    data = _load("09_tracker_update.json")
    export = next(n for n in data["nodes"] if n["name"] == "Export Tracker Files")
    assert export["parameters"]["method"] == "POST"
    assert export["parameters"]["url"].endswith("/exports/tracker")
    assert data["connections"]["Stage Summary"]["main"][0][0]["node"] == "Export Tracker Files"
    assert data["connections"]["Export Tracker Files"]["main"][0][0]["node"] == "Export Summary"


def test_placeholders_are_gone() -> None:
    for name, data in _all().items():
        assert "PLACEHOLDER" not in json.dumps(data), name
        assert "planned_phase" not in json.dumps(data), name


def test_all_expected_workflow_files_exist_and_are_valid() -> None:
    results = validate_directory(DEFAULT_DIR)
    assert set(results) == EXPECTED_FILES
    assert {name: errs for name, errs in results.items() if errs} == {}


def test_master_workflow_is_gated_on_safety_flags() -> None:
    data = json.loads((DEFAULT_DIR / "master_job_application_workflow.json").read_text("utf-8"))
    names = {n["name"] for n in data["nodes"]}
    assert {"Manual Trigger", "Schedule Trigger", "Check API Health and Safety Flags"} <= names
    if_node = next(n for n in data["nodes"] if n["name"] == "IF Startup Validation Passed")
    text = json.dumps(if_node)
    assert "manual_approval_required" in text
    assert "automatic_submission_enabled" in text
    assert data["active"] is False


def test_no_workflow_is_active() -> None:
    for path in DEFAULT_DIR.glob("*.json"):
        assert json.loads(path.read_text("utf-8")).get("active") is False


def test_valid_minimal_workflow_passes() -> None:
    assert _errors(_valid()) == []


def test_detects_unknown_connection_target() -> None:
    data = _valid()
    data["connections"]["A"]["main"][0][0]["node"] = "Ghost"
    assert any("Ghost" in e for e in _errors(data))


def test_detects_disconnected_node() -> None:
    data = _valid()
    data["nodes"].append(copy.deepcopy(data["nodes"][1]) | {"id": "3", "name": "C"})
    assert any("'C' is disconnected" in e for e in _errors(data))


def test_sticky_notes_may_be_unconnected() -> None:
    data = _valid()
    data["nodes"].append(
        {
            "id": "3",
            "name": "Note",
            "type": "n8n-nodes-base.stickyNote",
            "typeVersion": 1,
            "position": [0, 9],
            "parameters": {},
        }
    )
    assert _errors(data) == []


def test_detects_duplicate_names_and_missing_trigger() -> None:
    data = _valid()
    data["nodes"][1]["name"] = "A"
    assert any("duplicate node name" in e for e in _errors(data))
    data = _valid()
    data["nodes"][0]["type"] = "n8n-nodes-base.set"
    assert any("no trigger" in e for e in _errors(data))


def test_detects_hardcoded_secrets_and_personal_email() -> None:
    data = _valid()
    data["nodes"][1]["parameters"] = {"password": "hunter2hunter2"}
    assert any("hardcoded secret" in e for e in _errors(data))
    data = _valid()
    data["nodes"][1]["parameters"] = {"to": "someone@company.com"}
    assert any("hardcoded secret" in e for e in _errors(data))
    data = _valid()
    data["nodes"][1]["parameters"] = {"password": "={{ $env.X }}"}
    assert _errors(data) == []


def test_invalid_json_reported(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert "invalid JSON" in validate_directory(tmp_path)["bad.json"][0]


def test_empty_directory_reported(tmp_path: Path) -> None:
    assert validate_directory(tmp_path)
