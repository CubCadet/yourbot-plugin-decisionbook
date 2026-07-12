# DecisionBook project context

Updated: 2026-07-12

## Why this exists

This file is the durable handoff for the chat that designed, built, and hardened DecisionBook. A new
Codex session should read it before changing the project instead of reconstructing intent from code
alone. `ARCHITECTURE.md` is the normative technical companion for the current release.

## Brand and product

- Name: **DecisionBook**
- Plugin ID: `decisionbook`
- Version: `0.3.1`
- Author: **CubCadet**
- Tagline: **Decisions remembered. Context preserved.**
- Marketplace line: **Record what was decided, why it was chosen, and how it turned out.**
- Public icon: the v0.3.0-pinned HTTPS URL in `manifest.json`
- Brand palette, interface guidance, source SVG, and 512 px PNG export: `brand/`

DecisionBook fills the gap between disposable Discord chat and heavyweight project-management
software. It is intentionally an immutable decision ledger—not a poll bot, ticket system,
moderation suite, or AI summarizer.

## v0.3.x customer experience

The manifest declares one `/decision` root command with five type-1 subcommands:

- `/decision add` — opens a four-field modal for title, decision, required reasoning, and optional
  comma-separated tags. Copy states the immutable-record contract and the limit of five tags with
  24 characters per tag.
- `/decision view id:<number>` — shows the complete record only in the channel where it was added.
  An open record includes a close button.
- `/decision list` — optionally searches title/choice/reason/tags/ID, filters all/open/closed, and
  accepts one to ten results per page. Search and every resulting control are channel-scoped.
- `/decision close id:<number>` — creates a random verifier, opens an outcome-only modal as the
  interaction's required first response, then attempts to persist actor/channel/target verification
  state. Submission must match that state before the record, channel, and original author are
  validated.
- `/decision help` — returns an ephemeral, actionable quick-start guide.

Add and close modal submissions, `/decision view`, `/decision list`, view buttons, and pagination
buttons defer before durable RPCs and deliver follow-ups. Pagination buttons return a new private
page rather than editing the original list message. This preserves the YourBot/Discord
first-response deadline for every command/control flow that may perform storage work.

Interaction embeds use explicit status text, non-pinging Discord user mentions, and viewer-localized
timestamps. Lists distinguish an empty channel ledger, a complete no-match, an incomplete bounded
search, and unavailable malformed data. Exact numeric queries bypass the recent-window search and
load the requested ID directly.

Multi-page lists snapshot up to 500 matching IDs for 15 minutes. State is bound to its actor and
channel, later record closures do not reorder the result, and buttons load only their requested page.
A new list replaces old controls for that actor/channel. One-page lists consume no state key; when
quota or transient KV failure prevents state creation, the first page remains usable without
pagination.

## Privacy and administrative boundary

Every schema-2 decision stores the Discord channel where it originated. Command views, text/ID
searches, closes, view buttons, and pagination all require that same event channel. A request from
another channel behaves as not found and cannot mutate the record.

This is channel isolation, not a claim that Discord messages themselves are secret: users who can
read the channel can see non-ephemeral DecisionBook output posted there.

The dashboard is a separate, explicit administrative boundary. Its page requires `manager` and is
server-wide, showing records and totals across all channels. Customer-facing health copy states this
distinction. Do not change either side of the boundary without a product and privacy review.

## Durable data and retrieval model

Primary keys:

- `meta:schema_version` = integer `2`
- `meta:next_id` = monotonic high-water counter
- `decision:000000000001` = one channel-bound, schema-2 decision object

Revision-checked aggregate metadata:

- `meta:ledger_revision`
- `meta:open_count`
- `meta:closed_count`
- `meta:counted_decision_keys`
- `meta:malformed_count`
- `meta:counted_revision`
- `meta:counts_state` = `dirty` or `ready`
- `meta:mutation:<random>` = unique in-progress writer marker, five-minute fail-safe TTL

Temporary interaction state:

- `ui:list:<actor_id>:<channel_id>` = token/query/status/page-size/matched-ID snapshot, 15-minute TTL
- `ui:close:<actor_id>:<channel_id>` = actor/channel/decision/token close verifier, five-minute TTL

Records store original title, choice, reasoning, canonical Unicode-aware tags, status, author ID,
channel ID, creation timestamp, and optional closure. Closure preserves unknown same-schema fields
for forward compatibility. The parser validates all fields, Discord snowflakes, RFC 3339
timestamps, closure authorship/chronology, schema version, and the payload-ID-to-KV-key relationship.

The schema marker is strict: a truly empty namespace initializes schema 2, while decision keys with
no marker fail closed rather than being silently relabeled. Incompatible values also fail closed.
Schema 1 has no channel identity, so v0.3.x does not guess one, silently reset the ledger, or
auto-migrate. Any existing schema-1 deployment requires an explicit reviewed migration that assigns
each record to a channel before this runtime can use it.

