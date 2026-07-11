# DecisionBook security

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
  capped or implementation-defined `list_values` ordering.
- Search, pagination, collision probes, and sparse-ledger traversal are bounded for sandbox safety.
- Open/closed totals use dirty/ready metadata and the counted-key cardinality. Missing or
  inconsistent metadata triggers a defensive rebuild; unavailable records are excluded and logged
  in aggregate.
- A failed count update cannot turn a successful primary write into a false failure. A failed
  confirmation after commit reports that the decision was saved and tells the user how to verify it.
- Logs contain operation metadata, IDs, counters, and error types—not full decision text.

## Concurrency boundary

Atomic KV increment protects ID allocation. YourBot KV does not expose compare-and-swap for record
replacement, so closure cannot be fully transactional. A 15-second, decision-scoped
`ctx.ephemeral.dedup` guard prevents ordinary simultaneous first-close submissions.

The guard uses YourBot's non-durable, evictable ephemeral service and requires no manifest
capability. If the service is unavailable, DecisionBook logs the condition and permits the
authorized closure rather than making the feature unavailable. Eviction or guard failure can still
leave a last-authorized-write-wins race. Do not claim stronger closure guarantees without a new
platform primitive or a reviewed storage redesign.

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
durable. Exact count repair and complete pagination are guaranteed only within the documented
20,000-candidate supported ledger range; an unsafe sparse state fails closed.

## Reporting

Report vulnerabilities privately to the plugin maintainer through the YourBot developer profile.
Do not include private server decisions in a public report.
