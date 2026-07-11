#!/usr/bin/env python3
"""Run DecisionBook's independent, fail-closed release audit."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import tempfile
import zipfile
from builtins import compile as compile_source
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

try:  # Support both ``python -m tools.run_audit`` and direct execution.
    from .build_bundle import OUTPUT, RUNTIME_FILES, artifact_filename, build
    from .validate_bundle import validate as validate_bundle
except ImportError:  # pragma: no cover - direct script execution
    from build_bundle import OUTPUT, RUNTIME_FILES, artifact_filename, build
    from validate_bundle import validate as validate_bundle


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = [ROOT / "core.py", ROOT / "decisionbook.py", ROOT / "__main__.py"]
EXPECTED_CAPS = {"interaction:respond", "storage:kv"}
EXPECTED_SUBCOMMANDS = {"add", "close", "help", "list", "view"}
ALLOWED_CTX_SURFACES = {"ephemeral", "interaction", "kv", "log", "metrics"}
ALLOWED_PLUGIN_DECORATORS = {
    "on_component",
    "on_dashboard",
    "on_install",
    "on_modal_submit",
    "on_ready",
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


class AuditError(RuntimeError):
    """Raised when a release invariant is not satisfied."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditError(message)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditError(f"Could not read {path}: {exc}") from exc
    require(isinstance(value, dict), f"{path} must contain a JSON object")
    return value


def trees() -> list[tuple[Path, ast.Module]]:
    parsed: list[tuple[Path, ast.Module]] = []
    for path in RUNTIME:
        source = path.read_text(encoding="utf-8")
        parsed.append((path, ast.parse(source, filename=str(path))))
    return parsed


def call_path(node: ast.expr) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        parent = call_path(node.value)
        if parent is not None:
            return (*parent, node.attr)
    return None


def gate_syntax() -> None:
    paths = [
        *RUNTIME,
        *sorted((ROOT / "tools").glob("*.py")),
        *sorted((ROOT / "tests").glob("*.py")),
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        compile_source(source, str(path), "exec", dont_inherit=True)


def gate_tests() -> None:
    import pytest

    code = pytest.main([str(ROOT / "tests"), "-q", "-p", "no:cacheprovider"])
    require(code == pytest.ExitCode.OK, f"pytest failed with exit code {int(code)}")


def _manifest_version(manifest: dict[str, Any]) -> str:
    require(manifest.get("id") == "decisionbook", "manifest plugin id must be decisionbook")
    manifest_version = manifest.get("version")
    require(
        isinstance(manifest_version, str)
        and re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?",
            manifest_version,
        )
        is not None,
        "manifest version must be semantic",
    )
    return manifest_version


