# Changelog

## 0.2.0 — 2026-07-11

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
