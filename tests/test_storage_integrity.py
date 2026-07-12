from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from conftest import AUTHOR_ID, CHANNEL_ID, interaction, invoke

import decisionbook
from core import decision_key, make_record
from decisionbook import (
    CLOSED_COUNT_KEY,
    COUNTED_REVISION_KEY,
    COUNTER_KEY,
    COUNTS_DIRTY,
    COUNTS_STATE_KEY,
    MALFORMED_COUNT_KEY,
    OPEN_COUNT_KEY,
    SCHEMA_KEY,
    StorageStateError,
    decision_add,
    decision_close,
    decision_list,
    decision_view,
    get_storage_health,
    install,
    status_counts,
)


def add_event(**overrides):
    values = {
        "title": "Release timing",
        "choice": "Ship Friday",
        "reason": "QA completed",
        "tags": "release, qa",
    }
    values.update(overrides)
    return interaction(**values)


def seed(ctx, decision_id: int):
    ctx.kv.set(SCHEMA_KEY, 2)
    item = make_record(
        decision_id,
        title=f"Decision {decision_id}",
        choice="Choice",
        reason="Reason",
        tags="tag",
        author_id=AUTHOR_ID,
        channel_id=CHANNEL_ID,
        created_at="2026-07-11T00:00:00+00:00",
    )
    ctx.kv.set(decision_key(decision_id), item)
    return item


@pytest.mark.parametrize("marker", [True, "2", 0, 1, 3, {}, []])
def test_install_rejects_every_noncanonical_schema_marker(ctx, marker):
    ctx.kv.set(SCHEMA_KEY, marker)
    with pytest.raises(StorageStateError):
        install(ctx)


def test_install_initializes_and_repairs_missing_metadata(ctx):
    seed(ctx, 1)
    install(ctx)
    assert ctx.kv.get(SCHEMA_KEY) == 2
    assert ctx.kv.get(COUNTER_KEY) == 1
    assert status_counts(ctx) == (1, 0)


def test_missing_schema_marker_with_existing_records_fails_closed(ctx):
    item = make_record(
        1,
        title="Unlabeled record",
        choice="Unknown schema provenance",
        reason="The marker is missing",
        tags="integrity",
        author_id=AUTHOR_ID,
        channel_id=CHANNEL_ID,
        created_at="2026-07-11T00:00:00+00:00",
    )
    ctx.kv.set(decision_key(1), item)

    with pytest.raises(StorageStateError, match="without a schema marker"):
        install(ctx)
    assert ctx.kv.get(SCHEMA_KEY) is None
    assert ctx.kv.get(decision_key(1)) == item


def test_install_rejects_a_corrupt_counter_without_rewriting_it(ctx):
    seed(ctx, 1)
    ctx.kv.set(COUNTER_KEY, "broken")
    with pytest.raises(StorageStateError):
        install(ctx)
    assert ctx.kv.get(COUNTER_KEY) == "broken"


