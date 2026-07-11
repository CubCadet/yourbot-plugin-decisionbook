# DecisionBook agent guidance

Read [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) before changing this project. It is the durable
handoff for product decisions, the v0.2.0 architecture, platform constraints, and release procedure.

## Product contract

DecisionBook is a lightweight YourBot Marketplace plugin that records immutable, server-scoped
decisions. Preserve these principles unless the user explicitly changes the product direction:

- Original decision fields are immutable; closure appends an outcome.
- Only the original author may close a decision in v0.2.x.
- Never add destructive edit/delete behavior casually.
- Keep runtime dependencies to `yourbot-sdk` plus the Python standard library.
- Keep the declared capability set exactly `interaction:respond` and `storage:kv` unless the user
  explicitly approves an expansion after seeing its privacy and review implications.
- `ctx.ephemeral.dedup` is permitted only as the decision-scoped, 15-second close-race guard. The
  SDK requires no capability for it; do not treat it as durable or expand its use silently.
- Do not add HTTP, SQL, message-content listeners, schedules, Discord REST actions, proxy domains,
  secrets, or elevated permissions to solve a problem that fits the existing design.
- Every interaction response must suppress mentions with `allowed_mentions={"parse": []}`.
- Treat stored KV objects, component IDs, modal values, and dashboard parameters as untrusted.
- Preserve the 50-key maximum for each `get_many` batch and keep user-facing work bounded for the
  64 MB / 0.25 vCPU sandbox.
- Do not claim compare-and-swap or transactional closure guarantees; YourBot KV does not expose
  that primitive and the ephemeral dedup guard is best-effort.

## Structure

- `core.py` — pure normalization, validation, schema, filtering, and safe rendering rules.
- `decisionbook.py` — `/decision` dispatch, add/close modals, view/list/close components, KV
  orchestration, repair metadata, metrics, and dashboard RPC handlers.
- `__main__.py` — runtime entry point; `plugin.run()` must remain the final executable line.
- `manifest.json` — v0.2.0 identity, exact two capabilities, and one root command with five
  subcommands.
- `dashboard_manifest.json` — read-only onboarding, exact stat cards, and paginated recent table.
- `tests/` — unit, regression, SDK-contract, storage-integrity, manifest, dashboard, security, and
  packaging tests.
- `tools/` — manifest-derived deterministic builder, archive inspector, and 15-gate release audit.
- `.github/workflows/ci.yml` — lint, security/dependency, 90% branch-coverage, SDK, audit, and
  artifact gates.
- `brand/` — brand guide, source SVG, and 512 px PNG export; not included in the runtime ZIP.
- `dist/decisionbook-0.2.0.zip` — manifest-derived marketplace artifact after a successful build.

## Required checks

Use Python 3 with `yourbot-sdk>=0.8.3,<0.9`, pytest, pytest-cov, Ruff, Basedpyright, Bandit, and
pip-audit. Install them with `requirements-dev.txt`, then run the local equivalent of CI after
meaningful changes:

```bash
python3 -m ruff check .
python3 -m basedpyright
python3 -m bandit -q -r core.py decisionbook.py __main__.py tools -ll
python3 -m pip_audit -r requirements.txt
python3 -m pytest tests -q -p no:cacheprovider \
  --cov=core --cov=decisionbook --cov-branch --cov-report=term-missing --cov-fail-under=90
yourbot validate --path .
yourbot doctor --path .
python3 tools/run_audit.py
```

If the active shell lacks the dependencies, use the known-good SDK/test environment on this
machine for the core release checks:

```bash
/home/kace/Code/mmo-maid-plugin-trivium-venv/bin/python -m pytest tests -q -p no:cacheprovider
/home/kace/Code/mmo-maid-plugin-trivium-venv/bin/yourbot validate --path .
/home/kace/Code/mmo-maid-plugin-trivium-venv/bin/yourbot doctor --path .
/home/kace/Code/mmo-maid-plugin-trivium-venv/bin/python tools/run_audit.py
```

Do not report completion with failing tests, coverage below 90%, lint/security/dependency failures,
ignored validator errors, unresolved actionable doctor warnings, or a failed audit gate. Add a
regression test for every fixed defect.

## Packaging

Build and validate the manifest-derived artifact with:

```bash
python3 tools/build_bundle.py
python3 tools/validate_bundle.py
```

The archive must remain deterministic, contain only the explicit runtime allowlist, and place
`manifest.json` plus `__main__.py` at the ZIP root. Never ship tests, tools, caches, virtual
environments, git metadata, CI files, or brand source assets. Tests must build into temporary output
paths and must not overwrite the release artifact.

After any bundled source or documentation change, rebuild and record the new SHA-256 only after the
complete pipeline passes. The artifact filename must continue to come from `manifest.json`; do not
introduce a separately hardcoded release version in builder or validator logic.

## Working style

- Inspect the installed SDK rather than relying on outdated public v0.5.1 documentation.
- This project targets `yourbot-sdk>=0.8.3,<0.9`.
- Keep changes narrow, testable, and compatible with the sandbox limits.
- Preserve user-authored changes and do not modify `~/.claude`.
- The manifest `author` and hosted HTTPS `icon_url` remain human identity/release inputs unless the
  user supplies them; never invent either value.
