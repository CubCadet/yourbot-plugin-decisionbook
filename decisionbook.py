"""DecisionBook handlers, durable-state orchestration, and dashboard RPCs."""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from yourbot_sdk import ActionRow, Button, Context, Plugin, TextInput

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
COUNTS_STATE_KEY = "meta:counts_state"
COUNTS_READY = "ready"
COUNTS_DIRTY = "dirty"

DECISION_PREFIX = "decision:"
LIST_STATE_PREFIX = "ui:list:"
LIST_STATE_TTL_SECONDS = 15 * 60
KV_GET_MANY_LIMIT = 50
MAX_LEDGER_SCAN = 20_000
MAX_DECISION_ID = (10**KEY_WIDTH) - 1
MAX_COLLISION_PROBES = 1_000

ADD_MODAL_ID = "decision:add"
CLOSE_MODAL_ID = "decision:close"
LIST_COMPONENT_PREFIX = "decision:page:"
VIEW_COMPONENT_PREFIX = "decision:view:"
CLOSE_COMPONENT_PREFIX = "decision:close:"
_LIST_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,24}")


class StorageStateError(RuntimeError):
    """The ledger cannot be used safely without repair or migration."""


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


def respond_error(ctx: Context, message: str) -> None:
    ctx.interaction.respond(
        content=display_text(message, 1800),
        ephemeral=True,
        allowed_mentions=NO_MENTIONS,
    )


def safe_respond_error(ctx: Context, message: str) -> None:
    try:
        respond_error(ctx, message)
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
    update_message: bool = False,
) -> None:
    ctx.interaction.respond(
        content=content,
        embeds=[embed],
        components=components,
        ephemeral=ephemeral,
        allowed_mentions=NO_MENTIONS,
        update_message=update_message,
    )


def _nonnegative_integer(value: Any) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def require_schema(ctx: Context) -> None:
    marker = ctx.kv.get(SCHEMA_KEY)
    if marker is None:
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
    if not suffix.isascii() or not suffix.isdigit():
        return None
    try:
        return parse_decision_id(suffix)
    except InputError:
        return None


def discover_high_water(ctx: Context, *, repair: bool = True) -> tuple[int, int]:
    """Find a safe allocation/search floor without trusting one metadata key."""
    require_schema(ctx)
    stored_count = int(ctx.kv.count(prefix=DECISION_PREFIX))
    counter = _nonnegative_integer(ctx.kv.get(COUNTER_KEY))

    observed = 0
    key_limit = min(max(stored_count, 1), 1_000)
    for key in ctx.kv.list(prefix=DECISION_PREFIX, limit=key_limit):
        decision_id = _decision_id_from_key(key)
        if decision_id is not None:
            observed = max(observed, decision_id)

    floor = max(stored_count, observed)
    high_water = max(counter or 0, floor)
    if high_water > MAX_DECISION_ID:
        raise StorageStateError("The DecisionBook ID counter is outside its supported range.")
    if repair and counter != high_water:
        ctx.kv.set(COUNTER_KEY, high_water)
    return high_water, stored_count


def allocate_decision_id(ctx: Context) -> int:
    discover_high_water(ctx, repair=True)
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
) -> RecordWindow:
    """Load newest records from generated padded keys, never KV list ordering."""
    current_high, stored_count = discover_high_water(ctx, repair=True)
    start = current_high if high_water is None else min(current_high, high_water)
    if start < 1 or record_limit < 1:
        return RecordWindow([], current_high, stored_count, 0)
    budget = min(start, max(1, candidate_limit), MAX_LEDGER_SCAN)
    records: list[dict[str, Any]] = []
    malformed = 0
    scanned = 0
    next_id = start
    while next_id >= 1 and scanned < budget and len(records) < record_limit:
        batch_size = min(KV_GET_MANY_LIMIT, budget - scanned, next_id)
        ids = range(next_id, next_id - batch_size, -1)
        loaded, rejected, examined = _load_ids(ctx, ids)
        records.extend(loaded)
        malformed += rejected
        scanned += examined
        next_id -= batch_size
    if malformed:
        safe_log(
            ctx,
            "DecisionBook skipped unavailable stored records",
            level="warning",
            tags=["storage"],
            skipped=malformed,
        )
    return RecordWindow(records[:record_limit], current_high, stored_count, malformed)