def test_increment_failure_cannot_create_partial_record(ctx, monkeypatch):
    monkeypatch.setattr(
        ctx.kv,
        "increment",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    delivery = invoke(ctx, decision_add, add_event())
    assert ctx.kv.count(prefix="decision:") == 0
    assert "Nothing was saved" in delivery["content"]


def test_list_failure_cannot_allocate_or_overwrite(ctx, monkeypatch):
    original = seed(ctx, 1)
    monkeypatch.setattr(
        ctx.kv,
        "list",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    decision_add(ctx, add_event(title="Must not replace"))
    assert ctx.kv.get(decision_key(1)) == original
    assert ctx.kv.get(decision_key(2)) is None


def test_get_failure_returns_safe_view_error(ctx, monkeypatch):
    monkeypatch.setattr(
        ctx.kv,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    delivery = invoke(ctx, decision_view, interaction(id=1))
    assert "could not load" in delivery["content"]


def test_get_many_failure_returns_safe_search_error(ctx, monkeypatch):
    seed(ctx, 1)
    monkeypatch.setattr(
        ctx.kv,
        "get_many",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    delivery = invoke(ctx, decision_list, interaction())
    assert "could not search" in delivery["content"]


def test_close_storage_failure_preserves_authoritative_open_record(ctx, monkeypatch):
    decision_add(ctx, add_event())
    original = ctx.kv.get(decision_key(1))
    real_set = ctx.kv.set

    def fail(key, value, **kwargs):
        if key == decision_key(1) and value.get("status") == "closed":
            raise RuntimeError("offline")
        return real_set(key, value, **kwargs)

    monkeypatch.setattr(ctx.kv, "set", fail)
    delivery = invoke(ctx, decision_close, interaction(id=1, outcome="Done"))
    assert ctx.kv.get(decision_key(1)) == original
    assert "existing record remains authoritative" in delivery["content"]


def test_secondary_count_failure_does_not_negate_primary_commit(ctx, monkeypatch):
    original_set = ctx.kv.set

    def fail_counted_revision(key, value, **kwargs):
        if key == COUNTED_REVISION_KEY:
            raise RuntimeError("counter unavailable")
        return original_set(key, value, **kwargs)

    monkeypatch.setattr(ctx.kv, "set", fail_counted_revision)
    decision_add(ctx, add_event())
    assert ctx.kv.get(decision_key(1))["id"] == 1
    assert ctx.kv.get(COUNTS_STATE_KEY) == COUNTS_DIRTY
    monkeypatch.setattr(ctx.kv, "set", original_set)
    assert status_counts(ctx) == (1, 0)


def test_status_counts_repair_partial_metadata(ctx):
    seed(ctx, 1)
    ctx.kv.set(OPEN_COUNT_KEY, 999)
    ctx.kv.set(CLOSED_COUNT_KEY, -1)
    ctx.kv.set(COUNTS_STATE_KEY, "ready")
    assert status_counts(ctx) == (1, 0)


def test_status_counts_rebuilds_plausible_but_too_low_cached_totals(ctx):
    seed(ctx, 1)
    seed(ctx, 2)
    assert status_counts(ctx) == (2, 0)

    ctx.kv.set(OPEN_COUNT_KEY, 1)
    ctx.kv.set(CLOSED_COUNT_KEY, 0)
    ctx.kv.set(MALFORMED_COUNT_KEY, 0)
    ctx.kv.set(COUNTS_STATE_KEY, "ready")

    assert status_counts(ctx) == (2, 0)


def test_noncanonical_decision_prefix_key_is_counted_as_unavailable(ctx):
    ctx.kv.set(SCHEMA_KEY, 2)
    ctx.kv.set("decision:not-a-canonical-id", {"unexpected": True})

    assert status_counts(ctx) == (0, 0)
    assert ctx.kv.get(MALFORMED_COUNT_KEY) == 1
    assert ctx.kv.get(COUNTS_STATE_KEY) == "ready"
    health = get_storage_health(ctx, {})
    assert health["level"] == "warn"
    assert "1 stored record(s) are unavailable" in health["message"]


def test_sparse_keys_remain_visible_and_collision_safe(ctx):
    seed(ctx, 1)
    seed(ctx, 10)
    ctx.kv.set(COUNTER_KEY, 1)
    decision_add(ctx, add_event(title="Newest"))
    assert ctx.kv.get(decision_key(10))["title"] == "Decision 10"
    assert ctx.kv.get(decision_key(11))["title"] == "Newest"
    delivery = invoke(ctx, decision_list, interaction())
    assert "#11" in delivery["embeds"][0]["description"]


def test_runtime_never_depends_on_list_values_cap_or_order(ctx, monkeypatch):
    for decision_id in range(1, 102):
        seed(ctx, decision_id)
    monkeypatch.setattr(
        ctx.kv,
        "list_values",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    delivery = invoke(ctx, decision_list, interaction(limit=10))
    assert "#101" in delivery["embeds"][0]["description"]


def test_concurrent_adds_receive_unique_ids(ctx):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda index: decision_add(ctx, add_event(title=f"Item {index}")),
                range(20),
            )
        )
    ids = [value["id"] for value in ctx.kv.list_values(prefix="decision:", limit=100).values()]
    assert sorted(ids) == list(range(1, 21))
    assert len(set(ids)) == 20


def test_simultaneous_close_guard_allows_one_first_outcome(ctx, monkeypatch):
    decision_add(ctx, add_event())
    original_load = decisionbook.load_decision
    barrier = Barrier(2)

    def synchronized_load(*args, **kwargs):
        loaded = original_load(*args, **kwargs)
        if loaded.record is not None and loaded.record["status"] == "open":
            barrier.wait(timeout=5)
        return loaded

    monkeypatch.setattr(decisionbook, "load_decision", synchronized_load)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(decision_close, ctx, interaction(id=1, outcome=outcome))
            for outcome in ("First", "Second")
        ]
        for future in futures:
            future.result(timeout=10)
    stored = ctx.kv.get(decision_key(1))
    assert stored["status"] == "closed"
    assert stored["closure"]["outcome"] in {"First", "Second"}
    assert status_counts(ctx) == (0, 1)
    deliveries = [*ctx.interaction.responses, *ctx.interaction.followups]
    contents = [item["content"] for item in deliveries if item["content"]]
    assert any("already being closed" in content for content in contents)
