"""Structural validation of n8n workflow JSON files (offline; does not need n8n installed)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "n8n" / "workflows"
NODE_REQUIRED_KEYS = ("id", "name", "type", "typeVersion", "position", "parameters")
TRIGGER_TYPES = {
    "n8n-nodes-base.webhook",
    "n8n-nodes-base.errorTrigger",
    "n8n-nodes-base.executeWorkflowTrigger",
}
HTTP_NODE = "n8n-nodes-base.httpRequest"
EXECUTE_NODE = "n8n-nodes-base.executeWorkflow"
FORBIDDEN_ENDPOINT = re.compile(r"/(?:submit|approv)", re.IGNORECASE)
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b(?:password|api[_-]?key|secret|token)\b\"?\s*:\s*\"(?!=|\{\{)[^\"]{6,}\""),
    re.compile(r"[\w.+-]+@(?!example\.(?:com|org|net))[\w-]+\.[\w.-]+"),
)


def _is_trigger(node_type: str) -> bool:
    return node_type in TRIGGER_TYPES or node_type.endswith("Trigger")


def _connection_targets(connections: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for source, outputs in connections.items():
        for branches in outputs.values():
            for branch in branches:
                pairs.extend((source, link["node"]) for link in branch)
    return pairs


def _http_errors(node: dict[str, Any]) -> list[str]:
    name = node.get("name")
    params = node.get("parameters", {})
    url = str(params.get("url", ""))
    errors: list[str] = []
    if "api_url" not in url:
        errors.append(f"HTTP node {name!r} must build its URL from api_url, not a fixed host")
    if FORBIDDEN_ENDPOINT.search(url):
        errors.append(f"HTTP node {name!r} calls a submit or approval endpoint")
    if params.get("sendHeaders"):
        errors.append(f"HTTP node {name!r} sets headers by hand; use the API key credential")
    if not url.endswith("/health"):
        credentials = node.get("credentials", {})
        if params.get("genericAuthType") != "httpHeaderAuth" or "httpHeaderAuth" not in credentials:
            errors.append(f"HTTP node {name!r} must use the httpHeaderAuth credential")
    return errors


def _referenced_workflow_ids(data: dict[str, Any]) -> list[str]:
    references: list[str] = []
    for node in data.get("nodes", []):
        if node.get("type") != EXECUTE_NODE:
            continue
        target = node.get("parameters", {}).get("workflowId")
        references.append(str(target.get("value") if isinstance(target, dict) else target))
    error_workflow = data.get("settings", {}).get("errorWorkflow")
    if error_workflow:
        references.append(str(error_workflow))
    return references


def validate_workflow(data: Any, raw_text: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["top level must be an object"]
    for key in ("name", "nodes", "connections"):
        if key not in data:
            errors.append(f"missing top-level key '{key}'")
    if errors:
        return errors
    nodes = data["nodes"]
    connections = data["connections"]
    if not isinstance(nodes, list) or not nodes:
        return ["'nodes' must be a non-empty list"]

    names: set[str] = set()
    ids: set[str] = set()
    for node in nodes:
        missing = [key for key in NODE_REQUIRED_KEYS if key not in node]
        if missing:
            errors.append(f"node {node.get('name', '?')!r} missing keys: {', '.join(missing)}")
            continue
        if node["name"] in names:
            errors.append(f"duplicate node name {node['name']!r}")
        if node["id"] in ids:
            errors.append(f"duplicate node id {node['id']!r}")
        names.add(node["name"])
        ids.add(node["id"])

    for source, target in _connection_targets(connections):
        for endpoint in (source, target):
            if endpoint not in names:
                errors.append(f"connection references unknown node {endpoint!r}")

    connected = {name for pair in _connection_targets(connections) for name in pair}
    for node in nodes:
        name = node.get("name")
        if node.get("type") == "n8n-nodes-base.stickyNote" or name is None:
            continue
        if name not in connected and len(nodes) > 1:
            errors.append(f"node {name!r} is disconnected")

    if not any(_is_trigger(node.get("type", "")) for node in nodes):
        errors.append("workflow has no trigger node")

    for node in nodes:
        if node.get("type") == HTTP_NODE:
            errors.extend(_http_errors(node))
    if data.get("active") is not False:
        errors.append("workflow must be imported inactive ('active': false)")

    for pattern in SECRET_PATTERNS:
        match = pattern.search(raw_text)
        if match:
            errors.append(f"possible hardcoded secret or personal data: {match.group(0)[:40]!r}")
    return errors


def validate_directory(directory: Path) -> dict[str, list[str]]:
    results: dict[str, list[str]] = {}
    files = sorted(directory.glob("*.json"))
    if not files:
        return {str(directory): ["no workflow JSON files found"]}
    parsed: dict[str, dict[str, Any]] = {}
    for path in files:
        raw = path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            results[path.name] = [f"invalid JSON: {exc}"]
            continue
        results[path.name] = validate_workflow(data, raw)
        if isinstance(data, dict):
            parsed[path.name] = data
    _check_workflow_ids(parsed, results)
    return results


def _check_workflow_ids(parsed: dict[str, dict[str, Any]], results: dict[str, list[str]]) -> None:
    owners: dict[str, str] = {}
    for name, data in parsed.items():
        workflow_id = data.get("id")
        if not isinstance(workflow_id, str) or not workflow_id:
            results[name].append("missing workflow 'id' (needed for cross-workflow references)")
        elif workflow_id in owners:
            results[name].append(
                f"workflow id {workflow_id!r} is also used by {owners[workflow_id]}"
            )
        else:
            owners[workflow_id] = name
    for name, data in parsed.items():
        for reference in _referenced_workflow_ids(data):
            if reference not in owners:
                results[name].append(f"references unknown workflow id {reference!r}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    directory = Path(args[0]) if args else DEFAULT_DIR
    console = Console()
    results = validate_directory(directory)
    failed = False
    for name, errors in results.items():
        if errors:
            failed = True
            console.print(f"[red]FAIL[/red] {name}")
            for error in errors:
                console.print(f"    - {error}")
        else:
            console.print(f"[green]OK[/green]   {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
