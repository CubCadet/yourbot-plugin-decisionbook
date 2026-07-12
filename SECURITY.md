# DecisionBook security

## Supported versions

| Version | Security support |
| --- | --- |
| 0.3.x | Supported (first Marketplace/public release line) |
| 0.2.x | Unsupported historical development snapshot; no direct schema upgrade |
| Earlier versions | Unsupported |

Schema 1 did not store a channel ID. Historical v0.2 data therefore cannot be safely upgraded in
place by v0.3; use an explicit, reviewed migration that assigns every record to a channel. The v0.3
runtime fails closed instead of guessing ownership or resetting an incompatible ledger.

## Threat model

DecisionBook treats every command option, modal value, component payload, dashboard parameter, and
stored KV value as untrusted. Defenses cover mention abuse, Discord Markdown injection, Unicode
visual controls, malformed or spoofed records, stale metadata, concurrent mutation, excessive
input/work, capability creep, accidental networking, and unsafe packaging.

## Input and display controls

- Text is normalized with Unicode NFKC while retaining international letters, numbers, emoji, and
  valid emoji joiners.
- Control, bidi, directional-isolate, BOM, and unsafe zero-width formatting characters are removed.
- Titles and queries collapse to one line; decisions, reasoning, and outcomes preserve useful
  paragraph structure within strict character and 12-line limits.
- Tags are canonical, deduplicated, limited to five, and accept international letters/numbers.
- `@everyone` and `@here` are neutralized for display, user-controlled Discord Markdown is escaped,
  and every interaction response sets `allowed_mentions={"parse": []}`.
- Discord identities are canonical unsigned 64-bit snowflakes. Timestamps must be timezone-aware
  RFC 3339 values and are rendered as localized Discord tokens in interaction embeds.
- Numeric IDs reject booleans, floats, infinities, signs, and non-decimal coercion.

## Storage and integrity controls

- The schema marker is type- and version-exact. Incompatible storage fails closed before reads or
  writes instead of silently mixing schemas.
- ID allocation repairs stale high-water metadata, uses an atomic increment, checks the destination
  key, and probes bounded collisions. Existing decision keys are not intentionally overwritten.
- Records are parsed on defensive copies. Required fields, canonical tags, identity, timestamps,
  status, closure author, and closure chronology must all pass validation.
- A loaded payload ID must match the canonical ID in its padded KV key; mismatches never render as a
  different decision.
- Recent reads generate descending keys and use `get_many` batches of at most 50. They do not trust
  a single capped or implementation-defined `list()` result.
- Interactive text search scans at most 20,000 candidate IDs and retains at most 500 valid channel
  records. Exact-ID lookup bypasses that recent window. The 20,000 ceiling does not constrain key
  inventory, count repair, ledger health, or dashboard pagination.
- Complete inventory recursively partitions canonical decimal key prefixes until each exact-count
  branch fits the 1,000-key `list()` cap. Cardinality is checked before and after, so all decision
  keys within the 10,000-key quota are covered regardless of ID gaps; concurrent changes fail
  closed.
- Open/closed totals use dirty/ready metadata, ledger revision, active-writer markers, counted-key
  cardinality, and a separate malformed count. Deferred list and mutation flows can trigger a
  defensive full-inventory rebuild; dashboard RPCs fail fast rather than exceed their shorter
  deadline. Malformed keys and payloads are excluded and logged in aggregate.
- Count rebuilds and sparse dashboard traversal process records in batches of at most 50. They
  retain only bounded working records, at most 10,000 numeric key IDs, and—during dashboard
  traversal—the requested page of at most 50 rows.
- New-record and list-state admission preserves 64 KV slots for recovery. New close-verification
  state preserves 32 operational slots so it cannot consume all mutation-marker capacity.
- The close flow sends its modal before any KV call because a transport RPC may exceed Discord's
  acknowledgement deadline. The modal's editable content is only the outcome; the submitted field
  ID and random token must exactly
  match five-minute server-side state bound to actor, channel, and decision before the record is
  loaded or changed.
- A failed count update cannot turn a successful primary write into a false failure. A failed
  confirmation after commit reports that the decision was saved and tells the user how to verify it.
- Logs contain operation metadata, IDs, counters, and error types—not full decision text.

## Concurrency boundary

Atomic KV increment protects ID allocation. YourBot KV does not expose compare-and-swap for record
replacement, so closure cannot be fully transactional. A 15-second, decision-scoped
`ctx.ephemeral.dedup` guard prevents ordinary simultaneous first-close submissions.

The guard uses YourBot's non-durable, evictable ephemeral service and requires no manifest
capability. If the service is unavailable, DecisionBook logs the condition, refuses the closure,
and asks the user to retry. Eviction can still leave a last-authorized-write-wins race. Do not claim
stronger closure guarantees without a new platform primitive or a reviewed storage redesign.

## Capability boundary

Only `interaction:respond` and `storage:kv` are declared. The source audit permits
`ctx.ephemeral.dedup` only in the close handler with a decision-scoped key and 15-second TTL; no
additional capability is required for that SDK surface.

The audit rejects direct Discord REST calls, HTTP, WebSockets, SQL, message-event handlers,
schedules, dangerous built-ins, dynamic imports, credential surfaces, and forbidden modules.
DecisionBook requests no proxy domains and stores no secrets.

## Packaging and release controls

The deterministic ZIP builder derives its filename from the validated manifest identity and writes
only the explicit runtime allowlist with fixed metadata. Bundle validation rejects path traversal,
absolute or duplicate entries, symlinks, executables, encryption, unexpected compression, excess
file/size limits, development artifacts, and content that differs from current source.

CI enforces lint, runtime type checking, medium/high security findings, dependency vulnerabilities,
90% branch coverage, public YourBot validation and doctor checks, the explicit fail-closed audit,
deterministic rebuilds, source parity, and validation of the staged ZIP contents.

## Platform assumptions

YourBot is responsible for per-plugin/per-server KV isolation, ephemeral-service behavior, and
sandbox enforcement. DecisionBook does not treat the runtime filesystem or ephemeral service as
durable. The full-inventory paths cover the platform's 10,000-key quota independent of ID sparsity;
only interactive text search has the documented 20,000-candidate limit.

Channel scoping prevents DecisionBook commands and controls from reading a record in a different
channel, but it is not a secrecy guarantee: ordinary output is visible to anyone who can read the
originating Discord channel. The manager-only dashboard is intentionally server-wide and exposes
validated decisions across channels to that administrative role.

## Reporting

Use GitHub's [private vulnerability reporting form](https://github.com/CubCadet/yourbot-plugin-decisionbook/security/advisories/new).
Do not open a public issue for a suspected vulnerability.

Include the affected DecisionBook version, affected surface, security impact, and the smallest
reproduction possible. Replace server names, user IDs, decision text, and screenshots with
sanitized examples. Never submit Discord credentials, YourBot credentials, private server
decisions, or data belonging to another server.

If the private form is temporarily unavailable, contact the maintainer through the YourBot
developer profile and request a private reporting channel without disclosing vulnerability details.

Test only against Discord servers and data you own or are explicitly authorized to use. Avoid
privacy violations, service disruption, denial-of-service testing, and access to other tenants.
Allow the maintainer reasonable time to investigate and coordinate disclosure before publishing
technical details.