def _rebuild_status_counts(ctx: Context) -> tuple[int, int]:
    high_water, stored_count = discover_high_water(ctx, repair=True)
    if high_water > MAX_LEDGER_SCAN:
        raise StorageStateError(
            "DecisionBook cannot safely rebuild status counts for this sparse ledger."
        )
    ctx.kv.set(COUNTS_STATE_KEY, COUNTS_DIRTY)
    records, malformed, _ = _load_ids(ctx, range(high_water, 0, -1))
    opened = sum(item["status"] == "open" for item in records)
    closed = sum(item["status"] == "closed" for item in records)
    ctx.kv.set_many(
        {
            OPEN_COUNT_KEY: opened,
            CLOSED_COUNT_KEY: closed,
            COUNTED_KEYS_KEY: stored_count,
        }
    )
    ctx.kv.set(COUNTS_STATE_KEY, COUNTS_READY)
    if malformed:
        safe_log(
            ctx,
            "DecisionBook status counts exclude unavailable records",
            level="warning",
            tags=["storage", "repair"],
            skipped=malformed,
        )
    return opened, closed


def status_counts(ctx: Context) -> tuple[int, int]:
    require_schema(ctx)
    opened = _nonnegative_integer(ctx.kv.get(OPEN_COUNT_KEY))
    closed = _nonnegative_integer(ctx.kv.get(CLOSED_COUNT_KEY))
    counted = _nonnegative_integer(ctx.kv.get(COUNTED_KEYS_KEY))
    state = ctx.kv.get(COUNTS_STATE_KEY)
    stored_count = int(ctx.kv.count(prefix=DECISION_PREFIX))
    if (
        state != COUNTS_READY
        or opened is None
        or closed is None
        or counted != stored_count
        or opened + closed > stored_count
    ):
        return _rebuild_status_counts(ctx)
    return opened, closed


def _mark_counts_dirty(ctx: Context) -> None:
    status_counts(ctx)
    ctx.kv.set(COUNTS_STATE_KEY, COUNTS_DIRTY)


def _finish_count_update(ctx: Context, *, opened: int = 0, closed: int = 0) -> None:
    """Repairable metadata never changes the primary commit result."""
    try:
        if opened:
            value = ctx.kv.increment(OPEN_COUNT_KEY, amount=opened)
            if _nonnegative_integer(value) is None:
                raise StorageStateError("Invalid open decision counter result.")
        if closed:
            value = ctx.kv.increment(CLOSED_COUNT_KEY, amount=closed)
            if _nonnegative_integer(value) is None:
                raise StorageStateError("Invalid closed decision counter result.")
        ctx.kv.set(COUNTED_KEYS_KEY, ctx.kv.count(prefix=DECISION_PREFIX))
        ctx.kv.set(COUNTS_STATE_KEY, COUNTS_READY)
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


def _repair_counts_after_commit(ctx: Context) -> None:
    try:
        _rebuild_status_counts(ctx)
    except Exception as exc:
        safe_log(
            ctx,
            "DecisionBook status counts marked for repair",
            level="warning",
            tags=["storage", "repair"],
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
) -> None:
    try:
        respond_embed(
            ctx,
            decision_embed(record),
            content=content,
            components=_record_components(record),
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
        )


def _unavailable_message(decision_id: int, status: str) -> str:
    if status == "missing":
        return f"Decision #{decision_id} was not found in this server's DecisionBook."
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


@plugin.on_ready
def ready(ctx: Context) -> None:
    try:
        require_schema(ctx)
        discover_high_water(ctx, repair=True)
        status_counts(ctx)
    except Exception as exc:
        safe_log(
            ctx,
            "DecisionBook storage readiness failed",
            level="error",
            tags=["lifecycle", "storage"],
            error_type=type(exc).__name__,
        )
        raise
    safe_log(ctx, "DecisionBook ready", tags=["lifecycle"], schema_version=SCHEMA_VERSION)


def show_add_modal(ctx: Context) -> None:
    ctx.interaction.send_modal(
        title="Record a decision",
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
                "Tags",
                "tags",
                placeholder="Optional: release, events, planning",
                required=False,
                max_length=LIMITS["tags"],
            ),
        ],
    )


