"""DecisionBook handlers, durable-state orchestration, and dashboard RPCs."""

from __future__ import annotations

import re
import secrets
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from yourbot_sdk import ActionRow, Button, Context, KvQuotaError, Plugin, TextInput

from core import (
    KEY_WIDTH,
    LIMITS,
    SCAN_LIMIT,
    SCHEMA_VERSION,
    InputError,
    close_record,
    decision_embed,
    decision_key,
    display_text,
    list_embed,
    make_record,
    matching_records,
    normalize_text,
    parse_channel_id,
    parse_decision_id,
    parse_discord_id,
    parse_record,
)

plugin = Plugin()
NO_MENTIONS = {"parse": []}

SCHEMA_KEY = "meta:schema_version"
COUNTER_KEY = "meta:next_id"
OPEN_COUNT_KEY = "meta:open_count"
CLOSED_COUNT_KEY = "meta:closed_count"
COUNTED_KEYS_KEY = "meta:counted_decision_keys"
MALFORMED_COUNT_KEY = "meta:malformed_count"
COUNTED_REVISION_KEY = "meta:counted_revision"
COUNTS_STATE_KEY = "meta:counts_state"
LEDGER_REVISION_KEY = "meta:ledger_revision"
COUNTS_READY = "ready"
COUNTS_DIRTY = "dirty"

DECISION_PREFIX = "decision:"
LIST_STATE_PREFIX = "ui:list:"
CLOSE_INTENT_PREFIX = "ui:close:"
MUTATION_PREFIX = "meta:mutation:"
LIST_STATE_TTL_SECONDS = 15 * 60
CLOSE_INTENT_TTL_SECONDS = 5 * 60
MUTATION_TTL_SECONDS = 5 * 60
MUTATION_BASELINE_RETRIES = 25
KV_GET_MANY_LIMIT = 50
MAX_LEDGER_SCAN = 20_000
MAX_DECISION_ID = (10**KEY_WIDTH) - 1
MAX_COLLISION_PROBES = 1_000
KV_KEY_QUOTA = 10_000
KV_WRITE_RESERVE = 64
KV_OPERATIONAL_RESERVE = 32

ADD_MODAL_ID = "decision:add"
CLOSE_MODAL_ID = "decision:close"
CLOSE_OUTCOME_PREFIX = "outcome:"
LIST_COMPONENT_PREFIX = "decision:page:"
VIEW_COMPONENT_PREFIX = "decision:view:"
CLOSE_COMPONENT_PREFIX = "decision:close:"
_LIST_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,24}")
_CLOSE_OUTCOME_FIELD = re.compile(r"outcome:([1-9][0-9]*):([A-Za-z0-9_-]{8,24})")


class StorageStateError(RuntimeError):
    """The ledger cannot be used safely without repair or migration."""


class StorageBusyError(StorageStateError):
    """A valid concurrent mutation prevented a stable metadata snapshot."""


class StorageCapacityError(RuntimeError):
    """The ledger reached its reserved safe-write capacity."""


@dataclass(frozen=True)
class DecisionLoad:
    status: str
    record: dict[str, Any] | None = None


@dataclass(frozen=True)
class RecordWindow:
    records: list[dict[str, Any]]
    high_water: int
    stored_count: int
    malformed_count: int
    scanned_count: int
    complete: bool


@dataclass(frozen=True)
class DecisionKeyInventory:
    ids: list[int]
    malformed_keys: int
    total_keys: int


def _options(items: Any) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    if not isinstance(items, list):
        return parsed
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            parsed[item["name"]] = item.get("value")
    return parsed


def options_from(event: dict[str, Any]) -> dict[str, Any]:
    """Read flat options used by modal helpers and backwards-compatible tests."""
    return _options(event.get("command_options") or event.get("options") or [])