def _root_subcommands(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    commands = manifest.get("slash_commands")
    require(isinstance(commands, list), "manifest slash_commands must be a list")
    require(
        len(commands) == 1 and isinstance(commands[0], dict),
        "manifest must declare one root command",
    )
    root_command = commands[0]
    require(
        root_command.get("name") == "decision",
        "the root slash command must be /decision",
    )
    subcommands = root_command.get("options")
    require(isinstance(subcommands, list), "/decision options must contain subcommands")
    require(
        all(isinstance(item, dict) and item.get("type") == 1 for item in subcommands),
        "every /decision option must be a type-1 subcommand",
    )
    subcommand_names = {
        item.get("name") for item in subcommands if isinstance(item.get("name"), str)
    }
    require(
        len(subcommands) == len(subcommand_names),
        "/decision subcommand names must be unique strings",
    )
    require(
        subcommand_names == EXPECTED_SUBCOMMANDS,
        f"unexpected /decision subcommands: {sorted(subcommand_names ^ EXPECTED_SUBCOMMANDS)}",
    )
    return subcommands


def _subcommand_options(
    subcommands: list[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    indexed: dict[str, dict[str, dict[str, Any]]] = {}
    for subcommand in subcommands:
        name = subcommand["name"]
        options = subcommand.get("options", [])
        require(isinstance(options, list), f"/decision {name} options must be a list")
        option_names = [
            item.get("name")
            for item in options
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        require(
            len(options) == len(option_names) == len(set(option_names)),
            f"/decision {name} has malformed or duplicate options",
        )
        require(
            all(item.get("type") not in {1, 2} for item in options),
            f"/decision {name} cannot nest subcommands",
        )
        indexed[name] = {option["name"]: option for option in options}
    return indexed


def _validate_empty_subcommand_options(
    indexed: dict[str, dict[str, dict[str, Any]]],
) -> None:
    require(
        not indexed["add"] and not indexed["help"],
        "/decision add and /decision help must not expose redundant slash options",
    )


def _validate_id_subcommand_options(
    indexed: dict[str, dict[str, dict[str, Any]]],
) -> None:
    for subcommand_name in ("view", "close"):
        options = indexed[subcommand_name]
        require(
            set(options) == {"id"},
            f"/decision {subcommand_name} must declare exactly the id option",
        )
        identifier = options["id"]
        require(
            identifier.get("type") == 4
            and identifier.get("required") is True
            and identifier.get("min_value") == 1,
            f"/decision {subcommand_name} id must be a required positive integer",
        )


def _validate_status_option(status_option: dict[str, Any]) -> None:
    status_choices = status_option.get("choices")
    choice_values = (
        {choice.get("value") for choice in status_choices if isinstance(choice, dict)}
        if isinstance(status_choices, list)
        else set()
    )
    require(
        status_option.get("type") == 3
        and status_option.get("required") is False
        and isinstance(status_choices, list)
        and choice_values == {"all", "open", "closed"}
        and len(status_choices) == 3,
        "/decision list status must offer exactly all, open, and closed",
    )


def _validate_list_subcommand_options(
    indexed: dict[str, dict[str, dict[str, Any]]],
) -> None:
    list_options = indexed["list"]
    require(
        set(list_options) == {"query", "status", "limit"},
        "/decision list must declare query, status, and limit options",
    )
    query_option = list_options["query"]
    require(
        query_option.get("type") == 3
        and query_option.get("required") is False
        and query_option.get("max_length") == 80,
        "/decision list query must be an optional string capped at 80 characters",
    )
    _validate_status_option(list_options["status"])
    limit_option = list_options["limit"]
    require(
        limit_option.get("type") == 4
        and limit_option.get("required") is False
        and limit_option.get("min_value") == 1
        and limit_option.get("max_value") == 10,
        "/decision list limit must be an optional integer from 1 to 10",
    )


def _literal_decorator_names(
    tree: ast.Module,
    *,
    decorator_name: str,
    label: str,
) -> list[str]:
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or call_path(decorator.func) != (
                "plugin",
                decorator_name,
            ):
                continue
            require(
                len(decorator.args) == 1,
                f"{label} decorator on {node.name} needs one literal name",
            )
            try:
                handler_name = ast.literal_eval(decorator.args[0])
            except (TypeError, ValueError) as exc:
                raise AuditError(
                    f"{label} decorator on {node.name} must use a literal name"
                ) from exc
            require(
                isinstance(handler_name, str),
                f"{label} decorator on {node.name} has a non-string name",
            )
            names.append(handler_name)
    return names


def _decisionbook_tree() -> ast.Module:
    path = ROOT / "decisionbook.py"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def gate_manifest() -> None:
    manifest = load_json(ROOT / "manifest.json")
    manifest_version = _manifest_version(manifest)
    indexed = _subcommand_options(_root_subcommands(manifest))
    _validate_empty_subcommand_options(indexed)
    _validate_id_subcommand_options(indexed)
    _validate_list_subcommand_options(indexed)
    handler_names = _literal_decorator_names(
        _decisionbook_tree(),
        decorator_name="on_slash_command",
        label="slash-command",
    )
    require(
        handler_names == ["decision"],
        "decisionbook.py must declare exactly one @plugin.on_slash_command('decision') handler",
    )
    require(
        artifact_filename() == f"decisionbook-{manifest_version}.zip",
        "artifact name must derive from manifest",
    )


def _validate_declared_capabilities(manifest: dict[str, Any]) -> None:
    declared = manifest.get("capabilities_required")
    require(isinstance(declared, list), "capabilities_required must be a list")
    require(len(declared) == len(set(declared)), "capabilities_required contains duplicates")
    require(
        set(declared) == EXPECTED_CAPS,
        f"capabilities must be exactly {sorted(EXPECTED_CAPS)}",
    )
    require(
        "proxy_domains_requested" not in manifest,
        "DecisionBook must not request proxy domains",
    )


def _runtime_surface_usage(
    parsed_trees: list[tuple[Path, ast.Module]],
) -> tuple[set[str], set[str]]:
    ctx_surfaces: set[str] = set()
    decorators: set[str] = set()
    for _path, tree in parsed_trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                continue
            if node.value.id == "ctx":
                ctx_surfaces.add(node.attr)
            elif node.value.id == "plugin" and node.attr.startswith("on_"):
                decorators.add(node.attr)
    return ctx_surfaces, decorators


def _validate_runtime_surface_usage(
    parsed_trees: list[tuple[Path, ast.Module]],
) -> None:
    ctx_surfaces, decorators = _runtime_surface_usage(parsed_trees)
    unexpected_surfaces = ctx_surfaces - ALLOWED_CTX_SURFACES
    require(
        not unexpected_surfaces,
        f"out-of-scope ctx surfaces used: {sorted(unexpected_surfaces)}",
    )
    require("interaction" in ctx_surfaces, "interaction capability is declared but unused")
    require("kv" in ctx_surfaces, "KV capability is declared but unused")
    unexpected_decorators = decorators - ALLOWED_PLUGIN_DECORATORS
    require(
        not unexpected_decorators,
        f"out-of-scope plugin decorators used: {sorted(unexpected_decorators)}",
    )


def _ast_parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _ephemeral_dedup_call(tree: ast.Module) -> ast.Call:
    parents = _ast_parents(tree)
    ephemeral_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and call_path(node) == ("ctx", "ephemeral")
    ]
    require(
        len(ephemeral_nodes) == 1,
        "ctx.ephemeral must be used exactly once for the close dedup guard",
    )
    ephemeral_node = ephemeral_nodes[0]
    method_node = parents.get(ephemeral_node)
    require(
        isinstance(method_node, ast.Attribute)
        and method_node.value is ephemeral_node
        and method_node.attr == "dedup",
        "ctx.ephemeral may only access dedup",
    )
    call_node = parents.get(method_node)
    require(
        isinstance(call_node, ast.Call) and call_node.func is method_node,
        "ctx.ephemeral.dedup must be called directly",
    )
    return call_node


def _validate_ephemeral_owner(tree: ast.Module, call_node: ast.Call) -> None:
    owners = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and call_node in set(ast.walk(node))
    ]
    require(
        owners == ["decision_close"],
        "ctx.ephemeral.dedup is restricted to decision_close",
    )


def _validate_ephemeral_key(call_node: ast.Call) -> None:
    require(
        len(call_node.args) == 1 and isinstance(call_node.args[0], ast.JoinedStr),
        "close dedup must be scoped to the current DecisionBook decision ID",
    )
    values = call_node.args[0].values
    has_prefix = any(
        isinstance(value, ast.Constant) and value.value == "decisionbook:close:" for value in values
    )
    has_decision_id = any(
        isinstance(value, ast.FormattedValue)
        and isinstance(value.value, ast.Name)
        and value.value.id == "decision_id"
        for value in values
    )
    require(
        has_prefix and has_decision_id,
        "close dedup must be scoped to the current DecisionBook decision ID",
    )


def _validate_ephemeral_ttl(call_node: ast.Call) -> None:
    ttl_values = [keyword.value for keyword in call_node.keywords if keyword.arg == "ttl_seconds"]
    require(
        len(call_node.keywords) == 1
        and len(ttl_values) == 1
        and isinstance(ttl_values[0], ast.Constant)
        and ttl_values[0].value == 15,
        "close dedup must use the documented 15-second TTL",
    )


def gate_capabilities() -> None:
    manifest = load_json(ROOT / "manifest.json")
    _validate_declared_capabilities(manifest)
    parsed_trees = trees()
    _validate_runtime_surface_usage(parsed_trees)
    decisionbook_tree = next(tree for path, tree in parsed_trees if path.name == "decisionbook.py")
    dedup_call = _ephemeral_dedup_call(decisionbook_tree)
    _validate_ephemeral_owner(decisionbook_tree, dedup_call)
    _validate_ephemeral_key(dedup_call)
    _validate_ephemeral_ttl(dedup_call)


def gate_imports() -> None:
    for path, tree in trees():
        for node in ast.walk(tree):
            imported: set[str] = set()
            if isinstance(node, ast.Import):
                imported = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = {node.module.split(".")[0]}
            forbidden = imported & FORBIDDEN_IMPORTS
            require(not forbidden, f"{path} imports forbidden modules: {sorted(forbidden)}")


def gate_builtins() -> None:
    for path, tree in trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = call_path(node.func)
            require(
                target not in FORBIDDEN_CALLS,
                f"{path} calls forbidden runtime surface: {'.'.join(target or ())}",
            )


def gate_mentions() -> None:
    tree = next(tree for path, tree in trees() if path.name == "decisionbook.py")
    constants = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "NO_MENTIONS" for target in node.targets
        )
    ]
    require(len(constants) == 1, "NO_MENTIONS must be assigned exactly once")
    try:
        no_mentions = ast.literal_eval(constants[0].value)
    except (TypeError, ValueError) as exc:
        raise AuditError("NO_MENTIONS must be a static literal") from exc
    require(
        no_mentions == {"parse": []},
        "NO_MENTIONS must disable all Discord mention parsing",
    )

    response_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = call_path(node.func)
        if target not in {
            ("ctx", "interaction", "respond"),
            ("ctx", "interaction", "followup"),
        }:
            continue
        response_calls += 1
        guards = [keyword.value for keyword in node.keywords if keyword.arg == "allowed_mentions"]
        require(
            len(guards) == 1,
            f"interaction response at line {node.lineno} lacks allowed_mentions",
        )
        guard = guards[0]
        require(
            isinstance(guard, ast.Name) and guard.id == "NO_MENTIONS",
            f"interaction response at line {node.lineno} must use NO_MENTIONS",
        )
    require(response_calls > 0, "no interaction response calls were found")


