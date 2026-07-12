from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, Event, Lock, current_thread

import pytest
from conftest import AUTHOR_ID, interaction
from yourbot_sdk import KvQuotaError
from yourbot_sdk.testing import make_event

import decisionbook
from core import SCHEMA_VERSION, decision_key, make_record
from decisionbook import (
    COUNTER_KEY,
    COUNTS_DIRTY,
    COUNTS_STATE_KEY,
    DECISION_PREFIX,
    LIST_STATE_PREFIX,
    SCHEMA_KEY,
    StorageStateError,
    decision_add,
    decision_close,
    decision_list,
    decision_view,
    discover_high_water,
    get_recent_decisions,
    status_counts,
)

CHANNEL_ID = "1"
OTHER_CHANNEL_ID = "2"


def add_event(*, title: str, channel_id: str = CHANNEL_ID):
    return make_event(
        "interaction_create",
        command_name="decision",
        user_id=AUTHOR_ID,
        channel_id=channel_id,
        options=[
            {"name": "title", "value": title},
            {"name": "choice", "value": "Ship it"},
            {"name": "reason", "value": "Checks passed"},
            {"name": "tags", "value": "release"},
        ],
    )


def command_event(*, channel_id: str = CHANNEL_ID, **options):
    return make_event(
        "interaction_create",
        command_name="decision",
        user_id=AUTHOR_ID,
        channel_id=channel_id,
        options=[{"name": name, "value": value} for name, value in options.items()],
    )


def seed(ctx, decision_id: int, *, channel_id: str = CHANNEL_ID):
    ctx.kv.set(SCHEMA_KEY, 2)
    record = make_record(
        decision_id,
        title=f"Decision {decision_id}",
        choice="Choice",
        reason="Reason",
        tags="tag",
        author_id=AUTHOR_ID,
        channel_id=channel_id,
        created_at="2026-07-11T00:00:00+00:00",
    )
    ctx.kv.set(decision_key(decision_id), record)
    return record


def test_atomic_counter_repair_cannot_reuse_an_id(ctx, monkeypatch):
    original = seed(ctx, 1)
    ctx.kv.delete(COUNTER_KEY)
    real_get = ctx.kv.get
    barrier = Barrier(2)
    guard = Lock()
    synchronized_reads = 0

    def synchronize_missing_counter(key):
        nonlocal synchronized_reads
        value = real_get(key)
        if key != COUNTER_KEY:
            return value
        with guard:
            should_wait = synchronized_reads < 2
            synchronized_reads += 1
        if should_wait:
            assert value is None
            barrier.wait(timeout=5)
        return value

    monkeypatch.setattr(ctx.kv, "get", synchronize_missing_counter)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(decision_add, ctx, add_event(title=title))
            for title in ("Concurrent A", "Concurrent B")
        ]
        for future in futures:
            future.result(timeout=10)

    stored = ctx.kv.list_values(prefix=DECISION_PREFIX, limit=10)
    assert ctx.kv.get(decision_key(1)) == original
    new_records = [item for item in stored.values() if item["title"].startswith("Concurrent")]
    assert len(new_records) == 2
    assert len({item["id"] for item in new_records}) == 2
    assert all(item["id"] > 1 for item in new_records)


def test_radix_inventory_repairs_a_stale_counter_with_a_distant_hidden_key(ctx):
    template = seed(ctx, 1)
    for decision_id in range(2, 1_001):
        record = deepcopy(template)
        record["id"] = decision_id
        record["title"] = f"Decision {decision_id}"
        ctx.kv.set(decision_key(decision_id), record)
    distant = deepcopy(template)
    distant["id"] = 20_000
    distant["title"] = "Distant decision"
    ctx.kv.set(decision_key(20_000), distant)
    ctx.kv.set(COUNTER_KEY, 1_001)

    assert discover_high_water(ctx) == (20_000, 1_001)
    assert ctx.kv.get(COUNTER_KEY) == 20_000
    assert ctx.kv.get(decision_key(20_000))["title"] == "Distant decision"
    assert status_counts(ctx) == (1_001, 0)


def test_inventory_fails_closed_when_a_capped_listing_is_incomplete(ctx, monkeypatch):
    seed(ctx, 1)
    monkeypatch.setattr(ctx.kv, "list", lambda prefix="", limit=100: [])

    with pytest.raises(decisionbook.StorageBusyError, match="complete key inventory"):
        decisionbook.inventory_decision_keys(ctx, expected_total=1)


def test_inventory_rejects_an_expected_total_mismatch(ctx):
    seed(ctx, 1)
    with pytest.raises(decisionbook.StorageBusyError, match="changed while it was read"):
        decisionbook.inventory_decision_keys(ctx, expected_total=0)


def test_inventory_accounts_for_many_suffix_keys_beneath_one_canonical_key(ctx):
    seed(ctx, 1)
    canonical = decision_key(1)
    for index in range(1_000):
        ctx.kv.set(f"{canonical}:invalid:{index}", {"unexpected": True})

    inventory = decisionbook.inventory_decision_keys(ctx, expected_total=1_001)
    assert inventory.ids == [1]
    assert inventory.malformed_keys == 1_000
    assert inventory.total_keys == 1_001


