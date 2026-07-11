# DecisionBook

**Decisions remembered. Context preserved.**

DecisionBook turns important Discord decisions into durable, searchable records. It preserves what
was chosen, why it was chosen, who recorded it, and how it eventually turned out—without an
external service or elevated Discord permissions.

## Use DecisionBook

DecisionBook exposes one `/decision` command with five focused subcommands:

| Command | Experience |
| --- | --- |
| `/decision add` | Opens a modal for the title, decision, reasoning, and optional tags. |
| `/decision view id:<number>` | Shows the complete immutable record and a close button when it is open. |
| `/decision list` | Searches or filters recent records and provides view and Previous/Next buttons. |
| `/decision close id:<number>` | Verifies authorship, then opens an outcome modal. |
| `/decision help` | Shows a private quick-start guide. |

List results are newest-first and include explicit status text, a non-pinging Discord identity, a
viewer-localized timestamp, and “showing X–Y of Z” page context. Button-backed list state expires
after 15 minutes; run `/decision list` again to refresh an expired view.

The read-only dashboard provides onboarding, exact open/closed/total counts for the supported
ledger range, and a paginated recent-decisions table with human-readable status and UTC timestamps.

## Privacy and permissions

DecisionBook requests exactly two Safe-tier capabilities:

- `interaction:respond` — display modals and answer commands, components, and modal submissions.
- `storage:kv` — store server-scoped decisions, repair metadata, and short-lived list state.

The close path also uses `ctx.ephemeral.dedup` for a 15-second, per-decision race guard. YourBot does
not require a capability declaration for this ephemeral service, and DecisionBook does not treat it
as durable storage.

DecisionBook does not read messages, access external APIs, use SQL, schedule background work, or
request moderation permissions. Stored data consists of decision text, normalized tags,
author/closer Discord user IDs, status, UTC timestamps, integrity metadata, and temporary pagination
state. YourBot isolates KV by plugin and Discord server.

## Integrity and product limits

- Original decision fields cannot be edited or deleted; closure appends an outcome.
- Only the original author can close a decision in v0.2.0.
- The active storage schema is checked strictly. An incompatible marker blocks reads and writes
  until a deliberate migration or repair is performed.
- ID allocation reconciles stale metadata, increments atomically, and probes for key collisions
  before writing. An existing decision is never intentionally replaced during creation.
- Recent retrieval generates padded keys newest-first and reads them through `get_many` batches of
  at most 50. It does not depend on `list_values` ordering or its 100-value limit.
- Command search considers up to the newest 500 valid records and returns at most ten per page. It
  discloses when the ledger is larger than the searched window.
- Count metadata is marked dirty around mutations and rebuilt when missing, inconsistent, or stale.
- Defensive Unicode handling preserves international text, bounded multiline reasoning, and joined
  emoji while removing dangerous formatting controls. Display output escapes Discord Markdown and
  suppresses every mention.
- YourBot KV has no compare-and-swap record primitive. The ephemeral close guard substantially
  narrows simultaneous first-close races, but eviction or service failure can bypass it; closure is
  therefore best-effort rather than transactional.
- The dashboard is read-only. DecisionBook intentionally does not offer edits, deletion, voting,
  exports, reminders, external integrations, or AI summarization.

## Development

Use Python 3 with `yourbot-sdk>=0.8.3,<0.9`:

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests -q -p no:cacheprovider \
  --cov=core --cov=decisionbook --cov-branch --cov-report=term-missing --cov-fail-under=90
python3 -m ruff check .
python3 -m basedpyright
python3 -m bandit -q -r core.py decisionbook.py __main__.py tools -ll
python3 -m pip_audit -r requirements.txt
yourbot validate --path .
yourbot doctor --path .
python3 tools/run_audit.py
```

If the active environment does not contain the SDK and tests, this machine has a known fallback:

```bash
/home/kace/Code/mmo-maid-plugin-trivium-venv/bin/python -m pytest tests -q -p no:cacheprovider
/home/kace/Code/mmo-maid-plugin-trivium-venv/bin/yourbot validate --path .
/home/kace/Code/mmo-maid-plugin-trivium-venv/bin/yourbot doctor --path .
/home/kace/Code/mmo-maid-plugin-trivium-venv/bin/python tools/run_audit.py
```

CI repeats linting, runtime type checking, security and dependency audits, branch coverage with a
90% floor, SDK validation, the fail-closed release audit, and marketplace bundle creation.
Dependabot checks Python and GitHub Actions dependencies weekly.

## Build the marketplace artifact

```bash
python3 tools/build_bundle.py
python3 tools/validate_bundle.py
```

The builder derives `dist/decisionbook-0.2.0.zip` from the ID and version in `manifest.json`. The ZIP
is deterministic, contains only the explicit runtime allowlist, and places `manifest.json` plus
`__main__.py` at its root.

The marketplace `author` and hosted HTTPS `icon_url` remain human-owned release inputs. A
marketplace-ready 512 px PNG is available at `brand/decisionbook-icon-512.png`; host it over HTTPS
before adding its URL. The project does not invent the developer identity.

## Marketplace copy

> Record what was decided, why it was chosen, and how it turned out.

Discord conversations move quickly. DecisionBook keeps a lightweight, server-scoped ledger of the
choices that matter, without asking for message access, external integrations, or elevated server
permissions.
