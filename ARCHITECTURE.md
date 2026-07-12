# DecisionBook architecture

This document describes the v0.3.0 runtime and storage contracts. Historical v0.2 behavior belongs
in `CHANGELOG.md`; it must not be treated as the current implementation.

## Runtime flow

1. A single `@plugin.on_slash_command("decision")` handler validates and dispatches the `add`,
   `view`, `list`, `close`, or `help` subcommand.
2. `/decision add` opens an immutable-record modal. `/decision close` must call `send_modal()` as
   its first interaction response, so it creates a random verifier, opens an outcome-only modal,
   then best-effort persists the verifier under a deterministic actor/channel KV key. If that state
   cannot be persisted, the already-open modal fails closed when it is submitted.
3. Add and close modal submissions, view/list commands, view buttons, and pagination buttons call
   `defer()` before durable reads or writes, then deliver through mention-suppressed follow-ups.
   Pagination follow-ups are private and do not edit the original list message. Help and the two
   modal-opening commands answer immediately without preceding `defer()`.
4. `core.py` normalizes and validates all user-controlled and persisted values without SDK state.
   Every schema-2 record includes its originating Discord `channel_id`.
5. `decisionbook.py` verifies the storage schema, allocates or loads the canonical padded key, and
   commits through server-scoped `ctx.kv`. Command reads, searches, closes, view buttons, and list
   controls require the event channel to match the stored channel.
6. A primary mutation is committed before confirmation delivery. If delivery fails, logging and a
   best-effort follow-up say the record was saved instead of incorrectly denying the commit.
7. Every interaction response containing rendered data suppresses mentions. Dashboard handlers
   reuse the same parser and storage orchestration through read-only RPCs.

`__main__.py` is intentionally tiny so importing handlers in tests never starts the SDK loop;
`plugin.run()` remains its final executable line.

## Durable KV model

Primary data:

- `meta:schema_version` — strict active schema marker; currently integer `2`.
- `meta:next_id` — monotonic high-water counter used by atomic ID allocation.
- `decision:000000000001` — one schema-versioned, channel-bound immutable decision record.

Revision-checked, repairable aggregate metadata:

- `meta:ledger_revision` — incremented after every successful primary add or close write.
- `meta:open_count`
- `meta:closed_count`
- `meta:counted_decision_keys` — number of decision keys represented by the aggregate snapshot.
- `meta:malformed_count` — decision-prefixed keys or payloads excluded from validated totals.
- `meta:counted_revision` — ledger revision represented by the aggregate snapshot.
- `meta:counts_state` — `dirty` while counts may be stale, otherwise `ready`.
- `meta:mutation:<random>` — five-minute write marker; normally deleted after count publication.

Temporary interaction state:

- `ui:list:<actor_id>:<channel_id>` — token, normalized query/status, page size, and descending
  snapshot of matched decision IDs; one replaceable key per actor/channel with a 15-minute TTL.
- `ui:close:<actor_id>:<channel_id>` — actor, channel, decision ID, and random modal verifier; one
  replaceable key per actor/channel with a five-minute TTL.

The original record stores its ID, title, choice, reasoning, canonical tags, status, author
snowflake, channel snowflake, RFC 3339 creation time, and an optional closure. Same-schema unknown
fields survive a closure for forward compatibility. Every load validates the complete record and
confirms that its payload ID matches the ID encoded by its KV key.

## Schema and allocation integrity

An absent schema marker is initialized to schema 2 only in a namespace with no decision keys.
Markerless decision data, or a marker of another type/version, raises a storage-state error and
blocks ledger operations; DecisionBook never relabels unknown data or writes schema-2 records
beneath an incompatible marker.

Schema 1 did not record a channel identity, so the runtime cannot safely infer a schema-2 channel.
An existing schema-1 ledger therefore needs an explicit, reviewed migration that assigns every
record to a channel. v0.3.0 deliberately does not auto-migrate or silently reset that data.

Before allocation, DecisionBook compares the stored counter with the decision-key count and the
highest canonical padded key. When a capped `list()` cannot prove completeness, a radix inventory
recursively partitions `decision:` by decimal prefix, uses exact prefix counts, and lists only
branches of at most 1,000 keys. It covers every decision-prefixed key within the 10,000-key platform
quota regardless of gaps, and verifies cardinality again before returning. Concurrent changes or
internally inconsistent counts fail closed.

Counter repair only advances with atomic increment; it never resets the counter to an absolute
value. Allocation then increments atomically and collision-checks each proposed record key, with a
bounded 1,000-probe fail-safe instead of overwriting an existing decision.

## Mutation and aggregate-count protocol

The primary record and aggregate metadata are deliberately separate because YourBot KV does not
provide a multi-key transaction.

1. Establish or repair an exact count baseline. A bounded retry tolerates another valid mutation
   that is finishing its metadata publication.
2. Create a unique five-minute mutation marker and mark counts dirty.
3. Write the new or closed primary record.
4. Atomically advance `meta:ledger_revision`.
5. Apply the open/closed count delta, capture decision-key count and ledger revision, and release
   the writer's marker.
6. Publish `ready` only after proving that no marker is active and that the revision and key count
   are unchanged before and after publication.

Any secondary-metadata failure leaves the primary record authoritative and keeps counts dirty.
Deferred `/decision list` provides a non-mutating repair path; add/close repair before mutating.
Dashboard reads deliberately fail fast instead of rebuilding inside their shorter deadline.
Rebuilds capture a revision/key-count baseline, inventory the full decision-key namespace, and
stream canonical records in batches of at most 50. They retain
only a batch of parsed records plus at most 10,000 numeric IDs, count malformed keys/payloads
separately, and repeat the stability proof around metadata and `ready` publication. An overlapping
write therefore cannot make a stale open/closed split appear current.