def gate_entrypoint() -> None:
    tree = ast.parse((ROOT / "__main__.py").read_text(encoding="utf-8"), filename="__main__.py")
    require(bool(tree.body), "__main__.py is empty")
    last = tree.body[-1]
    require(
        isinstance(last, ast.Expr) and isinstance(last.value, ast.Call),
        "plugin.run() must be last",
    )
    target = call_path(last.value.func)
    require(
        target == ("plugin", "run"),
        "plugin.run() must be the final executable statement",
    )
    require(
        not last.value.args and not last.value.keywords,
        "plugin.run() must not receive arguments",
    )
    run_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and call_path(node.func) == ("plugin", "run")
    ]
    require(len(run_calls) == 1, "__main__.py must call plugin.run() exactly once")


def gate_dashboard() -> None:
    manifest = load_json(ROOT / "dashboard_manifest.json")
    pages = manifest.get("pages")
    require(isinstance(pages, list), "dashboard pages must be a list")
    expected: set[str] = set()
    for page in pages:
        require(
            isinstance(page, dict) and isinstance(page.get("widgets"), list),
            "dashboard page is malformed",
        )
        for widget in page["widgets"]:
            require(isinstance(widget, dict), "dashboard widget is malformed")
            rpc_method = widget.get("rpc_method")
            require(
                isinstance(rpc_method, str) and rpc_method.startswith("dashboard."),
                "dashboard rpc_method must start with dashboard.",
            )
            expected.add(rpc_method.removeprefix("dashboard."))

    source = ast.parse((ROOT / "decisionbook.py").read_text(encoding="utf-8"))
    actual: set[str] = set()
    for node in source.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or call_path(decorator.func) != (
                "plugin",
                "on_dashboard",
            ):
                continue
            require(
                len(decorator.args) == 1,
                f"dashboard decorator on {node.name} needs one name",
            )
            try:
                handler_name = ast.literal_eval(decorator.args[0])
            except (TypeError, ValueError) as exc:
                raise AuditError(
                    f"dashboard decorator on {node.name} must use a literal name"
                ) from exc
            require(
                isinstance(handler_name, str),
                f"dashboard decorator on {node.name} has a non-string name",
            )
            require(
                handler_name not in actual,
                f"duplicate dashboard handler: {handler_name}",
            )
            actual.add(handler_name)
    require(expected == actual, f"dashboard RPC mismatch: {sorted(expected ^ actual)}")