def decision_add(
    ctx: Context,
    event: dict[str, Any],
    values: dict[str, Any] | None = None,
) -> None:
    """Validate, collision-check, commit, then report the authoritative result."""
    committed: dict[str, Any] | None = None
    try:
        options = values if values is not None else options_from(event)
        draft = make_record(
            1,
            title=options.get("title"),
            choice=options.get("choice"),
            reason=options.get("reason"),
            tags=options.get("tags"),
            author_id=actor_from(event),
        )
        _mark_counts_dirty(ctx)
        decision_id = allocate_decision_id(ctx)
        draft["id"] = decision_id
        decision_embed(draft)
        ctx.kv.set(decision_key(decision_id), draft)
        committed = draft
        _finish_count_update(ctx, opened=1)
        metric(ctx, "decision_created")
    except InputError as exc:
        metric(ctx, "decision_error", reason="validation")
        safe_respond_error(ctx, str(exc))
        return
    except StorageStateError as exc:
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
            "Nothing was saved.",
        )
        return
    except Exception as exc:
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
        )
        return
    if committed is not None:
        _deliver_committed(ctx, committed, operation="creation")


@plugin.on_modal_submit("decision:add")
def decision_add_modal(ctx: Context, event: dict[str, Any]) -> None:
    decision_add(ctx, event, modal_values_from(event))


