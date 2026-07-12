# Changelog

## 0.3.0 — 2026-07-11

First supported Marketplace/public release. Historical v0.2 development data uses schema 1 and is
not a direct upgrade source because it has no originating channel identity.

### Integrity and privacy

- Replaced absolute ID-counter repair with monotonic atomic recovery so concurrent first writes or
  stale metadata cannot allocate the same decision ID or overwrite an acknowledged record.
- Added revision-checked aggregate repair so an overlapping add or close cannot publish a stale
  open/closed split as ready metadata.
- Added an exact radix inventory that covers the full 10,000-key KV quota independent of ID gaps,
  detects concurrent inventory changes, and enables safe monotonic high-water recovery.
- Streamed full-inventory count rebuilds and sparse dashboard validation in 50-record batches,
  tracking malformed keys/payloads separately while retaining only bounded working data.
- Advanced storage to schema 2 and bound every record to its originating Discord channel. Command
  reads, searches, closes, buttons, and pagination now enforce that channel boundary; the
  manager-only dashboard remains the server-wide administrative view.
- Made close setup modal-first to honor the interaction deadline, then bound its random verifier to
  the requesting user, channel, and decision in deterministic five-minute server state. Forged,
  replayed, expired, or replaced submissions now fail closed.
- Preserved 64 KV slots around new records/list snapshots and 32 operational slots around new
  close-verification state so temporary UI keys cannot starve mutation recovery.

### Customer experience and performance

- Deferred modal submissions, view/list commands, view buttons, and pagination buttons before
  durable RPCs, then delivered mention-suppressed follow-ups within the YourBot interaction
  contract. Pagination buttons now return private follow-up pages instead of editing the original
  list message.
- Removed heavy first-event readiness work. Deferred `/decision list` now provides non-mutating
  count repair, while dashboard RPCs use ready metadata or fail fast within their ten-second budget.
- Replaced live-recomputed list pages with owner/channel-bound snapshots of matched decision IDs,
  keeping pages stable after later closures and bounding component reads.
- Removed the page-100 dead end, added exact-ID search outside the recent window, and made every
  incomplete bounded search explicit instead of returning a false no-match claim.
- Removed the dashboard's page-400 clamp, added ledger-health and malformed-record guidance,
  surfaced closure time, and reduced the recent table to a denser five-column manager view. Full
  dashboard pagination is no longer constrained by the interactive 20,000-ID search ceiling.
- Avoided pagination KV state for one-page results, reused bounded per-user state, and added
  actionable storage-quota and repair guidance.
- Clarified immutability and tag limits in the modal/help experience.

### Release engineering

- Added the public author and version-pinned HTTPS icon metadata required for Marketplace
  presentation.
- Updated development dependency floors and GitHub Actions, removed duplicate CI runs, added
  concurrency cancellation, restricted release artifacts to main/tags, and added CodeQL plus
  development-dependency auditing.
- Grouped Dependabot development updates, removed machine-specific public setup instructions, and
  added portable contribution and private vulnerability-reporting paths.
- Added adversarial regression coverage for counter repair, concurrent count rebuilds, channel
  isolation, modal tampering, bounded search, pagination stability, quotas, and SDK deadlines.

## 0.2.0 — 2026-07-11 (historical development snapshot)

This version was not a supported Marketplace/public release. Its behavior is retained here only as
development history; schema-1 ledgers require an explicit reviewed migration before v0.3 can use
them.

### Customer experience

- Consolidated five top-level commands into `/decision` with `add`, `view`, `list`, `close`, and
  `help` subcommands.
- Replaced long create and close forms with focused add/outcome modals.
- Added close buttons to open-record views, per-result view buttons, and in-place Previous/Next
  list pagination.
- Added distinct empty-ledger, no-match, missing-record, and integrity-failure guidance.
- Added explicit status labels, non-pinging Discord identity rendering, localized Discord
  timestamps, page totals, and a dashboard quick-start panel.

### Integrity and resilience

- Made schema compatibility fail closed and added collision-safe monotonic ID allocation that
  repairs stale high-water metadata without overwriting existing records.
- Replaced capped ascending `list_values` retrieval with generated reverse keys and `get_many`
  batches of at most 50, keeping records above the previous boundary visible.
- Added repairable open/closed/count metadata so dashboard totals remain exact across supported
  ledgers and secondary-metadata failures never negate a primary decision commit.
- Separated durable commit results from confirmation delivery so a failed response never claims
  that successfully stored data was not saved.
- Added strict decision/snowflake/timestamp parsing, KV key-to-payload ID checks, closure chronology
  validation, and defensive malformed-record handling.
- Added a 15-second `ctx.ephemeral.dedup` close-race guard without expanding the declared capability
  set. It is explicitly best-effort because YourBot KV has no compare-and-swap primitive.

### Display and input safety

- Preserved bounded multiline decision prose, international tags, and valid joined emoji.
- Escaped user-controlled Discord Markdown, removed unsafe control/bidi/zero-width characters,
  neutralized broadcast mentions, and retained mention suppression on every interaction response.
- Added aggregate integrity logging and human-readable dashboard status/time presentation.

### Release engineering

- Derived the deterministic artifact name from `manifest.json` and hardened ZIP validation against
  stale content, unsafe paths, duplicates, symlinks, executables, expansion limits, and development
  artifacts.
- Replaced optimization-sensitive audit assertions with explicit fail-closed gates and added public
  SDK validation/doctor checks, staged bundle validation, and capability/UI parity checks.
- Added CI lint, runtime type, security, dependency, branch-coverage (90% floor), SDK, audit, and
  artifact gates.
- Expanded unit, regression, storage-integrity, dashboard, SDK-contract, security, and packaging
  coverage around the v0.2.0 behavior.

## 0.1.0 — 2026-07-11

- Added immutable decision recording, viewing, searching, filtering, and author-owned closure.
- Added five slash commands and a read-only overview dashboard.
- Added Unicode, mention, schema, and bounded-scan defenses.
- Limited permissions to `interaction:respond` and `storage:kv`.
- Added regression, security, manifest, dashboard, audit, and deterministic packaging checks.