def run_sdk_cli(command: str, path: Path) -> None:
    from yourbot_sdk.cli import main as yourbot_main

    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = yourbot_main([command, "--path", str(path)])
    except SystemExit as exc:
        code = int(exc.code or 0)
    output = "\n".join(
        part.strip() for part in (stdout.getvalue(), stderr.getvalue()) if part.strip()
    )
    if output:
        for line in output.splitlines():
            print(f"  {line}")
    require(code == 0, f"yourbot {command} failed with exit code {code}")
    warning_match = re.search(r"\b([1-9][0-9]*) warning\(s\)", output)
    require(warning_match is None, f"yourbot {command} reported unresolved warnings")


def gate_sdk_validate() -> None:
    run_sdk_cli("validate", ROOT)


def gate_sdk_version() -> None:
    installed = version("yourbot-sdk")
    match = re.fullmatch(r"0\.8\.([0-9]+)", installed)
    require(
        match is not None and int(match.group(1)) >= 3,
        f"yourbot-sdk {installed} is outside the supported >=0.8.3,<0.9 range",
    )
    requirements = {
        line.strip().replace(" ", "")
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    require(
        "yourbot-sdk>=0.8.3,<0.9" in requirements,
        "requirements.txt must constrain yourbot-sdk to >=0.8.3,<0.9",
    )


def gate_sdk_doctor() -> None:
    run_sdk_cli("doctor", ROOT)


def gate_build() -> None:
    first = build(OUTPUT)
    digest1 = hashlib.sha256(first.read_bytes()).hexdigest()
    second = build(OUTPUT)
    digest2 = hashlib.sha256(second.read_bytes()).hexdigest()
    require(digest1 == digest2, "bundle is not deterministic")


def gate_bundle() -> None:
    validate_bundle(OUTPUT)
    artifacts = sorted((ROOT / "dist").glob("*.zip"))
    require(
        artifacts == [OUTPUT],
        f"dist must contain only the current artifact; found {[path.name for path in artifacts]}",
    )
    with zipfile.ZipFile(OUTPUT) as archive:
        names = {item.filename for item in archive.infolist()}
        require(
            names == set(RUNTIME_FILES),
            "built bundle does not match the runtime allowlist",
        )
        for name in RUNTIME_FILES:
            require(
                archive.read(name) == (ROOT / name).read_bytes(),
                f"stale bundle entry: {name}",
            )


def gate_staged_validation() -> None:
    with tempfile.TemporaryDirectory(prefix="decisionbook-audit-") as temporary:
        stage = Path(temporary)
        with zipfile.ZipFile(OUTPUT) as archive:
            for name in RUNTIME_FILES:
                (stage / name).write_bytes(archive.read(name))
        run_sdk_cli("validate", stage)


Gate = tuple[str, Callable[[], None]]
GATES: list[Gate] = [
    ("compile", gate_syntax),
    ("tests", gate_tests),
    ("manifest", gate_manifest),
    ("capabilities", gate_capabilities),
    ("forbidden-imports", gate_imports),
    ("dangerous-builtins", gate_builtins),
    ("mention-suppression", gate_mentions),
    ("plugin-run", gate_entrypoint),
    ("dashboard-parity", gate_dashboard),
    ("yourbot-sdk-version", gate_sdk_version),
    ("yourbot-validate", gate_sdk_validate),
    ("yourbot-doctor", gate_sdk_doctor),
    ("bundle-build", gate_build),
    ("bundle-validation", gate_bundle),
    ("staged-yourbot-validate", gate_staged_validation),
]


def main() -> None:
    try:
        sdk_version = version("yourbot-sdk")
    except PackageNotFoundError as exc:
        print("FAIL: yourbot-sdk is not installed")
        raise SystemExit(1) from exc

    print(f"DecisionBook audit · yourbot-sdk {sdk_version} · {len(GATES)} gates")
    try:
        for index, (name, function) in enumerate(GATES, 1):
            print(f"[{index:02d}/{len(GATES)}] {name}")
            function()
            print("  PASS")
    except Exception as exc:
        print(f"  FAIL: {type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc
    print("AUDIT PASS")


if __name__ == "__main__":
    main()
