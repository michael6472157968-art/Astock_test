from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import yaml

from app.services import ai_skill_registry
from app.services.ai_skill_registry import (
    _validate_skill_payload,
    install_prompt_skill,
    list_skills,
    render_prompt_template,
)
from app.services.ai_tool_registry import MCP_AGENT_TOOLS, TOOLS


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _mcp_tool_names() -> set[str]:
    server_path = BACKEND_ROOT / "mcp_server" / "src" / "quantdinger_mcp" / "server.py"
    tree = ast.parse(server_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "MCP_TOOL_NAMES" for target in node.targets):
                return set(ast.literal_eval(node.value))
    raise AssertionError("MCP_TOOL_NAMES is missing")


def _normalize_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{param}", path)


def _agent_route_operations() -> set[tuple[str, str]]:
    routes_root = BACKEND_ROOT / "backend_api_python" / "app" / "routes" / "agent_v1"
    operations: set[tuple[str, str]] = set()
    for route_file in routes_root.glob("*.py"):
        if route_file.name == "me_tokens.py":
            continue
        tree = ast.parse(route_file.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "route"
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                ):
                    continue
                suffix = str(decorator.args[0].value)
                suffix = re.sub(r"<(?:[^:>]+:)?([^>]+)>", r"{\1}", suffix)
                methods = {"GET"}
                for keyword in decorator.keywords:
                    if keyword.arg == "methods":
                        methods = {str(value).upper() for value in ast.literal_eval(keyword.value)}
                operations.update((method, f"/api/agent/v1{suffix}") for method in methods)
    return operations


def test_agent_openapi_matches_registered_route_source():
    agent_paths = json.loads(
        (BACKEND_ROOT / "docs" / "agent" / "agent-openapi.json").read_text(encoding="utf-8")
    )["paths"]
    documented = {
        (method.upper(), path)
        for path, item in agent_paths.items()
        for method in item
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }
    assert _agent_route_operations() == documented


def test_mcp_metadata_matches_exported_tools_and_documented_routes():
    metadata_names = {tool.id.removeprefix("mcp.") for tool in MCP_AGENT_TOOLS}
    assert metadata_names == _mcp_tool_names()

    agent_paths = json.loads(
        (BACKEND_ROOT / "docs" / "agent" / "agent-openapi.json").read_text(encoding="utf-8")
    )["paths"]
    human_paths = yaml.safe_load(
        (BACKEND_ROOT / "docs" / "api" / "openapi.yaml").read_text(encoding="utf-8")
    )["paths"]
    documented = {_normalize_path(path) for path in (*agent_paths, *human_paths)}
    for tool in (*TOOLS, *MCP_AGENT_TOOLS):
        if tool.route and tool.route.startswith("/api/"):
            assert _normalize_path(tool.route) in documented, tool.id


def test_builtin_skill_registry_does_not_advertise_retired_experiments():
    skills = list_skills("en-US")
    by_id = {item["id"]: item for item in skills}
    assert "parameter_tuning" not in by_id
    assert "regime_detection" not in by_id
    assert by_id["news_research"]["requires"] == ["web_search"]
    assert "experiment" not in by_id["job_monitor"]["description"].lower()
    assert "symbol" not in by_id["backtest_runner"]["requires"]
    assert "timeframe" not in by_id["backtest_runner"]["requires"]


def test_prompt_skill_manifest_rejects_executable_or_action_fields():
    base = {
        "id": "safe_prompt",
        "kind": "prompt",
        "label": {"en": "Safe"},
        "prompt_template": "Review {symbol_label}",
    }
    assert _validate_skill_payload(base)[0] is True

    workflow = {**base, "action_type": "workflow"}
    assert _validate_skill_payload(workflow) == (
        False,
        "installed skills must use action_type=prompt",
    )

    nested_command = {**base, "ui": {"command": "do-something"}}
    assert _validate_skill_payload(nested_command)[0] is False

    unknown_placeholder = {**base, "prompt_template": "Review {account_id}"}
    assert _validate_skill_payload(unknown_placeholder)[0] is False

    external_route = {**base, "route": "https://example.com"}
    assert _validate_skill_payload(external_route)[0] is False


def test_installed_prompt_skill_is_always_non_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_skill_registry, "USER_SKILLS_DIR", tmp_path)
    payload = {
        "id": "safe_prompt",
        "kind": "prompt",
        "label": {"en": "Safe"},
        "prompt_template": "Review {symbol_label}",
        "priority": 50,
    }
    install_prompt_skill(payload)
    installed = next(item for item in list_skills("en-US") if item["id"] == "safe_prompt")
    assert installed["action_type"] == "prompt"
    assert render_prompt_template(ai_skill_registry.get_skill("safe_prompt"), "en-US", {"symbol": "SPY"}) == "Review SPY"