ID recovery compares the counter, decision-key count, and highest canonical key. A radix inventory
recursively partitions decimal key prefixes until every exact-count branch fits the 1,000-key
`list()` cap, covering the full 10,000-key quota regardless of gaps. It verifies cardinality before
returning and fails closed if concurrent changes or inconsistent counts prevent proof. A stale or
absent counter is repaired only with monotonic atomic increment. Allocation then increments
atomically and collision-checks up to 1,000 candidate keys; creation never intentionally replaces
an existing record.

Recent loading generates padded keys newest-first and calls `ctx.kv.get_many` in batches of no more
than 50. Interactive text search returns at most 500 channel records and scans at most 20,000
candidate IDs. That ceiling applies only to recent text search; exact-ID lookup, allocation
recovery, full count repair, ledger health, and dashboard pagination cover all decision keys within
the 10,000-key quota. Count repair streams record batches without retaining the full payload set.
Sparse dashboard pagination likewise streams validation when needed and retains only the requested
page of at most 50 records.

New-record and list-state admission preserve 64 KV slots after planned writes for mutation and
repair activity. A new close-verification key preserves a separate 32-slot operational reserve.
These checks keep short-lived UI state from exhausting capacity required to finish or recover a
ledger mutation.

## Count consistency and committed-write semantics

Before a primary add or close, DecisionBook establishes a stable count baseline, creates a unique
mutation marker, and marks aggregates dirty. After the primary write it advances the ledger
revision, applies atomic open/closed deltas, records the represented key count and revision, removes
the marker, and publishes `ready` only after repeat stability checks.

A count rebuild also checks active markers, ledger revision, and decision-key count before and after
its scan and around `ready` publication. An overlapping mutation cannot publish stale aggregates as
current. Failed or unverifiable secondary metadata remains dirty; the primary record stays
authoritative. Deferred `/decision list` is the non-mutating repair path, and add/close also repair
before mutation. Dashboard RPCs never launch a long repair inside their ten-second budget.

Likewise, interaction delivery occurs after the commit. A failed confirmation never turns a stored
add/close into a false “nothing saved” response; the fallback directs the user to verify the record.

## Closure integrity and known platform limit

The close command deliberately avoids KV before `send_modal()`: the SDK requires the modal to be the
first interaction response, and modal-first setup minimizes deadline risk. It creates a random
verifier, sends the modal, and then best-effort stores one five-minute object at the deterministic
actor/channel key. If that write fails, the visible form will fail closed on submission rather than
closing an unverified target.

The modal exposes only the outcome as editable content. Its field identifier carries the candidate
ID and token, which are treated as untrusted and must exactly match the server-side actor, channel,
decision, and verifier. Valid state is consumed before the runtime loads the record and checks its
channel, open status, and original author. Forged, expired, replayed, or replaced values cannot
redirect an irreversible closure.

`ctx.ephemeral.dedup` provides a decision-scoped 15-second first-close guard without another
declared capability. The handler fails closed when that guard is unavailable instead of proceeding
with an unprotected write. The guard remains evictable and YourBot KV still has no compare-and-swap
record primitive, so do not claim transactional closure semantics.

## Security and capability posture

Exactly two Safe-tier capabilities are declared:

- `interaction:respond`
- `storage:kv`

The project does not add HTTP, SQL, message-content listeners, schedules, Discord REST actions,
proxy domains, secrets, or elevated permissions. Runtime access is limited to interaction, KV,
no-capability ephemeral dedup, logging, and metrics.

The implementation provides Unicode NFKC normalization, dangerous control/bidi/zero-width removal,
valid joined-emoji preservation, bounded multiline prose, international canonical tags,
broadcast-mention neutralization, Discord Markdown escaping, mention suppression, strict lengths
and types, aggregate malformed-record logging, immutable originals, author-only closure,
channel-scoped command access, and source audits against forbidden runtime surfaces.

## Dashboard experience

The read-only manager dashboard contains:

- onboarding guidance;
- a ledger-health alert with validated-decision count, repair guidance, and remaining KV capacity;
- exact total/open/closed stat cards for a proven stable snapshot; and
- a five-column recent table with ID, status, title/choice/tag summary, recorded time, and close time.

Dashboard handlers consume only proven-ready count metadata and fail fast with repair guidance when
it is dirty or missing. A manager can run deferred `/decision list` in Discord and refresh; this
keeps cold full-inventory repair outside the dashboard's ten-second RPC deadline. No heavy
`on_ready` hook runs ahead of a tenant's first interaction.

The table supports page/page-size and offset/limit conventions, defaults to 25 rows, caps responses
at 50, and returns the validated server-wide total. Healthy contiguous ledgers load only the
requested page; sparse layouts use the full radix inventory and stream invalid-payload filtering
when necessary. The health warning changes from info to warn when fewer than 500 of the platform's
10,000 KV key slots remain.

