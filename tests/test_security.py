from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = [ROOT / "core.py", ROOT / "decisionbook.py", ROOT / "__main__.py"]
EXPECTED_CAPABILITIES = {"interaction:respond", "storage:kv"}
ALLOWED_CTX_SURFACES = {"ephemeral", "interaction", "kv", "log", "metrics"}
ALLOWED_DECORATORS = {
    "on_component",
    "on_dashboard",
    "on_install",
    "on_modal_submit",
    "on_slash_command",
}
FORBIDDEN_IMPORTS = {
    "aiohttp",
    "ctypes",
    "dbm",
    "httpx",
    "importlib",
    "marshal",
    "multiprocessing",
    "pickle",
    "requests",
    "shelve",
    "socket",
    "sqlite3",
    "subprocess",
    "urllib",
    "websockets",
}
FORBIDDEN_CALLS = {
    ("__import__",),
    ("breakpoint",),
    ("builtins", "compile"),
    ("builtins", "eval"),
    ("builtins", "exec"),
    ("builtins", "open"),
    ("compile",),
    ("delattr",),
    ("eval",),
    ("exec",),
    ("getattr",),
    ("globals",),
    ("input",),
    ("locals",),
    ("open",),
    ("os", "popen"),
    ("os", "system"),
    ("setattr",),
    ("vars",),
}


def parsed_files() -> list[tuple[Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))) for path in RUNTIME
    ]


def call_path(node: ast.expr) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        parent = call_path(node.value)
        if parent is not None:
            return (*parent, node.attr)
    return None


def test_all_project_python_compiles():
    paths = [
        *RUNTIME,
        *sorted((ROOT / "tools").glob("*.py")),
        *sorted((ROOT / "tests").glob("*.py")),
    ]
    for path in paths:
        compile(path.read_text(encoding="utf-8"), str(path), "exec", dont_inherit=True)


def test_exact_capabilities_and_no_proxy_domains():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    declared = manifest["capabilities_required"]
    assert len(declared) == len(set(declared))
    assert set(declared) == EXPECTED_CAPABILITIES
    assert "proxy_domains_requested" not in manifest


def test_no_forbidden_imports_or_dangerous_calls():
    for path, tree in parsed_files():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name.split(".")[0] for alias in node.names}
                assert not imported & FORBIDDEN_IMPORTS, path
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in FORBIDDEN_IMPORTS, path
            elif isinstance(node, ast.Call):
                assert call_path(node.func) not in FORBIDDEN_CALLS, path


def test_runtime_uses_only_approved_context_and_handler_surfaces():
    ctx_surfaces: set[str] = set()
    decorators: set[str] = set()
    for _, tree in parsed_files():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                continue
            if node.value.id == "ctx":
                ctx_surfaces.add(node.attr)
            elif node.value.id == "plugin" and node.attr.startswith("on_"):
                decorators.add(node.attr)

    assert ctx_surfaces <= ALLOWED_CTX_SURFACES
    assert {"interaction", "kv"} <= ctx_surfaces
    assert decorators <= ALLOWED_DECORATORS


def test_ephemeral_is_only_the_scoped_close_dedup_guard():
    tree = ast.parse((ROOT / "decisionbook.py").read_text(encoding="utf-8"))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    surfaces = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and call_path(node) == ("ctx", "ephemeral")
    ]
    assert len(surfaces) == 1
    method = parents[surfaces[0]]
    assert isinstance(method, ast.Attribute) and method.attr == "dedup"
    call = parents[method]
    assert isinstance(call, ast.Call) and call.func is method
    owners = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and call in set(ast.walk(node))
    ]
    assert owners == ["decision_close"]
    assert len(call.args) == 1 and isinstance(call.args[0], ast.JoinedStr)
    assert any(
        isinstance(value, ast.Constant) and value.value == "decisionbook:close:"
        for value in call.args[0].values
    )
    assert any(
        isinstance(value, ast.FormattedValue)
        and isinstance(value.value, ast.Name)
        and value.value.id == "decision_id"
        for value in call.args[0].values
    )
    assert len(call.keywords) == 1
    assert call.keywords[0].arg == "ttl_seconds"
    assert isinstance(call.keywords[0].value, ast.Constant)
    assert call.keywords[0].value.value == 15


def test_every_response_and_followup_suppresses_mentions():
    tree = ast.parse((ROOT / "decisionbook.py").read_text(encoding="utf-8"))
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "NO_MENTIONS" for target in node.targets
        )
    ]
    assert len(assignments) == 1
    assert ast.literal_eval(assignments[0].value) == {"parse": []}

    responses = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if call_path(node.func) not in {
            ("ctx", "interaction", "followup"),
            ("ctx", "interaction", "respond"),
        }:
            continue
        responses.append(node)
        guards = [keyword.value for keyword in node.keywords if keyword.arg == "allowed_mentions"]
        assert len(guards) == 1, f"response at line {node.lineno} lacks an allowed_mentions guard"
        assert isinstance(guards[0], ast.Name) and guards[0].id == "NO_MENTIONS"
    assert responses


def test_no_authorization_headers_credentials_or_secret_surfaces():
    constants = {
        node.value.casefold()
        for _, tree in parsed_files()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    source_strings = "\n".join(constants)
    for token in ("authorization", "secret_auth", "api_key", "api-key", "bearer "):
        assert token not in source_strings


def test_release_audit_is_explicit_and_uses_public_sdk_cli():
    path = ROOT / "tools" / "run_audit.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree))
    assert "yourbot_sdk._validation" not in source
    assert "from yourbot_sdk.cli import main" in source
