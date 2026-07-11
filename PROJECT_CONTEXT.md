# DecisionBook project context

Updated: 2026-07-11

## Why this exists

This file is the durable handoff for the chat that designed, built, and hardened DecisionBook. A new
Codex session should read it before changing the project instead of reconstructing intent from code
alone.

## Brand and product

- Name: **DecisionBook**
- Plugin ID: `decisionbook`
- Version: `0.2.0`
- Tagline: **Decisions remembered. Context preserved.**
- Marketplace line: **Record what was decided, why it was chosen, and how it turned out.**
- Brand palette, interface guidance, source SVG, and 512 px PNG export live under `brand/`.

DecisionBook fills the gap between disposable Discord chat and heavyweight project-management
software. It is intentionally an immutable decision ledger—not a poll bot, ticket system,
moderation suite, or AI summarizer.

## Current customer experience

The manifest declares one `/decision` root command with five type-1 subcommands:

- `/decision add` — opens a four-field modal for title, decision, required reasoning, and optional
  comma-separated tags.
- `/decision view id:<number>` — shows the complete record. An open record includes a close button.
- `/decision list` — optionally searches title/choice/reason/tags/ID, filters all/open/closed, and
  accepts a page size from one to ten. Results have per-record view buttons and Previous/Next
  navigation.
- `/decision close id:<number>` — verifies the original author and opens a prefilled outcome modal.
- `/decision help` — returns a private, actionable quick-start guide.

Interaction embeds use explicit status text, non-pinging Discord user mentions, and viewer-localized
timestamps. Lists show visible range/total context, distinguish an empty ledger from no matches, and
disclose when search is limited to the recent window. Pagination state uses a 15-minute KV TTL.

The read-only dashboard shows onboarding, exact total/open/closed stat cards for the supported
ledger range, and a paginated recent-decisions table. The table supports page/page-size and
offset/limit conventions, defaults to 25 rows, caps a request at 50 rows, and returns human-readable
status and UTC timestamps.

## Durable data and retrieval model

Primary keys:

- `meta:schema_version` = integer `1`
- `meta:next_id` = repaired high-water counter
- `decision:000000000001` = one versioned decision object

Repairable aggregate metadata:

- `meta:open_count`
- `meta:closed_count`
- `meta:counted_decision_keys`
- `meta:counts_state` = `dirty` or `ready`

Temporary interaction state:

- `ui:list:<token>` = normalized search/filter/page-size/high-water snapshot, 15-minute TTL

Records store original title, choice, reasoning, canonical Unicode-aware tags, status, author ID,
creation timestamp, and optional closure. Closure preserves unknown same-schema fields for forward
compatibility. The parser validates all fields, Discord snowflakes, RFC 3339 timestamps, closure
authorship/chronology, and the payload-ID-to-KV-key relationship.

The schema marker is strict: absent storage initializes schema 1; incompatible values fail closed.
ID allocation reconciles the counter with stored key evidence, increments atomically, checks key
existence, and performs bounded collision probes. Creation never intentionally replaces an existing
record.

Recent loading generates padded keys newest-first and calls `ctx.kv.get_many` in batches of no more
than 50; it does not depend on capped/ascending `list_values`. Interactive search returns at most ten
records per page from the newest 500 valid records and scans at most 20,000 candidate IDs. Count
metadata is marked dirty around mutations and rebuilt if absent, stale, or inconsistent. Primary
record commits remain authoritative if secondary metadata repair fails.

## Security and capability posture

Exactly two Safe-tier capabilities are declared:

- `interaction:respond`
- `storage:kv`

`ctx.ephemeral.dedup` is used once, only for a decision-scoped 15-second close guard. This YourBot
surface requires no declared capability and is not durable. The project does not add HTTP, SQL,
message-content listeners, schedules, Discord REST actions, proxy domains, secrets, or elevated
permissions.

The implementation provides Unicode NFKC normalization, dangerous control/bidi/zero-width removal,
valid joined-emoji preservation, bounded multiline prose, international canonical tags, broadcast-
mention neutralization, Discord Markdown escaping, mention suppression, strict lengths and types,
aggregate malformed-record logging, immutable originals, author-only closure, and source audits
against forbidden runtime surfaces.

Known platform constraint: KV exposes no compare-and-swap record primitive. The ephemeral guard
prevents ordinary simultaneous first-close operations, but it is evictable and the handler proceeds
if the service is unavailable. A residual last-authorized-write-wins race therefore remains. Do not
claim transactional closure semantics without a platform primitive or an explicitly reviewed
storage/capability redesign.

## Release engineering

`tools/build_bundle.py` validates the manifest identity and derives the canonical artifact name;
for this release it is `dist/decisionbook-0.2.0.zip`. It uses a fixed file allowlist, timestamp,
compression, permissions, and ordering. `tools/validate_bundle.py` verifies safe paths, archive
limits, entry types, exact allowlist/source parity, and deterministic metadata without extracting or
executing the ZIP.

The fail-closed audit has 15 explicit gates: compilation, tests, manifest, capabilities, forbidden
imports, dangerous built-ins, mention suppression, entrypoint, dashboard parity, SDK version,
`yourbot validate`, `yourbot doctor`, deterministic build, bundle validation, and staged validation.
It uses explicit exceptions rather than optimization-sensitive `assert` statements.

GitHub Actions additionally runs Ruff, a clean runtime Basedpyright check, Bandit at medium/high
severity, dependency vulnerability checking, branch coverage with a 90% floor, the public SDK
checks, the audit, and validated artifact upload. Dependabot checks Python and GitHub Actions
dependencies weekly.

## v0.2.0 verification snapshot

The completed release run used `yourbot-sdk 0.8.3` and produced:

- 236 passing tests
- 96.15% branch coverage, above the enforced 90% floor
- clean Ruff, Bandit medium/high, and dependency vulnerability checks
- clean `yourbot validate`
- all-green `yourbot doctor`
- 15/15 explicit audit gates passed
- deterministic source-identical ZIP built twice with the same digest
- artifact: `dist/decisionbook-0.2.0.zip`
- ZIP size: 21,403 bytes, 9 runtime files
- SHA-256: `3d683b1218d11abca0614aadb211139349529a5f39c5f4ea6b0e56f3d2c3a2e2`

Reproduce the release checks with:

```bash
python3 -m pytest tests -q -p no:cacheprovider \
  --cov=core --cov=decisionbook --cov-branch --cov-report=term-missing --cov-fail-under=90
yourbot validate --path .
yourbot doctor --path .
python3 tools/run_audit.py
python3 tools/validate_bundle.py
sha256sum dist/decisionbook-0.2.0.zip
```

Any later source, `README.md`, or `CHANGELOG.md` change legitimately changes the bundled digest.
Rebuild and update this snapshot only after the complete pipeline passes.

## Decisions intentionally deferred

Do not silently add these to v0.2.x:

- voting or polls
- reminders or scheduled reviews
- administrator override closure
- editing or deletion
- exports
- SQL analytics
- external integrations
- AI summarization
- message listeners

Each is a separate product decision and, where relevant, requires a capability/privacy review.

## Pending human inputs

- `manifest.json` intentionally has no `author`; use the actual YourBot developer identity rather
  than inventing one.
- `manifest.json` intentionally has no `icon_url`; host
  `brand/decisionbook-icon-512.png` over HTTPS before adding it.
- Building the ZIP does not upload or submit it to the YourBot Marketplace.
