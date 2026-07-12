from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    return json.loads((ROOT / name).read_text())


def nested_commands():
    root = load("manifest.json")["slash_commands"]
    assert len(root) == 1
    assert root[0]["name"] == "decision"
    return {item["name"]: item for item in root[0]["options"]}


def test_manifest_identity_version_and_exact_capabilities():
    manifest = load("manifest.json")
    assert manifest["id"] == "decisionbook"
    assert manifest["name"] == "DecisionBook"
    assert manifest["version"] == "0.3.0"
    assert manifest["author"] == "CubCadet"
    assert manifest["icon_url"] == (
        "https://raw.githubusercontent.com/CubCadet/"
        "yourbot-plugin-decisionbook/v0.3.0/brand/decisionbook-icon-512.png"
    )
    assert set(manifest["capabilities_required"]) == {
        "interaction:respond",
        "storage:kv",
    }
    assert "proxy_domains_requested" not in manifest


def test_one_root_command_with_exact_subcommands():
    commands = nested_commands()
    assert set(commands) == {"add", "view", "list", "close", "help"}
    assert all(command["type"] == 1 for command in commands.values())
    assert commands["add"]["options"] == []
    assert commands["help"]["options"] == []


def test_nested_option_types_and_ui_constraints():
    commands = nested_commands()
    view_id = commands["view"]["options"][0]
    close_id = commands["close"]["options"][0]
    assert view_id["type"] == close_id["type"] == 4
    assert view_id["required"] is close_id["required"] is True
    assert view_id["min_value"] == close_id["min_value"] == 1

    list_options = {item["name"]: item for item in commands["list"]["options"]}
    assert set(list_options) == {"query", "status", "limit"}
    assert list_options["query"]["type"] == 3
    assert list_options["query"]["max_length"] == 80
    assert list_options["query"]["required"] is False
    assert {choice["value"] for choice in list_options["status"]["choices"]} == {
        "all",
        "open",
        "closed",
    }
    assert list_options["limit"]["type"] == 4
    assert list_options["limit"]["min_value"] == 1
    assert list_options["limit"]["max_value"] == 10


def test_runtime_registers_root_components_and_exact_modals():
    source = (ROOT / "decisionbook.py").read_text()
    tree = ast.parse(source)
    slash = []
    modal = []
    prefixes = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if decorator.func.attr == "on_slash_command":
                slash.append(ast.literal_eval(decorator.args[0]))
            elif decorator.func.attr == "on_modal_submit":
                modal.append(ast.literal_eval(decorator.args[0]))
            elif decorator.func.attr == "on_component":
                prefix = next(
                    (
                        ast.literal_eval(item.value)
                        for item in decorator.keywords
                        if item.arg == "prefix"
                    ),
                    None,
                )
                prefixes.append(prefix)
    assert slash == ["decision"]
    assert set(modal) == {"decision:add", "decision:close"}
    assert set(prefixes) == {"decision:view:", "decision:close:", "decision:page:"}


def test_dashboard_rpc_names_match_python_handlers():
    dashboard = load("dashboard_manifest.json")
    rpc_names = {
        widget["rpc_method"].removeprefix("dashboard.")
        for page in dashboard["pages"]
        for widget in page["widgets"]
    }
    source = ast.parse((ROOT / "decisionbook.py").read_text())
    handlers = set()
    for node in source.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "on_dashboard"
            ):
                handlers.add(ast.literal_eval(decorator.args[0]))
    assert rpc_names == handlers


def test_dashboard_is_read_only_and_responsive():
    dashboard = load("dashboard_manifest.json")
    page = dashboard["pages"][0]
    assert page["permission"] == "manager"
    widgets = page["widgets"]
    assert {item["type"] for item in widgets} == {
        "alert",
        "markdown",
        "stat_card",
        "table",
    }
    assert all("save_method" not in item for item in widgets)
    alert = next(item for item in widgets if item["type"] == "alert")
    assert alert["rpc_method"] == "dashboard.get_storage_health"
    table = next(item for item in widgets if item["type"] == "table")
    assert [column["key"] for column in table["columns"]] == [
        "id",
        "status",
        "summary",
        "recorded",
        "closed",
    ]


def test_entrypoint_calls_plugin_run_last():
    source = ast.parse((ROOT / "__main__.py").read_text())
    last = source.body[-1]
    assert isinstance(last, ast.Expr)
    assert isinstance(last.value, ast.Call)
    assert isinstance(last.value.func, ast.Attribute)
    assert last.value.func.attr == "run"
