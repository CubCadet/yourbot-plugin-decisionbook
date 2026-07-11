# DecisionBook architecture

## Runtime flow

1. One `@plugin.on_slash_command("decision")` handler validates and dispatches the selected
   `add`, `view`, `list`, `close`, or `help` subcommand.
2. Add and close begin with `ctx.interaction.send_modal(...)`; dedicated modal handlers validate
   their submitted values. View, close, and pagination buttons use prefix-scoped component handlers.
3. `core.py` normalizes and validates all user-controlled and persisted values without SDK state.
4. `decisionbook.py` verifies storage schema, allocates or loads the canonical padded key, and
   commits through server-scoped `ctx.kv`.
5. A committed mutation is recorded before confirmation delivery. If delivery fails, logging and a
   best-effort error response say the record was saved instead of incorrectly denying the commit.
6. Every response containing rendered data suppresses mentions. Dashboard handlers reuse the same
   parser and storage orchestration through read-only RPCs.

`__main__.py` is intentionally tiny so importing handlers in tests never starts the SDK loop;
`plugin.run()` remains its final executable line.

## Durable KV model

Primary data:

- `meta:schema_version` — strict active schema marker; currently integer `1`.
- `meta:next_id` — repaired high-water metadata used by atomic ID allocation.
- `decision:000000000001` — one schema-versioned immutable decision record.

Repairable aggregate metadata:

- `meta:open_count`
- `meta:closed_count`
- `meta:counted_decision_keys`
- `meta:counts_state` — `dirty` while a mutation may make counts stale, otherwise `ready`.

Temporary interaction state:

- `ui:list:<token>` — normalized query, status, page size, and snapshot high-water value with a
  15-minute KV TTL.

The original record stores its ID, title, choice, reasoning, canonical tags, status, author
snowflake, RFC 3339 creation time, and an optional closure. Same-schema unknown fields survive a
closure for forward compatibility. Every load validates the complete record and confirms that its
payload ID matches the ID encoded by its KV key.

## Schema and allocation integrity

An absent schema marker is initialized to schema 1. A marker of another type or version raises a
storage-state error and blocks ledger operations; DecisionBook never writes schema-1 data beneath
an incompatible marker.

Before allocation, DecisionBook reconciles the stored counter with the decision-key count and the
highest observed canonical padded key. It repairs the counter, increments it atomically, and checks
that the proposed decision key does not exist. Collisions advance to another ID, with a bounded
fail-safe rather than overwriting an existing decision.

The primary record write and its count metadata are deliberately distinct. Counts are marked dirty
before mutation. After a successful write, metadata is incremented or rebuilt; failures leave the
primary record authoritative and cause the next count read to repair aggregates.

## Reverse reads and bounded search

Recent retrieval does not call `list_values`. It starts from the repaired high-water ID, generates
canonical keys in descending order, and calls `ctx.kv.get_many` in batches of at most 50. Missing
keys are skipped, malformed or key-mismatched payloads are rejected, and one aggregate warning is
logged for rejected records.

Interactive command search loads at most the newest 500 valid records, scans at most 20,000 ID
candidates, and returns one to ten records per page. A stable high-water snapshot and 15-minute
pagination token keep button navigation coherent. When more valid decisions exist than the search
window, the result explicitly states that only the newest window was searched.

The 20,000-candidate ceiling bounds CPU and KV calls in the 64 MB / 0.25 vCPU sandbox. Extremely
sparse ledgers beyond that range require an explicit repair or a future storage design; the plugin
fails closed instead of claiming complete results.

## Immutability and closure concurrency

Title, choice, reasoning, tags, author, and creation time never change. Closure validates the
original author, preserves the full normalized record, and appends status plus an outcome,
`closed_by`, and `closed_at`. There is no edit or delete command. A repeated close reports the
authoritative first closure without replacing its outcome.

YourBot KV exposes atomic counters but no compare-and-swap record transaction. DecisionBook uses a
15-second `ctx.ephemeral.dedup("decisionbook:close:<id>")` guard so ordinarily only one concurrent
first-close request proceeds. `ctx.ephemeral` requires no additional declared capability, is
evictable and non-durable, and can be unavailable; the handler therefore fails open on guard
failure and logs it. This narrows the race but cannot guarantee transactional closure. The last
authorized write can still win in the remaining platform-level race.

## Interaction and dashboard UI

Open record embeds include a close button. List embeds include view buttons plus Previous/Next
navigation, explicit text statuses, safe Discord identities, localized relative times, and visible
range/total metadata. Add and close modals keep long prose out of slash-command option fields.

The dashboard manifest maps `dashboard.<name>` RPC methods to
`@plugin.on_dashboard("<name>")` handlers. It is read-only and contains onboarding, total/open/closed
stat cards, and a responsive recent table. Count cards use repaired exact metadata. Table requests
support page/page-size or offset/limit pagination, default to 25 rows, cap pages at 50 rows, load one
record window per page, and return the exact valid-record total for the supported ledger range.

## Capability boundary

The manifest declares exactly `interaction:respond` and `storage:kv`. Runtime source is constrained
to interaction, KV, no-capability ephemeral dedup, logging, and metrics surfaces. It does not use
HTTP, WebSockets, SQL, message content, schedules, Discord REST, filesystem persistence, subprocesses,
dynamic imports, or elevated Discord permissions.
