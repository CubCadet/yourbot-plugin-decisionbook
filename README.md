# DecisionBook

**Decisions remembered. Context preserved.**

[![CI](https://github.com/CubCadet/yourbot-plugin-decisionbook/actions/workflows/ci.yml/badge.svg)](https://github.com/CubCadet/yourbot-plugin-decisionbook/actions/workflows/ci.yml)
[![CodeQL](https://github.com/CubCadet/yourbot-plugin-decisionbook/actions/workflows/codeql.yml/badge.svg)](https://github.com/CubCadet/yourbot-plugin-decisionbook/actions/workflows/codeql.yml)

DecisionBook turns important Discord decisions into durable, searchable, channel-scoped records.
It preserves what was chosen, why it was chosen, who recorded it, and how it eventually turned
out—without an external service or elevated Discord permissions.

## Use DecisionBook

DecisionBook exposes one `/decision` command with five focused subcommands:

| Command | Experience |
| --- | --- |
| `/decision add` | Opens a modal for the title, decision, reasoning, and optional tags. |
| `/decision view id:<number>` | Shows the complete immutable record and a close button when it is open. |
| `/decision list` | Searches or filters recent records and provides view and Previous/Next buttons. |
| `/decision close id:<number>` | Opens the outcome modal; validates its target and author when submitted. |
| `/decision help` | Shows an ephemeral quick-start guide. |

List results are newest-first and include explicit status text, a non-pinging Discord identity, a
viewer-localized timestamp, and “showing X–Y of Z” page context. Multi-page list state expires
after 15 minutes; run `/decision list` again to refresh an expired view. Previous/Next buttons
acknowledge immediately and return the requested page as a new private follow-up instead of editing
the original channel message.

The read-only manager dashboard provides onboarding, exact open/closed/total counts, ledger health,
and a paginated recent-decisions table with human-readable status and UTC timestamps. It uses
proven-ready metadata and fails fast with guidance instead of attempting a long repair inside its
ten-second deadline. Running deferred `/decision list` repairs counts safely before a refresh.
Complete inventory covers the platform's full 10,000-key KV quota, including sparse IDs.

## Privacy and permissions

DecisionBook requests exactly two Safe-tier capabilities:

- `interaction:respond` — display modals and answer commands, components, and modal submissions.
- `storage:kv` — store channel-bound decisions in the server's isolated plugin namespace, repair
  metadata, and keep short-lived interaction state.

The close path also uses `ctx.ephemeral.dedup` for a 15-second, per-decision race guard. YourBot does
not require a capability declaration for this ephemeral service, and DecisionBook does not treat it
as durable storage.

DecisionBook does not read messages, access external APIs, use SQL, schedule background work, or
request moderation permissions. Stored data consists of decision text, normalized tags,
author/closer Discord user IDs, channel IDs, status, UTC timestamps, integrity metadata, and
temporary pagination and close-form verification state. YourBot isolates KV by plugin and Discord
server.

Channel scoping is an access boundary inside DecisionBook, not a promise that a Discord channel is
secret. Command output posted in a channel is visible to members who can read that channel. The
manager dashboard is intentionally server-wide and can show decisions from every channel.

## Integrity and product limits

- Original decision fields cannot be edited or deleted; closure appends an outcome.
- Only the original author can close a decision.
- Command access is limited to the channel where the decision was recorded. The manager-only
  dashboard is the intentional server-wide administrative view.
- The active storage schema is checked strictly. Markerless decision data or an incompatible marker
  blocks reads and writes until a deliberate migration or repair is performed.
- ID allocation reconciles stale metadata, increments atomically, and probes for key collisions
  before writing. An existing decision is never intentionally replaced during creation.
- Recent retrieval generates padded keys newest-first and reads them through `get_many` batches of
  at most 50. It does not depend on a single capped `list()` result or its ordering.
- Interactive text search considers up to the newest 500 valid channel records, scans at most
  20,000 candidate IDs, and returns at most ten per page. It discloses an incomplete search, while
  exact numeric lookup remains available outside that recent window. The 20,000-ID ceiling does
  not limit inventory, count repair, or dashboard pagination.
- A radix key inventory uses exact prefix counts to cover all decision keys within the 10,000-key
  quota regardless of ID gaps. Count repair and sparse dashboard pagination process records in
  batches and retain only bounded working data; malformed keys and records are counted separately
  and excluded from validated totals.
- Count metadata is marked dirty around mutations and rebuilt when missing, inconsistent, or stale.
  Revision, key-count, and active-writer checks prevent a stale split from being published ready.
- New decisions and list snapshots preserve 64 KV slots for recovery writes. Close-form state uses
  a separate 32-slot operational reserve so verification state cannot consume marker capacity.
- Defensive Unicode handling preserves international text, bounded multiline reasoning, and joined
  emoji while removing dangerous formatting controls. Display output escapes Discord Markdown and
  suppresses every mention.
- YourBot KV has no compare-and-swap record primitive. The ephemeral close guard substantially
  narrows simultaneous first-close races. If the guard service is unavailable, DecisionBook refuses
  the close and asks the user to retry; eviction can still leave a platform-level race, so closure
  remains best-effort rather than transactional.
- The dashboard is read-only. DecisionBook intentionally does not offer edits, deletion, voting,
  exports, reminders, external integrations, or AI summarization.

## Release and schema compatibility

v0.3.0 is DecisionBook's first supported Marketplace/public release. Its schema-2 records require
an originating channel. Historical v0.2 development ledgers used schema 1 and did not store that
identity, so they cannot be upgraded directly or assigned a channel safely by this runtime. Do not
install v0.3 over schema-1 data without an explicit, reviewed migration; the plugin fails closed
rather than guessing or resetting the ledger.

## Development

Use Python 3.12 with `yourbot-sdk>=0.8.3,<0.9`. Create an isolated environment so project tools do
not affect system packages:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

On Windows PowerShell, activate the environment with `.venv\Scripts\Activate.ps1`. Once the
environment is active, run the complete local checks from the repository root:

```bash
python -m pytest tests -q -p no:cacheprovider \
  --cov=core --cov=decisionbook --cov-branch --cov-report=term-missing --cov-fail-under=90
python -m ruff check .
python -m basedpyright
python -m bandit -q -r core.py decisionbook.py __main__.py tools -ll
python -m pip_audit --strict -r requirements.txt
python -m pip_audit --strict -r requirements-dev.txt
yourbot validate --path .
yourbot doctor --path .
python tools/run_audit.py
```

CI repeats linting, runtime type checking, security and runtime/development dependency audits,
branch coverage with a 90% floor, SDK validation, and the fail-closed release audit. It uploads a
marketplace bundle only for validated `main` and version-tag pushes. A separate CodeQL workflow
analyzes Python and GitHub Actions code. Dependabot groups Python and GitHub Actions updates weekly.

## Build the marketplace artifact

```bash
python3 tools/build_bundle.py
python3 tools/validate_bundle.py
```

The builder derives `dist/decisionbook-0.3.0.zip` from the ID and version in `manifest.json`. The ZIP
is deterministic, contains only the explicit runtime allowlist, and places `manifest.json` plus
`__main__.py` at its root.

The manifest publishes the verified `CubCadet` byline and a version-pinned HTTPS URL for the
512 px icon in `brand/decisionbook-icon-512.png`.

## Support and feedback

Use the repository's [issue forms](https://github.com/CubCadet/yourbot-plugin-decisionbook/issues/new/choose)
for sanitized bug reports and feature requests. Never post private Discord decision content,
credentials, or security details in an issue. Report vulnerabilities through the
[private security form](https://github.com/CubCadet/yourbot-plugin-decisionbook/security/advisories/new)
instead.

## Marketplace copy

> Record what was decided, why it was chosen, and how it turned out.

Discord conversations move quickly. DecisionBook keeps a lightweight, channel-scoped ledger of the
choices that matter, with a manager-only server overview and without asking for message access,
external integrations, or elevated server permissions.