def subcommand_from(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Read one nested /decision subcommand from either observed SDK key."""
    raw = event.get("command_options") or event.get("options") or []
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise InputError("Choose one DecisionBook action. Try /decision help.")
    action = normalize_text(raw[0].get("name")).lower()
    if action not in {"add", "view", "list", "close", "help"}:
        raise InputError("That DecisionBook action is unavailable. Try /decision help.")
    return action, _options(raw[0].get("options") or [])


def modal_values_from(event: dict[str, Any]) -> dict[str, Any]:
    values = event.get("modal_values")
    return dict(values) if isinstance(values, dict) else {}


def actor_from(event: dict[str, Any]) -> str:
    return parse_discord_id(event.get("user_id"))


def channel_from(event: dict[str, Any]) -> str:
    return parse_channel_id(event.get("channel_id"))


def safe_log(
    ctx: Context,
    message: str,
    *,
    level: str = "info",
    tags: list[str] | None = None,
    **extra: Any,
) -> None:
    """Observability must never break a customer interaction."""
    try:
        ctx.log(message, level=level, tags=tags, **extra)
    except Exception:
        return


def metric(ctx: Context, name: str, **tags: str) -> None:
    try:
        ctx.metrics.record(name, tags=tags or None)
    except Exception:
        safe_log(
            ctx,
            "DecisionBook metric unavailable",
            level="warning",
            tags=["metrics"],
            metric=name,
        )


def respond_error(ctx: Context, message: str, *, deferred: bool = False) -> None:
    content = display_text(message, 1800)
    if deferred:
        ctx.interaction.followup(
            content=content,
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
    else:
        ctx.interaction.respond(
            content=content,
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )


def safe_respond_error(ctx: Context, message: str, *, deferred: bool = False) -> None:
    try:
        respond_error(ctx, message, deferred=deferred)
    except Exception as exc:
        safe_log(
            ctx,
            "DecisionBook could not deliver an error response",
            level="error",
            tags=["interaction"],
            error_type=type(exc).__name__,
        )


def respond_embed(
    ctx: Context,
    embed: dict[str, Any],
    *,
    content: str = "",
    components: list[Any] | None = None,
    ephemeral: bool = False,
    deferred: bool = False,
) -> None:
    if deferred:
        ctx.interaction.followup(
            content=content,
            embeds=[embed],
            components=components,
            ephemeral=ephemeral,
            allowed_mentions=NO_MENTIONS,
        )
    else:
        ctx.interaction.respond(
            content=content,
            embeds=[embed],
            components=components,
            ephemeral=ephemeral,
            allowed_mentions=NO_MENTIONS,
        )


def _nonnegative_integer(value: Any) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _require_safe_capacity(ctx: Context, *, required: int, preserve_reserve: bool) -> None:
    used = int(ctx.kv.count())
    reserve = KV_WRITE_RESERVE if preserve_reserve else 0
    if used + required > KV_KEY_QUOTA - reserve:
        raise StorageCapacityError("DecisionBook has reached its safe KV capacity.")


def require_schema(ctx: Context) -> None:
    marker = ctx.kv.get(SCHEMA_KEY)
    if marker is None:
        if int(ctx.kv.count(prefix=DECISION_PREFIX)):
            raise StorageStateError(
                "DecisionBook found records without a schema marker and cannot relabel them safely."
            )
        ctx.kv.set(SCHEMA_KEY, SCHEMA_VERSION)
        return
    if type(marker) is not int or marker != SCHEMA_VERSION:
        raise StorageStateError(
            f"Unsupported DecisionBook storage schema {marker!r}; expected {SCHEMA_VERSION}."
        )


def _decision_id_from_key(key: Any) -> int | None:
    if not isinstance(key, str) or not key.startswith(DECISION_PREFIX):
        return None
    suffix = key.removeprefix(DECISION_PREFIX)
    if len(suffix) != KEY_WIDTH or not suffix.isascii() or not suffix.isdigit():
        return None
    try:
        decision_id = parse_decision_id(suffix)
    except InputError:
        return None
    return decision_id if decision_key(decision_id) == key else None


def inventory_decision_keys(
    ctx: Context,
    *,
    expected_total: int | None = None,
) -> DecisionKeyInventory:
    """Prove the canonical numeric key inventory despite the 1,000-key list cap."""
    total = int(ctx.kv.count(prefix=DECISION_PREFIX))
    if expected_total is not None and total != expected_total:
        raise StorageBusyError("The DecisionBook key inventory changed while it was read.")
    if total > KV_KEY_QUOTA:
        raise StorageStateError("DecisionBook contains more decision keys than the KV quota.")

    ids: list[int] = []
    malformed = 0

    def visit(prefix: str, count: int, depth: int) -> None:
        nonlocal malformed
        if count <= 1_000:
            keys = ctx.kv.list(prefix=prefix, limit=max(1, count))
            if len(keys) != count or len(keys) != len(set(keys)):
                raise StorageBusyError("DecisionBook could not prove a complete key inventory.")
            for key in keys:
                decision_id = _decision_id_from_key(key)
                if decision_id is None:
                    malformed += 1
                else:
                    ids.append(decision_id)
            return

        if depth >= KEY_WIDTH:
            decision_id = _decision_id_from_key(prefix)
            if decision_id is not None and ctx.kv.exists(prefix):
                ids.append(decision_id)
                malformed += count - 1
            else:
                malformed += count
            return

        child_total = 0
        for digit in "0123456789":
            child_prefix = f"{prefix}{digit}"
            child_count = int(ctx.kv.count(prefix=child_prefix))
            child_total += child_count
            if child_count:
                visit(child_prefix, child_count, depth + 1)
        if child_total > count:
            raise StorageBusyError("DecisionBook key-prefix counts changed during inventory.")
        malformed += count - child_total

    if total:
        visit(DECISION_PREFIX, total, 0)
    if int(ctx.kv.count(prefix=DECISION_PREFIX)) != total:
        raise StorageBusyError("The DecisionBook key inventory changed while it was read.")
    if len(ids) + malformed != total or len(ids) != len(set(ids)):
        raise StorageStateError("DecisionBook key inventory is internally inconsistent.")
    return DecisionKeyInventory(sorted(ids, reverse=True), malformed, total)


def discover_high_water(
    ctx: Context,
    *,
    repair: bool = True,
    verify_inventory: bool = True,
    _inventory: DecisionKeyInventory | None = None,
) -> tuple[int, int]:
    """Find a safe allocation/search floor without trusting one metadata key."""
    require_schema(ctx)
    stored_count = int(ctx.kv.count(prefix=DECISION_PREFIX))
    raw_counter = ctx.kv.get(COUNTER_KEY)
    counter = _nonnegative_integer(raw_counter)
    if raw_counter is not None and counter is None:
        raise StorageStateError(
            "The DecisionBook ID counter is invalid and cannot be repaired safely."
        )

    inventory = _inventory
    if inventory is not None and inventory.total_keys != stored_count:
        raise StorageBusyError("The DecisionBook key inventory changed while it was used.")
    if verify_inventory and inventory is None:
        inventory = inventory_decision_keys(ctx, expected_total=stored_count)

    observed = max(inventory.ids, default=0) if inventory is not None else 0
    listed_keys: list[str] = []
    if inventory is None:
        key_limit = min(max(stored_count, 1), 1_000)
        listed_keys = ctx.kv.list(prefix=DECISION_PREFIX, limit=key_limit)
        for key in listed_keys:
            decision_id = _decision_id_from_key(key)
            if decision_id is not None:
                observed = max(observed, decision_id)

    floor = max(stored_count, observed)
    high_water = max(counter or 0, floor)
    if high_water > MAX_DECISION_ID:
        raise StorageStateError("The DecisionBook ID counter is outside its supported range.")
    repair_needed = counter is None or counter < floor
    if repair and repair_needed:
        if inventory is None and stored_count > len(listed_keys):
            inventory = inventory_decision_keys(ctx, expected_total=stored_count)
            observed = max(inventory.ids, default=0)
            floor = max(stored_count, observed)
            high_water = max(counter or 0, floor)
            if high_water > MAX_DECISION_ID:
                raise StorageStateError(
                    "The DecisionBook ID counter is outside its supported range."
                )
        deficit = floor - (counter or 0)
        if deficit:
            advanced = _nonnegative_integer(ctx.kv.increment(COUNTER_KEY, amount=deficit))
            if advanced is None or advanced < floor:
                raise StorageStateError(
                    "DecisionBook could not advance its ID counter monotonically."
                )
            if advanced > MAX_DECISION_ID:
                raise StorageStateError(
                    "The DecisionBook ID counter is outside its supported range."
                )
            high_water = max(high_water, advanced)
    return high_water, stored_count


def allocate_decision_id(ctx: Context) -> int:
    discover_high_water(ctx, repair=True, verify_inventory=False)
    collisions = 0
    while collisions < MAX_COLLISION_PROBES:
        raw = ctx.kv.increment(COUNTER_KEY)
        try:
            decision_id = parse_decision_id(raw)
        except InputError as exc:
            raise StorageStateError("DecisionBook could not allocate a valid decision ID.") from exc
        if decision_id > MAX_DECISION_ID:
            raise StorageStateError("DecisionBook has reached its supported ID range.")
        if not ctx.kv.exists(decision_key(decision_id)):
            if collisions:
                safe_log(
                    ctx,
                    "DecisionBook repaired stale ID collisions",
                    level="warning",
                    tags=["storage", "repair"],
                    collisions=collisions,
                )
            return decision_id
        collisions += 1
    raise StorageStateError("DecisionBook could not find a collision-free decision ID.")


def load_decision(ctx: Context, decision_id: int) -> DecisionLoad:
    require_schema(ctx)
    expected_id = parse_decision_id(decision_id)
    raw = ctx.kv.get(decision_key(expected_id))
    if raw is None:
        return DecisionLoad("missing")
    parsed = parse_record(raw)
    if parsed is None or parsed["id"] != expected_id:
        safe_log(
            ctx,
            "Unavailable DecisionBook record rejected",
            level="warning",
            tags=["storage"],
            decision_id=expected_id,
        )
        return DecisionLoad("unavailable")
    return DecisionLoad("ok", parsed)


def _chunks(values: Iterable[int], size: int = KV_GET_MANY_LIMIT) -> Iterable[list[int]]:
    chunk: list[int] = []
    for value in values:
        chunk.append(value)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _load_ids(ctx: Context, ids: Iterable[int]) -> tuple[list[dict[str, Any]], int, int]:
    records: list[dict[str, Any]] = []
    malformed = 0
    scanned = 0
    for batch in _chunks(ids):
        keys = [decision_key(decision_id) for decision_id in batch]
        values = ctx.kv.get_many(keys)
        scanned += len(batch)
        for decision_id, key in zip(batch, keys, strict=True):
            raw = values.get(key)
            if raw is None:
                continue
            parsed = parse_record(raw)
            if parsed is None or parsed["id"] != decision_id:
                malformed += 1
                continue
            records.append(parsed)
    return records, malformed, scanned


def load_record_window(
    ctx: Context,
    *,
    record_limit: int = SCAN_LIMIT,
    high_water: int | None = None,
    candidate_limit: int = MAX_LEDGER_SCAN,
    channel_id: str | None = None,
) -> RecordWindow:
    """Load newest records from generated padded keys, never KV list ordering."""
    current_high, stored_count = discover_high_water(
        ctx,
        repair=True,
        verify_inventory=False,
    )
    start = current_high if high_water is None else min(current_high, high_water)
    if start < 1 or record_limit < 1:
        return RecordWindow([], current_high, stored_count, 0, 0, True)
    budget = min(start, max(1, candidate_limit), MAX_LEDGER_SCAN)
    records: list[dict[str, Any]] = []
    malformed = 0
    scanned = 0
    valid_scanned = 0
    next_id = start
    while next_id >= 1 and scanned < budget and len(records) < record_limit:
        batch_size = min(KV_GET_MANY_LIMIT, budget - scanned, next_id)
        ids = range(next_id, next_id - batch_size, -1)
        loaded, rejected, examined = _load_ids(ctx, ids)
        valid_scanned += len(loaded)
        if channel_id is not None:
            loaded = [record for record in loaded if record["channel_id"] == channel_id]
        records.extend(loaded)
        malformed += rejected
        scanned += examined
        next_id -= batch_size
    if next_id < 1:
        malformed += max(0, stored_count - valid_scanned - malformed)
    if malformed:
        safe_log(
            ctx,
            "DecisionBook skipped unavailable stored records",
            level="warning",
            tags=["storage"],
            skipped=malformed,
        )
    return RecordWindow(
        records[:record_limit],
        current_high,
        stored_count,
        malformed,
        scanned,
        next_id < 1,
    )


def _ledger_revision(ctx: Context) -> int:
    raw = ctx.kv.get(LEDGER_REVISION_KEY)
    if raw is None:
        return 0
    revision = _nonnegative_integer(raw)
    if revision is None:
        raise StorageStateError("The DecisionBook ledger revision is invalid.")
    return revision


def _active_mutations(ctx: Context) -> int:
    return int(ctx.kv.count(prefix=MUTATION_PREFIX))


def _snapshot_is_stable(ctx: Context, *, revision: int, stored_count: int) -> bool:
    return (
        _active_mutations(ctx) == 0
        and _ledger_revision(ctx) == revision
        and int(ctx.kv.count(prefix=DECISION_PREFIX)) == stored_count
    )


def _require_stable_snapshot(ctx: Context, *, revision: int, stored_count: int) -> None:
    try:
        stable = _snapshot_is_stable(ctx, revision=revision, stored_count=stored_count)
    except Exception:
        with suppress(Exception):
            ctx.kv.set(COUNTS_STATE_KEY, COUNTS_DIRTY)
        raise
    if not stable:
        with suppress(Exception):
            ctx.kv.set(COUNTS_STATE_KEY, COUNTS_DIRTY)
        raise StorageBusyError("The DecisionBook ledger changed while status counts were rebuilt.")


def _rebuild_status_counts(ctx: Context) -> tuple[int, int]:
    inventory = inventory_decision_keys(ctx)
    _, stored_count = discover_high_water(
        ctx,
        repair=True,
        _inventory=inventory,
    )
    revision = _ledger_revision(ctx)
    ctx.kv.set(COUNTS_STATE_KEY, COUNTS_DIRTY)
    _require_stable_snapshot(ctx, revision=revision, stored_count=stored_count)
    opened = 0
    closed = 0
    malformed = inventory.malformed_keys
    for batch in _chunks(inventory.ids):
        records, rejected, _ = _load_ids(ctx, batch)
        malformed += rejected
        opened += sum(item["status"] == "open" for item in records)
        closed += sum(item["status"] == "closed" for item in records)
    _require_stable_snapshot(ctx, revision=revision, stored_count=stored_count)
    unaccounted = stored_count - opened - closed - malformed
    if unaccounted < 0:
        raise StorageStateError("DecisionBook found inconsistent decision-key accounting.")
    malformed += unaccounted
    ctx.kv.set_many(
        {
            OPEN_COUNT_KEY: opened,
            CLOSED_COUNT_KEY: closed,
            COUNTED_KEYS_KEY: stored_count,
            MALFORMED_COUNT_KEY: malformed,
            COUNTED_REVISION_KEY: revision,
        }
    )
    _require_stable_snapshot(ctx, revision=revision, stored_count=stored_count)
    ctx.kv.set(COUNTS_STATE_KEY, COUNTS_READY)
    _require_stable_snapshot(ctx, revision=revision, stored_count=stored_count)
    if malformed:
        safe_log(
            ctx,
            "DecisionBook status counts exclude unavailable records",
            level="warning",
            tags=["storage", "repair"],
            skipped=malformed,
        )
    return opened, closed


def status_counts(ctx: Context, *, repair: bool = True) -> tuple[int, int]:
    require_schema(ctx)
    opened = _nonnegative_integer(ctx.kv.get(OPEN_COUNT_KEY))
    closed = _nonnegative_integer(ctx.kv.get(CLOSED_COUNT_KEY))
    counted = _nonnegative_integer(ctx.kv.get(COUNTED_KEYS_KEY))
    malformed = _nonnegative_integer(ctx.kv.get(MALFORMED_COUNT_KEY))
    counted_revision = _nonnegative_integer(ctx.kv.get(COUNTED_REVISION_KEY))
    state = ctx.kv.get(COUNTS_STATE_KEY)
    stored_count = int(ctx.kv.count(prefix=DECISION_PREFIX))
    revision = _ledger_revision(ctx)
    active_mutations = _active_mutations(ctx)
    if active_mutations:
        raise StorageBusyError("A DecisionBook write is still in progress.")
    if (
        state != COUNTS_READY
        or opened is None
        or closed is None
        or malformed is None
        or counted != stored_count
        or counted_revision != revision
        or opened + closed + malformed != stored_count
    ):
        if not repair:
            raise StorageStateError("DecisionBook status counts require repair.")
        return _rebuild_status_counts(ctx)
    return opened, closed


def _mark_counts_dirty(ctx: Context) -> None:
    ctx.kv.set(COUNTS_STATE_KEY, COUNTS_DIRTY)


def _begin_primary_mutation(ctx: Context) -> str:
    # Establish an exact baseline before applying an atomic aggregate delta. A
    # concurrent writer keeps its marker until its count delta is complete, so
    # a short bounded retry avoids rejecting an otherwise healthy write.
    for attempt in range(MUTATION_BASELINE_RETRIES):
        try:
            status_counts(ctx)
            break
        except StorageBusyError:
            if attempt == MUTATION_BASELINE_RETRIES - 1:
                raise
            time.sleep(min(0.01 * (attempt + 1), 0.05))
    marker = f"{MUTATION_PREFIX}{secrets.token_urlsafe(18)}"
    ctx.kv.set(marker, True, ttl_seconds=MUTATION_TTL_SECONDS)
    try:
        _mark_counts_dirty(ctx)
    except Exception:
        with suppress(Exception):
            ctx.kv.delete(marker)
        raise
    return marker


def _abort_primary_mutation(ctx: Context, marker: str) -> None:
    with suppress(Exception):
        _mark_counts_dirty(ctx)
    with suppress(Exception):
        ctx.kv.delete(marker)


def _complete_primary_mutation(ctx: Context, marker: str) -> bool:
    """Publish a primary commit revision while retaining its write marker."""
    revision_advanced = False
    try:
        revision = ctx.kv.increment(LEDGER_REVISION_KEY)
        if _nonnegative_integer(revision) is None:
            raise StorageStateError("Invalid ledger revision result.")
        revision_advanced = True
    except Exception as exc:
        safe_log(
            ctx,
            "DecisionBook committed a record but could not advance its ledger revision",
            level="error",
            tags=["storage", "committed"],
            error_type=type(exc).__name__,
        )
    with suppress(Exception):
        _mark_counts_dirty(ctx)
    return revision_advanced


def _finish_count_update(
    ctx: Context,
    *,
    marker: str | None = None,
    revision_advanced: bool = True,
    opened: int = 0,
    closed: int = 0,
) -> None:
    """Repairable metadata never changes the primary commit result."""
    marker_released = marker is None
    try:
        if not revision_advanced:
            raise StorageStateError("The committed ledger revision could not be advanced.")
        if opened:
            value = ctx.kv.increment(OPEN_COUNT_KEY, amount=opened)
            if _nonnegative_integer(value) is None:
                raise StorageStateError("Invalid open decision counter result.")
        if closed:
            value = ctx.kv.increment(CLOSED_COUNT_KEY, amount=closed)
            if _nonnegative_integer(value) is None:
                raise StorageStateError("Invalid closed decision counter result.")
        current_open = _nonnegative_integer(ctx.kv.get(OPEN_COUNT_KEY))
        current_closed = _nonnegative_integer(ctx.kv.get(CLOSED_COUNT_KEY))
        malformed = _nonnegative_integer(ctx.kv.get(MALFORMED_COUNT_KEY))
        stored_count = int(ctx.kv.count(prefix=DECISION_PREFIX))
        if (
            current_open is None
            or current_closed is None
            or malformed is None
            or current_open + current_closed + malformed != stored_count
        ):
            raise StorageStateError("DecisionBook aggregate counters are not exact.")
        ctx.kv.set(COUNTED_KEYS_KEY, stored_count)
        ctx.kv.set(COUNTED_REVISION_KEY, _ledger_revision(ctx))
        if marker is not None:
            ctx.kv.delete(marker)
            marker_released = True
        if _active_mutations(ctx):
            raise StorageStateError("A ledger mutation is still in progress.")
        revision = _ledger_revision(ctx)
        stored_count = int(ctx.kv.count(prefix=DECISION_PREFIX))
        _require_stable_snapshot(ctx, revision=revision, stored_count=stored_count)
        ctx.kv.set(COUNTS_STATE_KEY, COUNTS_READY)
        _require_stable_snapshot(ctx, revision=revision, stored_count=stored_count)
    except Exception as exc:
        with suppress(Exception):
            ctx.kv.set(COUNTS_STATE_KEY, COUNTS_DIRTY)
        safe_log(
            ctx,
            "DecisionBook status counts marked for repair",
            level="warning",
            tags=["storage", "repair"],
            error_type=type(exc).__name__,
        )
    finally:
        if marker is not None and not marker_released:
            try:
                ctx.kv.delete(marker)
            except Exception as exc:
                safe_log(
                    ctx,
                    "DecisionBook mutation marker will expire automatically",
                    level="warning",
                    tags=["storage", "committed"],
                    error_type=type(exc).__name__,
                )


def _record_components(record: dict[str, Any]) -> list[Any]:
    if record["status"] != "open":
        return []
    return [
        ActionRow(
            Button(
                "Close decision",
                custom_id=f"{CLOSE_COMPONENT_PREFIX}{record['id']}",
                style="success",
                emoji="✅",
            )
        )
    ]


def _deliver_committed(
    ctx: Context,
    record: dict[str, Any],
    *,
    operation: str,
    content: str = "",
    deferred: bool = False,
) -> None:
    try:
        respond_embed(
            ctx,
            decision_embed(record),
            content=content,
            components=_record_components(record),
            deferred=deferred,
        )
    except Exception as exc:
        safe_log(
            ctx,
            f"DecisionBook {operation} committed but confirmation failed",
            level="error",
            tags=["interaction", "committed"],
            decision_id=record["id"],
            error_type=type(exc).__name__,
        )
        safe_respond_error(
            ctx,
            f"Decision #{record['id']} was saved, but its confirmation could not be displayed. "
            f"Use /decision view with ID {record['id']} to verify it.",
            deferred=deferred,
        )


def _unavailable_message(decision_id: int, status: str) -> str:
    if status == "missing":
        return f"Decision #{decision_id} was not found in this channel's DecisionBook."
    return (
        f"Decision #{decision_id} exists but is temporarily unavailable because its stored "
        "record did not pass integrity checks. The record was not changed."
    )


@plugin.on_install
def install(ctx: Context) -> None:
    require_schema(ctx)
    discover_high_water(ctx, repair=True)
    status_counts(ctx)
    safe_log(ctx, "DecisionBook installed", tags=["lifecycle"], schema_version=SCHEMA_VERSION)


def show_add_modal(ctx: Context) -> None:
    ctx.interaction.send_modal(
        title="Record an immutable decision",
        custom_id=ADD_MODAL_ID,
        fields=[
            TextInput(
                "Title",
                "title",
                placeholder="What did the team decide?",
                min_length=1,
                max_length=LIMITS["title"],
            ),
            TextInput(
                "Decision",
                "choice",
                style="paragraph",
                placeholder="What was selected or agreed?",
                min_length=1,
                max_length=LIMITS["choice"],
            ),
            TextInput(
                "Reason",
                "reason",
                style="paragraph",
                placeholder="Why was this choice made?",
                min_length=1,
                max_length=LIMITS["reason"],
            ),
            TextInput(
                "Tags (max 5, 24 characters each)",
                "tags",
                placeholder="Optional, comma-separated: release, events",
                required=False,
                max_length=LIMITS["tags"],
            ),
        ],
    )


def decision_add(
    ctx: Context,
    event: dict[str, Any],
    values: dict[str, Any] | None = None,
    *,
    deferred: bool = False,
) -> None:
    """Validate, collision-check, commit, then report the authoritative result."""
    committed: dict[str, Any] | None = None
    mutation_marker: str | None = None
    try:
        options = values if values is not None else options_from(event)
        draft = make_record(
            1,
            title=options.get("title"),
            choice=options.get("choice"),
            reason=options.get("reason"),
            tags=options.get("tags"),
            author_id=actor_from(event),
            channel_id=channel_from(event),
        )
        _require_safe_capacity(ctx, required=2, preserve_reserve=True)
        mutation_marker = _begin_primary_mutation(ctx)
        decision_id = allocate_decision_id(ctx)
        draft["id"] = decision_id
        decision_embed(draft)
        ctx.kv.set(decision_key(decision_id), draft)
        committed = draft
        revision_advanced = _complete_primary_mutation(ctx, mutation_marker)
        _finish_count_update(
            ctx,
            marker=mutation_marker,
            revision_advanced=revision_advanced,
            opened=1,
        )
        mutation_marker = None
        metric(ctx, "decision_created")
    except InputError as exc:
        if mutation_marker is not None:
            _abort_primary_mutation(ctx, mutation_marker)
        metric(ctx, "decision_error", reason="validation")
        safe_respond_error(ctx, str(exc), deferred=deferred)
        return
    except StorageCapacityError:
        if mutation_marker is not None:
            _abort_primary_mutation(ctx, mutation_marker)
        metric(ctx, "decision_error", reason="capacity")
        safe_respond_error(
            ctx,
            "DecisionBook is at its safe storage limit. Nothing was saved; ask a server manager "
            "to review Ledger health in the dashboard.",
            deferred=deferred,
        )
        return
    except StorageStateError as exc:
        if mutation_marker is not None:
            _abort_primary_mutation(ctx, mutation_marker)
        safe_log(
            ctx,
            "Decision creation blocked by storage state",
            level="error",
            tags=["command", "storage"],
            error_type=type(exc).__name__,
        )
        metric(ctx, "decision_error", reason="state")
        safe_respond_error(
            ctx,
            "DecisionBook's ledger needs a safe storage repair before new decisions can be added. "
            "Nothing was saved. Ask a server manager to review Ledger health in the dashboard.",
            deferred=deferred,
        )
        return
    except KvQuotaError:
        if mutation_marker is not None:
            _abort_primary_mutation(ctx, mutation_marker)
        metric(ctx, "decision_error", reason="quota")
        safe_respond_error(
            ctx,
            "DecisionBook's storage quota is full. Nothing was saved; ask a server admin "
            "to review the ledger before trying again.",
            deferred=deferred,
        )
        return
    except Exception as exc:
        if mutation_marker is not None:
            _abort_primary_mutation(ctx, mutation_marker)
        safe_log(
            ctx,
            "Decision creation failed before commit",
            level="error",
            tags=["command"],
            error_type=type(exc).__name__,
        )
        metric(ctx, "decision_error", reason="storage")
        safe_respond_error(
            ctx,
            "DecisionBook could not store that decision. Nothing was saved; please try again.",
            deferred=deferred,
        )
        return
    if committed is not None:
        _deliver_committed(ctx, committed, operation="creation", deferred=deferred)


@plugin.on_modal_submit("decision:add")
def decision_add_modal(ctx: Context, event: dict[str, Any]) -> None:
    ctx.interaction.defer()
    decision_add(ctx, event, modal_values_from(event), deferred=True)


def decision_view(
    ctx: Context,
    event: dict[str, Any],
    values: dict[str, Any] | None = None,
    *,
    ephemeral: bool = False,
) -> None:
    deferred = False
    try:
        options = values if values is not None else options_from(event)
        decision_id = parse_decision_id(options.get("id"))
        channel_id = channel_from(event)
        ctx.interaction.defer(ephemeral=ephemeral)
        deferred = True
        loaded = load_decision(ctx, decision_id)
        if loaded.record is None:
            respond_error(
                ctx,
                _unavailable_message(decision_id, loaded.status),
                deferred=deferred,
            )
            return
        if loaded.record["channel_id"] != channel_id:
            respond_error(
                ctx,
                _unavailable_message(decision_id, "missing"),
                deferred=deferred,
            )
            return
        metric(ctx, "decision_viewed")
        respond_embed(
            ctx,
            decision_embed(loaded.record),
            components=_record_components(loaded.record),
            ephemeral=ephemeral,
            deferred=deferred,
        )
    except InputError as exc:
        metric(ctx, "decision_error", reason="validation")
        safe_respond_error(ctx, str(exc), deferred=deferred)
    except StorageStateError:
        metric(ctx, "decision_error", reason="state")
        safe_respond_error(
            ctx,
            "DecisionBook cannot read this ledger until its schema is repaired.",
            deferred=deferred,
        )
    except Exception as exc:
        safe_log(
            ctx,
            "Decision view failed",
            level="error",
            tags=["command"],
            error_type=type(exc).__name__,
        )
        metric(ctx, "decision_error", reason="storage")
        safe_respond_error(
            ctx,
            "DecisionBook could not load that decision. Please try again.",
            deferred=deferred,
        )


def _whole_number(
    value: Any,
    *,
    default: int,
    minimum: int = 1,
    maximum: int,
    label: str,
) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise InputError(f"{label} must be a whole number from {minimum} to {maximum}.")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise InputError(f"{label} must be a whole number from {minimum} to {maximum}.") from exc
    if str(parsed) != str(value).strip() or not minimum <= parsed <= maximum:
        raise InputError(f"{label} must be a whole number from {minimum} to {maximum}.")
    return parsed


def _list_state_key(actor_id: str, channel_id: str) -> str:
    return f"{LIST_STATE_PREFIX}{parse_discord_id(actor_id)}:{parse_channel_id(channel_id)}"


def _save_list_state(
    ctx: Context,
    *,
    query: str,
    status: str,
    limit: int,
    match_ids: list[int],
    actor_id: str,
    channel_id: str,
) -> str | None:
    try:
        _require_safe_capacity(ctx, required=1, preserve_reserve=True)
        token = secrets.token_urlsafe(9)
        ctx.kv.set(
            _list_state_key(actor_id, channel_id),
            {
                "token": token,
                "query": query,
                "status": status,
                "limit": limit,
                "match_ids": match_ids,
                "actor_id": actor_id,
                "channel_id": channel_id,
            },
            ttl_seconds=LIST_STATE_TTL_SECONDS,
        )
        return token
    except KvQuotaError as exc:
        safe_log(
            ctx,
            "DecisionBook skipped pagination state because storage quota is full",
            level="warning",
            tags=["interaction", "storage", "quota"],
            error_type=type(exc).__name__,
        )
        return None
    except Exception as exc:
        safe_log(
            ctx,
            "DecisionBook pagination controls unavailable",
            level="warning",
            tags=["interaction", "storage"],
            error_type=type(exc).__name__,
        )
        return None


def _load_list_state(
    ctx: Context,
    token: str,
    *,
    actor_id: str,
    channel_id: str,
) -> dict[str, Any] | None:
    if _LIST_TOKEN.fullmatch(token) is None:
        raise InputError("That list control is invalid. Run /decision list again.")
    raw = ctx.kv.get(_list_state_key(actor_id, channel_id))
    if not isinstance(raw, dict):
        return None
    try:
        if raw.get("token") != token:
            return None
        query = normalize_text(raw.get("query"))
        if len(query) > LIMITS["query"]:
            return None
        status = normalize_text(raw.get("status")).lower()
        if status not in {"all", "open", "closed"}:
            return None
        limit = _whole_number(raw.get("limit"), default=5, maximum=10, label="Limit")
        stored_actor = parse_discord_id(raw.get("actor_id"))
        stored_channel = parse_channel_id(raw.get("channel_id"))
        raw_ids = raw.get("match_ids")
        if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= SCAN_LIMIT:
            return None
        match_ids = [parse_decision_id(value) for value in raw_ids]
        if match_ids != sorted(set(match_ids), reverse=True):
            return None
    except InputError:
        return None
    if stored_actor != actor_id or stored_channel != channel_id:
        return None
    return {
        "query": query,
        "status": status,
        "limit": limit,
        "match_ids": match_ids,
        "actor_id": stored_actor,
        "channel_id": stored_channel,
    }


def _list_components(
    records: list[dict[str, Any]],
    *,
    token: str | None,
    page: int,
    pages: int,
) -> list[Any]:
    rows: list[Any] = []
    buttons = [
        Button(
            f"View #{item['id']}",
            custom_id=f"{VIEW_COMPONENT_PREFIX}{item['id']}",
            style="secondary",
        )
        for item in records
    ]
    for index in range(0, len(buttons), 5):
        rows.append(ActionRow(*buttons[index : index + 5]))
    if token is not None and pages > 1:
        rows.append(
            ActionRow(
                Button(
                    "Previous",
                    custom_id=f"{LIST_COMPONENT_PREFIX}{token}:{max(1, page - 1)}",
                    style="secondary",
                    disabled=page <= 1,
                ),
                Button(
                    f"Page {page}/{pages}",
                    custom_id=f"{LIST_COMPONENT_PREFIX}{token}:{page}",
                    style="secondary",
                    disabled=True,
                ),
                Button(
                    "Next",
                    custom_id=f"{LIST_COMPONENT_PREFIX}{token}:{min(pages, page + 1)}",
                    style="primary",
                    disabled=page >= pages,
                ),
            )
        )
    return rows[:5]


def _render_list_page(
    ctx: Context,
    *,
    query: str,
    status: str,
    limit: int,
    page: int,
    actor_id: str,
    channel_id: str,
    deferred: bool,
) -> None:
    exact_match = re.fullmatch(r"#?([1-9][0-9]*)", query)
    incomplete = False
    searched = 0
    scanned = 0
    channel_records: list[dict[str, Any]]
    if exact_match is not None:
        decision_id = parse_decision_id(exact_match.group(1))
        loaded = load_decision(ctx, decision_id)
        channel_records = (
            [loaded.record]
            if loaded.record is not None and loaded.record["channel_id"] == channel_id
            else []
        )
        matches = matching_records(channel_records, query="", status=status)
        searched = len(channel_records)
        scanned = searched
        if not matches:
            raise InputError(
                f"Decision #{decision_id} was not found with that status in this channel."
            )
    else:
        window = load_record_window(ctx, channel_id=channel_id)
        channel_records = window.records
        matches = matching_records(channel_records, query=query, status=status)
        incomplete = not window.complete
        searched = len(channel_records)
        scanned = window.scanned_count

        if not channel_records and window.malformed_count:
            raise StorageStateError(
                "Stored decisions were found, but none passed integrity checks."
            )

    total_matches = len(matches)
    total_pages = max(1, (total_matches + limit - 1) // limit)
    if page > total_pages:
        raise InputError(f"Page {page} does not exist. This result has {total_pages} page(s).")
    offset = (page - 1) * limit
    records = matches[offset : offset + limit]

    if not records:
        if incomplete:
            raise InputError(
                f"Search incomplete: no match was found in the newest {searched} decisions from "
                f"this channel across {scanned} recent ledger IDs, but older decisions were not "
                "all searched. Try an exact numeric ID."
            )
        if not channel_records:
            raise InputError(
                "No decisions have been recorded in this channel yet. Start with /decision add."
            )
        raise InputError(
            "No decisions match that search. Try a shorter query "
            "or change the status filter to All."
        )

    token: str | None = None
    if total_pages > 1:
        token = _save_list_state(
            ctx,
            query=query,
            status=status,
            limit=limit,
            match_ids=[record["id"] for record in matches],
            actor_id=actor_id,
            channel_id=channel_id,
        )
    else:
        try:
            ctx.kv.delete(_list_state_key(actor_id, channel_id))
        except Exception as exc:
            safe_log(
                ctx,
                "DecisionBook could not retire an older list snapshot",
                level="warning",
                tags=["interaction", "storage"],
                error_type=type(exc).__name__,
            )
    embed = list_embed(records, query=query, status=status, offset=offset, total=total_matches)
    if incomplete:
        embed["footer"]["text"] += (
            f" • Search incomplete: the newest {searched} channel decisions across "
            f"{scanned} ledger IDs were searched."
        )
    if total_pages > 1 and token is None:
        embed["footer"]["text"] += " • Pagination is temporarily unavailable."
    respond_embed(
        ctx,
        embed,
        components=_list_components(records, token=token, page=page, pages=total_pages),
        deferred=deferred,
    )


def _render_snapshot_page(
    ctx: Context,
    *,
    state: dict[str, Any],
    token: str,
    page: int,
) -> None:
    match_ids = state["match_ids"]
    limit = state["limit"]
    total_pages = max(1, (len(match_ids) + limit - 1) // limit)
    if page > total_pages:
        raise InputError(f"Page {page} does not exist. This result has {total_pages} page(s).")
    offset = (page - 1) * limit
    page_ids = match_ids[offset : offset + limit]
    records, malformed, _ = _load_ids(ctx, page_ids)
    records = [record for record in records if record["channel_id"] == state["channel_id"]]
    if not records:
        if malformed:
            raise InputError(
                "That page contains records which no longer pass integrity checks. "
                "Run /decision list again."
            )
        raise InputError("That page is no longer available. Run /decision list again.")
    embed = list_embed(
        records,
        query=state["query"],
        status=state["status"],
        offset=offset,
        total=len(match_ids),
    )
    embed["footer"]["text"] += " • Stable 15-minute result snapshot."
    respond_embed(
        ctx,
        embed,
        components=_list_components(records, token=token, page=page, pages=total_pages),
        ephemeral=True,
        deferred=True,
    )


def decision_list(
    ctx: Context,
    event: dict[str, Any],
    values: dict[str, Any] | None = None,
) -> None:
    deferred = False
    try:
        options = values if values is not None else options_from(event)
        query = normalize_text(options.get("query"))
        if len(query) > LIMITS["query"]:
            raise InputError(
                f"Query is too long ({len(query)} characters). "
                f"Keep it to {LIMITS['query']} characters or fewer."
            )
        status = normalize_text(options.get("status") or "all").lower()
        if status not in {"all", "open", "closed"}:
            raise InputError("Status must be one of: all, open, or closed.")
        limit = _whole_number(options.get("limit"), default=5, maximum=10, label="Limit")
        page = _whole_number(options.get("page"), default=1, maximum=SCAN_LIMIT, label="Page")
        actor_id = actor_from(event)
        channel_id = channel_from(event)
        ctx.interaction.defer()
        deferred = True
        # The deferred list flow is the non-mutating repair path for dashboard
        # aggregates; dashboard RPCs must stay within their shorter 10s budget.
        status_counts(ctx)
        _render_list_page(
            ctx,
            query=query,
            status=status,
            limit=limit,
            page=page,
            actor_id=actor_id,
            channel_id=channel_id,
            deferred=True,
        )
        metric(ctx, "decision_listed")
    except InputError as exc:
        metric(ctx, "decision_error", reason="validation")
        safe_respond_error(ctx, str(exc), deferred=deferred)
    except StorageStateError:
        metric(ctx, "decision_error", reason="state")
        safe_respond_error(
            ctx,
            "DecisionBook cannot search until its storage state is repaired. "
            "Ask a server manager to review Ledger health in the dashboard.",
            deferred=deferred,
        )
    except Exception as exc:
        safe_log(
            ctx,
            "Decision list failed",
            level="error",
            tags=["command"],
            error_type=type(exc).__name__,
        )
        metric(ctx, "decision_error", reason="storage")
        safe_respond_error(
            ctx,
            "DecisionBook could not search the ledger. Please try again.",
            deferred=deferred,
        )


def _close_intent_key(actor_id: str, channel_id: str) -> str:
    return f"{CLOSE_INTENT_PREFIX}{parse_discord_id(actor_id)}:{parse_channel_id(channel_id)}"


def _save_close_intent(
    ctx: Context,
    *,
    actor_id: str,
    channel_id: str,
    decision_id: int,
    token: str,
) -> None:
    if _LIST_TOKEN.fullmatch(token) is None:
        raise InputError("That close form is invalid. Open the decision and try again.")
    key = _close_intent_key(actor_id, channel_id)
    if not ctx.kv.exists(key) and int(ctx.kv.count()) + 1 > KV_KEY_QUOTA - KV_OPERATIONAL_RESERVE:
        raise StorageCapacityError("DecisionBook must preserve mutation-marker capacity.")
    ctx.kv.set(
        key,
        {
            "actor_id": actor_id,
            "channel_id": channel_id,
            "decision_id": decision_id,
            "token": token,
        },
        ttl_seconds=CLOSE_INTENT_TTL_SECONDS,
    )


def _consume_close_intent(
    ctx: Context,
    event: dict[str, Any],
    values: dict[str, Any],
) -> tuple[int, Any] | None:
    outcome_fields = [
        key for key in values if isinstance(key, str) and key.startswith(CLOSE_OUTCOME_PREFIX)
    ]
    if len(outcome_fields) != 1:
        return None
    outcome_field = outcome_fields[0]
    matched = _CLOSE_OUTCOME_FIELD.fullmatch(outcome_field)
    if matched is None:
        return None
    try:
        submitted_id = parse_decision_id(matched.group(1))
        actor_id = actor_from(event)
        channel_id = channel_from(event)
    except InputError:
        return None
    token = matched.group(2)
    key = _close_intent_key(actor_id, channel_id)
    raw = ctx.kv.get(key)
    if not isinstance(raw, dict):
        return None
    try:
        stored_actor = parse_discord_id(raw.get("actor_id"))
        stored_channel = parse_channel_id(raw.get("channel_id"))
        decision_id = parse_decision_id(raw.get("decision_id"))
        stored_token = raw.get("token")
    except InputError:
        return None
    if (
        stored_actor != actor_id
        or stored_channel != channel_id
        or decision_id != submitted_id
        or not isinstance(stored_token, str)
        or not secrets.compare_digest(stored_token, token)
    ):
        return None
    ctx.kv.delete(key)
    return decision_id, values.get(outcome_field)


def show_close_modal(ctx: Context, decision_id: int, token: str) -> None:
    ctx.interaction.send_modal(
        title=f"Close Decision #{decision_id}",
        custom_id=CLOSE_MODAL_ID,
        fields=[
            TextInput(
                "Outcome",
                f"{CLOSE_OUTCOME_PREFIX}{decision_id}:{token}",
                style="paragraph",
                placeholder="What happened after this decision?",
                min_length=1,
                max_length=LIMITS["outcome"],
            ),
        ],
    )


def prepare_close(ctx: Context, event: dict[str, Any], values: dict[str, Any]) -> None:
    try:
        decision_id = parse_decision_id(values.get("id"))
        channel_id = channel_from(event)
        actor_id = actor_from(event)
        token = secrets.token_urlsafe(18)
        show_close_modal(ctx, decision_id, token)
        try:
            _save_close_intent(
                ctx,
                actor_id=actor_id,
                channel_id=channel_id,
                decision_id=decision_id,
                token=token,
            )
        except Exception as exc:
            safe_log(
                ctx,
                "DecisionBook could not persist close-form verification state",
                level="error",
                tags=["interaction", "storage"],
                error_type=type(exc).__name__,
            )
    except InputError as exc:
        safe_respond_error(ctx, str(exc))
    except Exception as exc:
        safe_log(
            ctx,
            "Decision close form failed",
            level="error",
            tags=["command"],
            error_type=type(exc).__name__,
        )
        safe_respond_error(ctx, "DecisionBook could not open the close form. Please try again.")


def decision_close(
    ctx: Context,
    event: dict[str, Any],
    values: dict[str, Any] | None = None,
    *,
    deferred: bool = False,
) -> None:
    committed: dict[str, Any] | None = None
    mutation_marker: str | None = None
    try:
        options = values if values is not None else options_from(event)
        decision_id = parse_decision_id(options.get("id"))
        channel_id = channel_from(event)
        loaded = load_decision(ctx, decision_id)
        if loaded.record is None:
            respond_error(ctx, _unavailable_message(decision_id, loaded.status), deferred=deferred)
            return
        if loaded.record["channel_id"] != channel_id:
            respond_error(ctx, _unavailable_message(decision_id, "missing"), deferred=deferred)
            return
        if loaded.record["status"] == "closed":
            respond_embed(
                ctx,
                decision_embed(loaded.record),
                content=f"Decision #{decision_id} was already closed; no changes were made.",
                ephemeral=True,
                deferred=deferred,
            )
            return
        try:
            updated = close_record(
                loaded.record,
                actor_id=actor_from(event),
                outcome=options.get("outcome"),
            )
        except PermissionError:
            respond_error(
                ctx,
                f"Only the person who recorded Decision #{decision_id} can close it. "
                "The original record remains unchanged.",
                deferred=deferred,
            )
            return
        decision_embed(updated)
        _require_safe_capacity(ctx, required=1, preserve_reserve=False)
        mutation_marker = _begin_primary_mutation(ctx)
        try:
            acquired = ctx.ephemeral.dedup(
                f"decisionbook:close:{decision_id}",
                ttl_seconds=15,
            )
        except Exception as exc:
            _abort_primary_mutation(ctx, mutation_marker)
            mutation_marker = None
            safe_log(
                ctx,
                "DecisionBook close race guard unavailable",
                level="error",
                tags=["storage"],
                error_type=type(exc).__name__,
            )
            respond_error(
                ctx,
                "DecisionBook cannot safely close that decision right now. Please try again.",
                deferred=deferred,
            )
            return
        if not acquired:
            _abort_primary_mutation(ctx, mutation_marker)
            mutation_marker = None
            respond_error(
                ctx,
                f"Decision #{decision_id} is already being closed. "
                "Wait a moment and view it again.",
                deferred=deferred,
            )
            return
        ctx.kv.set(decision_key(decision_id), updated)
        committed = updated
        revision_advanced = _complete_primary_mutation(ctx, mutation_marker)
        _finish_count_update(
            ctx,
            marker=mutation_marker,
            revision_advanced=revision_advanced,
            opened=-1,
            closed=1,
        )
        mutation_marker = None
        metric(ctx, "decision_closed")
    except InputError as exc:
        if mutation_marker is not None:
            _abort_primary_mutation(ctx, mutation_marker)
        metric(ctx, "decision_error", reason="validation")
        safe_respond_error(ctx, str(exc), deferred=deferred)
        return
    except StorageCapacityError:
        if mutation_marker is not None:
            _abort_primary_mutation(ctx, mutation_marker)
        metric(ctx, "decision_error", reason="capacity")
        safe_respond_error(
            ctx,
            "DecisionBook is temporarily out of safe write capacity, so that decision was not "
            "changed. Ask a server manager to review Ledger health in the dashboard.",
            deferred=deferred,
        )
        return
    except StorageStateError:
        if mutation_marker is not None:
            _abort_primary_mutation(ctx, mutation_marker)
        metric(ctx, "decision_error", reason="state")
        safe_respond_error(
            ctx,
            "DecisionBook cannot close decisions until storage is repaired. "
            "Ask a server manager to review Ledger health in the dashboard.",
            deferred=deferred,
        )
        return
    except KvQuotaError:
        if mutation_marker is not None:
            _abort_primary_mutation(ctx, mutation_marker)
        metric(ctx, "decision_error", reason="quota")
        safe_respond_error(
            ctx,
            "DecisionBook's storage quota is full, so that decision was not changed. "
            "Ask a server admin to review the ledger before trying again.",
            deferred=deferred,
        )
        return
    except Exception as exc:
        if mutation_marker is not None:
            _abort_primary_mutation(ctx, mutation_marker)
        safe_log(
            ctx,
            "Decision closure failed before commit",
            level="error",
            tags=["command"],
            error_type=type(exc).__name__,
        )
        metric(ctx, "decision_error", reason="storage")
        safe_respond_error(
            ctx,
            "DecisionBook could not close that decision. "
            "Its existing record remains authoritative.",
            deferred=deferred,
        )
        return
    if committed is not None:
        _deliver_committed(ctx, committed, operation="closure", deferred=deferred)


@plugin.on_modal_submit("decision:close")
def decision_close_modal(ctx: Context, event: dict[str, Any]) -> None:
    ctx.interaction.defer()
    try:
        consumed = _consume_close_intent(ctx, event, modal_values_from(event))
        if consumed is None:
            raise InputError(
                "That close form is invalid, expired, or was replaced. "
                "Open the decision and try again."
            )
        decision_id, outcome = consumed
        decision_close(
            ctx,
            event,
            {"id": decision_id, "outcome": outcome},
            deferred=True,
        )
    except InputError as exc:
        safe_respond_error(ctx, str(exc), deferred=True)
    except Exception as exc:
        safe_log(
            ctx,
            "Decision close form state failed",
            level="error",
            tags=["interaction", "storage"],
            error_type=type(exc).__name__,
        )
        safe_respond_error(
            ctx,
            "DecisionBook could not verify that close form. Open the decision and try again.",
            deferred=True,
        )


def decision_help(ctx: Context, event: dict[str, Any]) -> None:
    del event
    ctx.interaction.respond(
        content=(
            "**DecisionBook — Decisions remembered. Context preserved.**\n\n"
            "**Quick start**\n"
            "1. /decision add → record what was chosen and why.\n"
            "2. /decision list or /decision view → revisit the context.\n"
            "3. /decision close → append the real-world outcome.\n\n"
            "**Tips**\n"
            "• Add up to five comma-separated tags, with 24 characters per tag.\n"
            "• Lists show IDs and can be paged with buttons.\n"
            "• Command access stays in the channel where each record was added.\n"
            "• Original details are immutable. Only the original author can close one.\n"
            "• The server dashboard provides counts and a paginated recent ledger."
        ),
        ephemeral=True,
        allowed_mentions=NO_MENTIONS,
    )


@plugin.on_slash_command("decision")
def decision_command(ctx: Context, event: dict[str, Any]) -> None:
    try:
        action, values = subcommand_from(event)
    except InputError as exc:
        safe_respond_error(ctx, str(exc))
        return
    if action == "add":
        try:
            channel_from(event)
        except InputError as exc:
            safe_respond_error(ctx, str(exc))
        else:
            show_add_modal(ctx)
    elif action == "view":
        decision_view(ctx, event, values)
    elif action == "list":
        decision_list(ctx, event, values)
    elif action == "close":
        prepare_close(ctx, event, values)
    else:
        decision_help(ctx, event)


@plugin.on_component(prefix="decision:view:")
def decision_view_component(ctx: Context, event: dict[str, Any]) -> None:
    custom_id = event.get("custom_id")
    value = custom_id.removeprefix(VIEW_COMPONENT_PREFIX) if isinstance(custom_id, str) else None
    decision_view(ctx, event, {"id": value}, ephemeral=True)


@plugin.on_component(prefix="decision:close:")
def decision_close_component(ctx: Context, event: dict[str, Any]) -> None:
    custom_id = event.get("custom_id")
    value = custom_id.removeprefix(CLOSE_COMPONENT_PREFIX) if isinstance(custom_id, str) else None
    prepare_close(ctx, event, {"id": value})


@plugin.on_component(prefix="decision:page:")
def decision_list_component(ctx: Context, event: dict[str, Any]) -> None:
    deferred = False
    try:
        custom_id = event.get("custom_id")
        if not isinstance(custom_id, str):
            raise InputError("That list control is invalid. Run /decision list again.")
        payload = custom_id.removeprefix(LIST_COMPONENT_PREFIX)
        token, separator, page_value = payload.rpartition(":")
        if not separator:
            raise InputError("That list control is invalid. Run /decision list again.")
        actor_id = actor_from(event)
        channel_id = channel_from(event)
        page = _whole_number(page_value, default=1, maximum=SCAN_LIMIT, label="Page")
        ctx.interaction.defer(ephemeral=True)
        deferred = True
        state = _load_list_state(
            ctx,
            token,
            actor_id=actor_id,
            channel_id=channel_id,
        )
        if state is None:
            raise InputError(
                "That list expired, was replaced, or belongs to someone else. "
                "Run /decision list to refresh it."
            )
        _render_snapshot_page(ctx, state=state, token=token, page=page)
        metric(ctx, "decision_listed")
    except InputError as exc:
        safe_respond_error(ctx, str(exc), deferred=deferred)
    except Exception as exc:
        safe_log(
            ctx,
            "DecisionBook list control failed",
            level="error",
            tags=["interaction"],
            error_type=type(exc).__name__,
        )
        safe_respond_error(
            ctx,
            "DecisionBook could not update that list. Run /decision list again.",
            deferred=deferred,
        )


@plugin.on_dashboard("get_onboarding")
def get_onboarding(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    del ctx, params
    return {
        "markdown": (
            "### Decisions remembered. Context preserved.\n"
            "Use /decision add to record a choice, /decision list to browse it, "
            "and /decision close to append the outcome. Original details remain immutable."
        )
    }


@plugin.on_dashboard("get_storage_health")
def get_storage_health(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    del params
    try:
        opened, closed = status_counts(ctx, repair=False)
        malformed = _nonnegative_integer(ctx.kv.get(MALFORMED_COUNT_KEY))
        if malformed is None:
            raise StorageStateError("The malformed-record count is invalid.")
        stored_keys = int(ctx.kv.count())
        remaining = max(0, KV_KEY_QUOTA - stored_keys)
    except StorageBusyError:
        return {
            "level": "warn",
            "message": (
                "A decision write is finishing. Counts remain protected; refresh Ledger health "
                "in a moment."
            ),
        }
    except Exception:
        return {
            "level": "error",
            "message": (
                "Ledger metadata needs repair. Run /decision list in Discord, then refresh "
                "this dashboard. If the error remains, avoid new writes and contact the plugin "
                "maintainer."
            ),
        }
    level = "warn" if remaining < 500 or malformed else "info"
    prefix = "Attention:" if malformed else "Healthy:"
    integrity = (
        f" {malformed} stored record(s) are unavailable and excluded from totals."
        if malformed
        else ""
    )
    return {
        "level": level,
        "message": (
            f"{prefix} {opened + closed} validated decisions; {remaining:,} KV key slots remain. "
            "Discord commands are channel-scoped; this manager dashboard is server-wide."
            f"{integrity}"
        ),
    }


@plugin.on_dashboard("get_total_decisions")
def get_total_decisions(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    del params
    opened, closed = status_counts(ctx, repair=False)
    return {"value": opened + closed}


@plugin.on_dashboard("get_open_decisions")
def get_open_decisions(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    del params
    opened, _ = status_counts(ctx, repair=False)
    return {"value": opened}


@plugin.on_dashboard("get_closed_decisions")
def get_closed_decisions(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    del params
    _, closed = status_counts(ctx, repair=False)
    return {"value": closed}


def _dashboard_integer(
    value: Any,
    *,
    default: int,
    minimum: int = 1,
    maximum: int,
) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(parsed, maximum))


def _dashboard_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return "Unavailable"
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def _load_inventory_page(
    ctx: Context,
    ids: list[int],
    *,
    offset: int,
    page_size: int,
) -> list[dict[str, Any]]:
    """Stream a sparse inventory and retain only the requested validated page."""
    valid_seen = 0
    page_records: list[dict[str, Any]] = []
    for batch in _chunks(ids):
        records, _, _ = _load_ids(ctx, batch)
        for record in records:
            if valid_seen >= offset:
                page_records.append(record)
                if len(page_records) == page_size:
                    return page_records
            valid_seen += 1
    return page_records


@plugin.on_dashboard("get_recent_decisions")
def get_recent_decisions(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    page_size = _dashboard_integer(
        params.get("page_size", params.get("limit")),
        default=25,
        maximum=50,
    )
    if "offset" in params:
        offset = _dashboard_integer(
            params.get("offset"),
            default=0,
            minimum=0,
            maximum=MAX_LEDGER_SCAN,
        )
    else:
        page = _dashboard_integer(params.get("page"), default=1, maximum=MAX_LEDGER_SCAN)
        offset = (page - 1) * page_size

    opened, closed = status_counts(ctx, repair=False)
    malformed = _nonnegative_integer(ctx.kv.get(MALFORMED_COUNT_KEY))
    if malformed is None:
        raise StorageStateError("The malformed-record count is invalid.")
    total = opened + closed
    if offset >= total:
        return {"rows": [], "total": total}

    high_water, stored_count = discover_high_water(
        ctx,
        repair=True,
        verify_inventory=False,
    )
    if high_water == stored_count == total:
        start = high_water - offset
        page_ids = range(start, max(0, start - page_size), -1)
        page_records, _, _ = _load_ids(ctx, page_ids)
    else:
        inventory = inventory_decision_keys(
            ctx,
            expected_total=stored_count,
        )
        if malformed == inventory.malformed_keys:
            page_records, _, _ = _load_ids(
                ctx,
                inventory.ids[offset : offset + page_size],
            )
        else:
            page_records = _load_inventory_page(
                ctx,
                inventory.ids,
                offset=offset,
                page_size=page_size,
            )
    rows = []
    for item in page_records:
        closure = item.get("closure") or {}
        tag_text = ", ".join(item["tags"])
        summary = display_text(f"{item['title']} — {item['choice']}", 180)
        if tag_text:
            summary += f" · {display_text(tag_text, 80)}"
        rows.append(
            {
                "id": item["id"],
                "status": item["status"].title(),
                "summary": summary,
                "recorded": _dashboard_time(item["created_at"]),
                "closed": _dashboard_time(closure.get("closed_at")),
            }
        )
    return {"rows": rows, "total": total}