def decision_view(
    ctx: Context,
    event: dict[str, Any],
    values: dict[str, Any] | None = None,
    *,
    ephemeral: bool = False,
) -> None:
    try:
        options = values if values is not None else options_from(event)
        decision_id = parse_decision_id(options.get("id"))
        loaded = load_decision(ctx, decision_id)
        if loaded.record is None:
            respond_error(ctx, _unavailable_message(decision_id, loaded.status))
            return
        metric(ctx, "decision_viewed")
        respond_embed(
            ctx,
            decision_embed(loaded.record),
            components=_record_components(loaded.record),
            ephemeral=ephemeral,
        )
    except InputError as exc:
        metric(ctx, "decision_error", reason="validation")
        safe_respond_error(ctx, str(exc))
    except StorageStateError:
        metric(ctx, "decision_error", reason="state")
        safe_respond_error(
            ctx, "DecisionBook cannot read this ledger until its schema is repaired."
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
        safe_respond_error(ctx, "DecisionBook could not load that decision. Please try again.")


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


def _list_state_key(token: str) -> str:
    if _LIST_TOKEN.fullmatch(token) is None:
        raise InputError("That list control is invalid. Run /decision list again.")
    return f"{LIST_STATE_PREFIX}{token}"


def _save_list_state(
    ctx: Context,
    *,
    query: str,
    status: str,
    limit: int,
    high_water: int,
) -> str | None:
    try:
        token = secrets.token_urlsafe(9)
        ctx.kv.set(
            _list_state_key(token),
            {
                "query": query,
                "status": status,
                "limit": limit,
                "high_water": high_water,
            },
            ttl_seconds=LIST_STATE_TTL_SECONDS,
        )
        return token
    except Exception as exc:
        safe_log(
            ctx,
            "DecisionBook pagination controls unavailable",
            level="warning",
            tags=["interaction", "storage"],
            error_type=type(exc).__name__,
        )
        return None


def _load_list_state(ctx: Context, token: str) -> dict[str, Any] | None:
    raw = ctx.kv.get(_list_state_key(token))
    if not isinstance(raw, dict):
        return None
    try:
        query = normalize_text(raw.get("query"))
        if len(query) > LIMITS["query"]:
            return None
        status = normalize_text(raw.get("status")).lower()
        if status not in {"all", "open", "closed"}:
            return None
        limit = _whole_number(raw.get("limit"), default=5, maximum=10, label="Limit")
        high_water = parse_decision_id(raw.get("high_water"))
    except InputError:
        return None
    return {"query": query, "status": status, "limit": limit, "high_water": high_water}


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
    high_water: int | None = None,
    token: str | None = None,
    update_message: bool = False,
) -> None:
    window = load_record_window(ctx, high_water=high_water)
    matches = matching_records(window.records, query=query, status=status)
    total_matches = len(matches)
    total_pages = max(1, (total_matches + limit - 1) // limit)
    if page > total_pages:
        raise InputError(f"Page {page} does not exist. This result has {total_pages} page(s).")
    offset = (page - 1) * limit
    records = matches[offset : offset + limit]

    if not records:
        if window.stored_count == 0:
            raise InputError("No decisions have been recorded yet. Start with /decision add.")
        if not window.records and window.malformed_count:
            raise InputError(
                "Decision records exist, but none are currently available because they did not "
                "pass integrity checks. No stored record was changed."
            )
        raise InputError(
            "No decisions match that search. Try a shorter query "
            "or change the status filter to All."
        )

    if token is None:
        token = _save_list_state(
            ctx,
            query=query,
            status=status,
            limit=limit,
            high_water=window.high_water,
        )
    embed = list_embed(records, query=query, status=status, offset=offset, total=total_matches)
    valid_total = sum(status_counts(ctx))
    if valid_total > len(window.records):
        embed["footer"]["text"] += (
            f" • Searched the newest {len(window.records)} of {valid_total} decisions."
        )
    respond_embed(
        ctx,
        embed,
        components=_list_components(records, token=token, page=page, pages=total_pages),
        update_message=update_message,
    )


def decision_list(
    ctx: Context,
    event: dict[str, Any],
    values: dict[str, Any] | None = None,
) -> None:
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
        page = _whole_number(options.get("page"), default=1, maximum=100, label="Page")
        _render_list_page(ctx, query=query, status=status, limit=limit, page=page)
        metric(ctx, "decision_listed")
    except InputError as exc:
        metric(ctx, "decision_error", reason="validation")
        safe_respond_error(ctx, str(exc))
    except StorageStateError:
        metric(ctx, "decision_error", reason="state")
        safe_respond_error(ctx, "DecisionBook cannot search until its storage state is repaired.")
    except Exception as exc:
        safe_log(
            ctx,
            "Decision list failed",
            level="error",
            tags=["command"],
            error_type=type(exc).__name__,
        )
        metric(ctx, "decision_error", reason="storage")
        safe_respond_error(ctx, "DecisionBook could not search the ledger. Please try again.")


def show_close_modal(ctx: Context, decision_id: int) -> None:
    ctx.interaction.send_modal(
        title=f"Close Decision #{decision_id}",
        custom_id=CLOSE_MODAL_ID,
        fields=[
            TextInput(
                "Decision ID",
                "id",
                value=str(decision_id),
                min_length=1,
                max_length=KEY_WIDTH,
            ),
            TextInput(
                "Outcome",
                "outcome",
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
        loaded = load_decision(ctx, decision_id)
        if loaded.record is None:
            respond_error(ctx, _unavailable_message(decision_id, loaded.status))
            return
        actor = actor_from(event)
        if loaded.record["status"] == "closed":
            respond_embed(
                ctx,
                decision_embed(loaded.record),
                content=f"Decision #{decision_id} was already closed; no changes were made.",
                ephemeral=True,
            )
            return
        if actor != loaded.record["author_id"]:
            respond_error(
                ctx,
                f"Only the person who recorded Decision #{decision_id} can close it. "
                "The original record remains unchanged.",
            )
            return
        show_close_modal(ctx, decision_id)
    except InputError as exc:
        safe_respond_error(ctx, str(exc))
    except StorageStateError:
        safe_respond_error(ctx, "DecisionBook cannot close decisions until storage is repaired.")
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
) -> None:
    committed: dict[str, Any] | None = None
    try:
        options = values if values is not None else options_from(event)
        decision_id = parse_decision_id(options.get("id"))
        loaded = load_decision(ctx, decision_id)
        if loaded.record is None:
            respond_error(ctx, _unavailable_message(decision_id, loaded.status))
            return
        if loaded.record["status"] == "closed":
            respond_embed(
                ctx,
                decision_embed(loaded.record),
                content=f"Decision #{decision_id} was already closed; no changes were made.",
                ephemeral=True,
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
            )
            return
        try:
            acquired = ctx.ephemeral.dedup(
                f"decisionbook:close:{decision_id}",
                ttl_seconds=15,
            )
        except Exception as exc:
            acquired = True
            safe_log(
                ctx,
                "DecisionBook close race guard unavailable",
                level="warning",
                tags=["storage"],
                error_type=type(exc).__name__,
            )
        if not acquired:
            respond_error(
                ctx,
                f"Decision #{decision_id} is already being closed. "
                "Wait a moment and view it again.",
            )
            return
        decision_embed(updated)
        _mark_counts_dirty(ctx)
        ctx.kv.set(decision_key(decision_id), updated)
        committed = updated
        _repair_counts_after_commit(ctx)
        metric(ctx, "decision_closed")
    except InputError as exc:
        metric(ctx, "decision_error", reason="validation")
        safe_respond_error(ctx, str(exc))
        return
    except StorageStateError:
        metric(ctx, "decision_error", reason="state")
        safe_respond_error(ctx, "DecisionBook cannot close decisions until storage is repaired.")
        return
    except Exception as exc:
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
        )
        return
    if committed is not None:
        _deliver_committed(ctx, committed, operation="closure")


@plugin.on_modal_submit("decision:close")
def decision_close_modal(ctx: Context, event: dict[str, Any]) -> None:
    decision_close(ctx, event, modal_values_from(event))


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
            "• Add up to five comma-separated tags, such as release, qa.\n"
            "• Lists show IDs and can be paged with buttons.\n"
            "• Records are scoped to this Discord server.\n"
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
    try:
        custom_id = event.get("custom_id")
        if not isinstance(custom_id, str):
            raise InputError("That list control is invalid. Run /decision list again.")
        payload = custom_id.removeprefix(LIST_COMPONENT_PREFIX)
        token, separator, page_value = payload.rpartition(":")
        if not separator:
            raise InputError("That list control is invalid. Run /decision list again.")
        state = _load_list_state(ctx, token)
        if state is None:
            raise InputError("That list has expired. Run /decision list to refresh it.")
        page = _whole_number(page_value, default=1, maximum=100, label="Page")
        _render_list_page(
            ctx,
            query=state["query"],
            status=state["status"],
            limit=state["limit"],
            page=page,
            high_water=state["high_water"],
            token=token,
            update_message=True,
        )
        metric(ctx, "decision_listed")
    except InputError as exc:
        safe_respond_error(ctx, str(exc))
    except Exception as exc:
        safe_log(
            ctx,
            "DecisionBook list control failed",
            level="error",
            tags=["interaction"],
            error_type=type(exc).__name__,
        )
        safe_respond_error(
            ctx, "DecisionBook could not update that list. Run /decision list again."
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


@plugin.on_dashboard("get_total_decisions")
def get_total_decisions(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    del params
    opened, closed = status_counts(ctx)
    return {"value": opened + closed}


@plugin.on_dashboard("get_open_decisions")
def get_open_decisions(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    del params
    opened, _ = status_counts(ctx)
    return {"value": opened}


@plugin.on_dashboard("get_closed_decisions")
def get_closed_decisions(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    del params
    _, closed = status_counts(ctx)
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
        page = _dashboard_integer(params.get("page"), default=1, maximum=400)
        offset = (page - 1) * page_size

    opened, closed = status_counts(ctx)
    total = opened + closed
    if offset >= total:
        return {"rows": [], "total": total}

    high_water, _ = discover_high_water(ctx, repair=True)
    needed = min(total, offset + page_size)
    window = load_record_window(
        ctx,
        record_limit=max(1, needed),
        high_water=high_water,
        candidate_limit=MAX_LEDGER_SCAN,
    )
    page_records = window.records[offset : offset + page_size]
    rows = []
    for item in page_records:
        closure = item.get("closure") or {}
        tag_text = ", ".join(item["tags"])
        summary = display_text(item["choice"], 120)
        if tag_text:
            summary += f" · {display_text(tag_text, 80)}"
        rows.append(
            {
                "id": item["id"],
                "title": display_text(item["title"], 80),
                "status": item["status"].title(),
                "summary": summary,
                "author": item["author_id"],
                "recorded": _dashboard_time(item["created_at"]),
                "closed": _dashboard_time(closure.get("closed_at")),
            }
        )
    return {"rows": rows, "total": total}