## Release engineering

v0.3.1 is the current deployment-compatibility patch; v0.3.0 remains the first supported
Marketplace/public release. The patch mirrors the exact two Safe-tier capabilities across the
canonical `capabilities_required` field and the SDK 0.8.3 transition field
`capabilities_requested`. This is compatibility metadata, not an expanded permission set. The
v0.2.0 material below is a historical development snapshot only. Schema 1 lacks channel identity
and is not a direct upgrade source for the schema-2 runtime; migration must be explicit and
reviewed.

`tools/build_bundle.py` validates manifest identity and derives
`dist/decisionbook-0.3.1.zip`. The nine-file bundle uses a fixed allowlist, timestamp, compression,
permissions, and ordering. `tools/validate_bundle.py` rejects unsafe paths, duplicates, links,
executables, expansion-limit violations, stale entries, and source/allowlist mismatches without
extracting or executing the archive.

`tools/run_audit.py` has 15 fail-closed gates: compilation, tests, manifest, capabilities,
forbidden imports, dangerous built-ins, mention suppression, entrypoint, dashboard parity, SDK
version, `yourbot validate`, `yourbot doctor`, deterministic build, bundle validation, and staged
validation. It uses explicit exceptions rather than optimization-sensitive assertions.

GitHub Actions additionally runs Ruff, full-tree Basedpyright, Bandit at medium/high severity,
runtime and development dependency audits, branch coverage with a 90% floor, SDK checks, the audit,
and trusted-branch/tag artifact upload. A separate pinned CodeQL workflow analyzes Python and GitHub
Actions. Dependabot groups weekly Python and GitHub Actions updates.

Reproduce the v0.3.1 release checks with:

```bash
python3 -m pip install -r requirements-dev.txt
ruff check .
basedpyright
bandit -q -r core.py decisionbook.py __main__.py tools -ll
pip-audit --strict -r requirements.txt
pip-audit --strict -r requirements-dev.txt
python3 -m pytest tests -q -p no:cacheprovider \
  --cov=core --cov=decisionbook --cov-branch --cov-report=term-missing --cov-fail-under=90
yourbot validate --path .
yourbot doctor --path .
python3 tools/run_audit.py
python3 tools/validate_bundle.py
sha256sum dist/decisionbook-0.3.1.zip
```

## v0.3.1 verification snapshot

The final compatibility-patch pipeline recorded:

- 276 passing tests
- 90.41% branch coverage, above the enforced 90% floor
- clean Ruff formatting/lint, full-tree Basedpyright, Bandit medium/high, codespell, and Vulture
- no known runtime or development dependency vulnerabilities
- clean `yourbot validate` and all-green `yourbot doctor`
- 15/15 explicit audit gates passed, including staged bundle validation
- source and bundled manifests with identical `capabilities_required` and
  `capabilities_requested` arrays
- deterministic source-identical ZIP built twice with the same digest
- artifact: `dist/decisionbook-0.3.1.zip`
- ZIP size: 29,897 bytes, 9 runtime files
- SHA-256: `cdfa00a71b259a47e4901af0d000fa46698fccaa5ec9973879582a6d40afd59b`

Any later bundled source, `README.md`, or `CHANGELOG.md` change legitimately changes the digest and
requires a rebuild and updated snapshot.

## Historical v0.3.0 verification snapshot

The final integrated pipeline recorded:

- 275 passing tests
- 90.57% branch coverage, above the enforced 90% floor
- clean Ruff formatting/lint, full-tree Basedpyright, Bandit medium/high, codespell, and Vulture
- no known runtime or development dependency vulnerabilities
- clean `yourbot validate` and all-green `yourbot doctor`
- 15/15 explicit audit gates passed, including staged bundle validation
- deterministic source-identical ZIP built twice with the same digest
- artifact: `dist/decisionbook-0.3.0.zip`
- ZIP size: 29,483 bytes, 9 runtime files
- SHA-256: `d2bbb2f64d99c8dd906c776900e2a6f915be9457c0935132a56d46bd97766c02`

## Historical v0.2.0 development verification snapshot

The pre-release development snapshot used `yourbot-sdk 0.8.3` and recorded:

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

This is historical evidence only. It does not verify v0.3.x or its schema-2 behavior.

## Decisions intentionally deferred

Do not silently add these to v0.3.x:

- voting or polls
- reminders or scheduled reviews
- administrator override closure
- editing or deletion
- exports
- SQL analytics
- external integrations
- AI summarization
- message listeners
- automatic schema-1 channel assignment

Each is a separate product decision and, where relevant, requires a capability/privacy review.

## External release boundary

Building or pushing the repository does not submit DecisionBook to the YourBot Marketplace. Treat
Marketplace submission as a separate, explicit action after the final v0.3.1 artifact and public
release checks are complete.