## Reverse reads, search, and pagination

Recent retrieval does not depend on the ordering or completeness of one capped `list()` call. It
starts from the safe high-water ID, generates canonical keys in descending order, and calls
`ctx.kv.get_many` in batches of at most 50. Missing keys are skipped, malformed or key-mismatched
payloads are rejected, and one aggregate warning is logged for rejected records.

Interactive text search loads at most the newest 500 valid records from the current channel and
scans at most 20,000 ID candidates. If the scan cannot reach the beginning of the ledger, both a
limited result and a no-match response explicitly say that older records were not all searched.
An exact numeric query, with or without `#`, directly loads that ID so a known older record remains
reachable outside the recent window.

One-page results create no pagination KV state. Multi-page results store the complete descending
match-ID snapshot under the requesting actor and channel for 15 minutes. Buttons validate the
token, actor, channel, page, and stored IDs, then load only the requested page of at most ten
records. Each button is acknowledged first, then returns a new private follow-up; the original list
message is not edited. Later closures cannot reorder the snapshot. Starting a new multi-page list in
the same actor/channel replaces the prior controls instead of accumulating keys. If transient state
cannot be saved, the first page remains usable and clearly says pagination is unavailable.

The 20,000-candidate ceiling applies only to interactive recent text search, where it bounds latency
and KV calls in the 64 MB / 0.25 vCPU sandbox. It does not limit allocation recovery, count repair,
ledger health, or dashboard pagination; those use the complete radix inventory across the 10,000-key
quota. Exact-ID command lookup also bypasses the recent-search window.

## Capacity reserves

The platform permits 10,000 KV keys per server/plugin namespace. New records and list snapshots are
admitted only while 64 slots remain after their planned writes, preserving room for mutation and
repair metadata. A new deterministic close-verification key is admitted only while 32 operational
slots remain. Closure reuses an existing primary key but still requires marker capacity. These
reserves turn a full-namespace edge case into actionable, fail-closed guidance instead of allowing
temporary UI state to starve ledger recovery.

## Immutability and closure concurrency

Title, choice, reasoning, tags, author, channel, and creation time never change. Closure validates
the original author, preserves the normalized record, and appends status plus an outcome,
`closed_by`, and `closed_at`. There is no edit or delete command. A repeated close reports the
authoritative first closure without replacing its outcome.

The close modal exposes only an editable outcome. Its field identifier carries the candidate
decision ID and random verifier; neither is trusted. Submission must match the five-minute
`ui:close:<actor_id>:<channel_id>` object exactly before the state is consumed. Only then does the
runtime load the record and validate channel, open status, and original author. A forged, expired,
replaced, cross-user, or cross-channel submission cannot redirect the closure and must reopen the
record.

YourBot KV exposes atomic counters but no compare-and-swap record transaction. DecisionBook uses a
15-second `ctx.ephemeral.dedup("decisionbook:close:<id>")` guard so ordinarily only one concurrent
first-close request proceeds. If the guard is unavailable, the handler now refuses the closure and
asks the user to retry. The guard is still evictable and non-durable, so it narrows the race but
does not justify claiming transactional compare-and-swap semantics.

## Dashboard trust boundary and performance

`dashboard_manifest.json` grants the overview page to `manager`. Unlike Discord commands, the
dashboard is intentionally server-wide: a manager can see decisions from every channel. This trust
boundary is called out in the ledger-health widget and must remain explicit in customer copy.

The dashboard exposes onboarding, ledger health, total/open/closed stat cards, and a responsive
recent-decisions table. It reads only proven-ready counts: health reports validated totals and KV
capacity, while dirty/missing metadata fails fast with instructions to run the deferred
`/decision list` command and refresh. This keeps full-inventory repair outside the dashboard's
ten-second RPC budget.
No heavy `on_ready` hook runs synchronously ahead of the first tenant interaction.

The recent table supports page/page-size or offset/limit conventions, defaults to 25 rows, and caps
each response at 50 rows. A contiguous healthy ledger loads only the requested IDs, including deep
pages. A sparse ledger inventories all decision keys; if invalid payloads affect pagination, it
streams batches and retains only the requested validated page. Rows expose ID, status, a compact
title/choice/tag summary, recorded time, and closure time.

## Capability boundary

The manifest declares exactly `interaction:respond` and `storage:kv`. Runtime source is constrained
to interaction, KV, no-capability ephemeral dedup, logging, and metrics surfaces. It does not use
HTTP, WebSockets, SQL, message content, schedules, Discord REST, filesystem persistence,
subprocesses, dynamic imports, or elevated Discord permissions.

## Release artifact and verification

v0.3.0 is the first supported Marketplace/public release. Historical v0.2.0 was a development
snapshot, not a supported upgrade source. Because schema 1 lacks channel identity, a schema-1 ledger
cannot be directly upgraded by this runtime and requires a separately reviewed migration.

The canonical Marketplace bundle is `dist/decisionbook-0.3.0.zip`. Its nine-file allowlist is
derived from `tools/build_bundle.py`; the builder fixes timestamps, ordering, compression, and
permissions. `tools/validate_bundle.py` checks safe paths, entry types, archive limits, exact source
parity, and deterministic metadata without extracting or executing the ZIP.

Run the local release pipeline from the repository root:

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
sha256sum dist/decisionbook-0.3.0.zip
```

The 15-gate audit compiles the tree, runs tests, validates manifest/capability/source/UI invariants,
checks the supported YourBot SDK, runs validate and doctor, builds twice, validates exact bundle
parity, and validates the staged bundle. CI repeats lint, full-tree type, security, dependency,
branch-coverage, SDK, and audit checks. CodeQL separately analyzes Python and GitHub Actions.