def test_stale_rebuild_cannot_publish_over_two_concurrent_closes(ctx, monkeypatch):
    for decision_id in range(1, 4):
        seed(ctx, decision_id)
    ctx.kv.set(COUNTER_KEY, 3)
    assert status_counts(ctx) == (3, 0)
    ctx.kv.set(COUNTS_STATE_KEY, COUNTS_DIRTY)

    real_load_ids = decisionbook._load_ids
    stale_scan_loaded = Event()
    release_stale_scan = Event()
    rebuild_thread_name = "stale-count-rebuild"

    def pause_stale_scan(context, ids):
        loaded = real_load_ids(context, ids)
        if current_thread().name.startswith(rebuild_thread_name):
            stale_scan_loaded.set()
            assert release_stale_scan.wait(timeout=10)
        return loaded

    monkeypatch.setattr(decisionbook, "_load_ids", pause_stale_scan)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix=rebuild_thread_name) as rebuild_pool:
        stale_future = rebuild_pool.submit(status_counts, ctx)
        assert stale_scan_loaded.wait(timeout=5)

        with ThreadPoolExecutor(max_workers=2) as close_pool:
            close_futures = [
                close_pool.submit(
                    decision_close,
                    ctx,
                    command_event(id=decision_id, outcome=f"Outcome {decision_id}"),
                )
                for decision_id in (1, 2)
            ]
            for future in close_futures:
                future.result(timeout=10)

        release_stale_scan.set()
        with pytest.raises(StorageStateError, match="changed while status counts"):
            stale_future.result(timeout=10)

    assert status_counts(ctx) == (1, 2)
    assert ctx.kv.get(COUNTS_STATE_KEY) == "ready"


def test_active_writer_short_circuits_count_repair_before_inventory(ctx, monkeypatch):
    seed(ctx, 1)
    ctx.kv.set(f"{decisionbook.MUTATION_PREFIX}active", True)
    monkeypatch.setattr(
        decisionbook,
        "inventory_decision_keys",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("inventory must not run while a writer marker is active")
        ),
    )

    with pytest.raises(decisionbook.StorageBusyError, match="write is still in progress"):
        status_counts(ctx)


def test_channel_visibility_is_private_but_dashboard_remains_server_wide(ctx):
    decision_add(ctx, add_event(title="Private staff choice"))
    stored = ctx.kv.get(decision_key(1))
    assert stored["schema_version"] == SCHEMA_VERSION
    assert stored["channel_id"] == CHANNEL_ID

    decision_view(ctx, command_event(channel_id=OTHER_CHANNEL_ID, id=1))
    assert "not found in this channel" in ctx.interaction.followups[-1]["content"]

    decision_list(ctx, command_event(channel_id=OTHER_CHANNEL_ID))
    assert "recorded in this channel" in ctx.interaction.followups[-1]["content"]

    decision_close(
        ctx,
        command_event(channel_id=OTHER_CHANNEL_ID, id=1, outcome="Should not close"),
    )
    assert ctx.kv.get(decision_key(1))["status"] == "open"

    decision_view(ctx, command_event(id=1))
    assert ctx.interaction.followups[-1]["embeds"][0]["title"].startswith("Decision #1")
    dashboard = get_recent_decisions(ctx, {"page": 1, "page_size": 25})
    assert dashboard["total"] == 1
    assert "Private staff choice" in dashboard["rows"][0]["summary"]


def test_invalid_event_channel_is_rejected_and_one_page_list_uses_no_state(ctx):
    decision_add(ctx, add_event(title="Invalid", channel_id="not-a-channel"))
    assert ctx.kv.count(prefix=DECISION_PREFIX) == 0
    assert "valid channel identity" in ctx.interaction.responses[-1]["content"]

    decision_add(ctx, add_event(title="Only result"))
    decision_list(ctx, interaction())
    assert ctx.kv.count(prefix=LIST_STATE_PREFIX) == 0
    assert ctx.interaction.followups[-1]["components"]


def test_quota_failure_before_commit_has_specific_safe_guidance(ctx, monkeypatch):
    real_set = ctx.kv.set

    def fail_mutation_marker(key, value, **kwargs):
        if key.startswith(decisionbook.MUTATION_PREFIX):
            raise KvQuotaError("full")
        return real_set(key, value, **kwargs)

    monkeypatch.setattr(ctx.kv, "set", fail_mutation_marker)
    decision_add(ctx, add_event(title="Cannot fit"))
    assert ctx.kv.count(prefix=DECISION_PREFIX) == 0
    response = ctx.interaction.responses[-1]
    assert response["ephemeral"] is True
    assert "storage quota is full" in response["content"]
    assert "Nothing was saved" in response["content"]


def test_safe_capacity_reserve_blocks_add_before_counter_or_marker_drift(ctx, monkeypatch):
    real_count = ctx.kv.count

    def near_capacity(prefix=""):
        if prefix == "":
            return decisionbook.KV_KEY_QUOTA - decisionbook.KV_WRITE_RESERVE
        return real_count(prefix=prefix)

    monkeypatch.setattr(ctx.kv, "count", near_capacity)
    decision_add(ctx, add_event(title="Must not allocate"))

    assert ctx.kv.get(COUNTER_KEY) is None
    assert real_count(prefix=DECISION_PREFIX) == 0
    assert real_count(prefix=decisionbook.MUTATION_PREFIX) == 0
    assert "safe storage limit" in ctx.interaction.responses[-1]["content"]
