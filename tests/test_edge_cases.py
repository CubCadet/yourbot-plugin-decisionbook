from __future__ import annotations

import pytest
from conftest import AUTHOR_ID, component, interaction, subcommand

import decisionbook
from core import InputError, decision_key, make_record
from decisionbook import (
    CLOSED_COUNT_KEY,
    COUNTED_KEYS_KEY,
    COUNTER_KEY,
    COUNTS_READY,
    COUNTS_STATE_KEY,
    MAX_DECISION_ID,
    MAX_LEDGER_SCAN,
    OPEN_COUNT_KEY,
    SCHEMA_KEY,
    StorageStateError,
    _dashboard_integer,
    _dashboard_time,
    _decision_id_from_key,
    _finish_count_update,
    _load_list_state,
    _options,
    _repair_counts_after_commit,
    _save_list_state,
    allocate_decision_id,
    decision_close,
    decision_command,
    decision_list,
    decision_list_component,
    decision_view,
    discover_high_water,
    install,
    modal_values_from,
    safe_respond_error,
    status_counts,
    subcommand_from,
)


def seed(ctx, decision_id: int, *, closed: bool = False, tags: str = "tag"):
    record = make_record(
        decision_id,
        title=f"Decision {decision_id}",
        choice="Choice",
        reason="Reason",
        tags=tags,
        author_id=AUTHOR_ID,
        created_at="2026-07-11T00:00:00+00:00",
    )
    if closed:
        record["status"] = "closed"
        record["closure"] = {
            "outcome": "Done",
            "closed_by": AUTHOR_ID,
            "closed_at": "2026-07-12T00:00:00+00:00",
        }
    ctx.kv.set(decision_key(decision_id), record)
    return record


def test_event_parsers_reject_malformed_shapes():
    assert _options(None) == {}
    assert _options([None, {"name": 7}, {"name": "ok", "value": 1}]) == {"ok": 1}
    assert modal_values_from({"modal_values": []}) == {}
    with pytest.raises(InputError):
        subcommand_from({})
    with pytest.raises(InputError):
        subcommand_from({"options": [{"name": "delete", "type": 1}]})


def test_safe_error_delivery_absorbs_transport_and_log_failures(ctx, monkeypatch):
    monkeypatch.setattr(
        ctx.interaction,
        "respond",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(
        ctx,
        "log",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    safe_respond_error(ctx, "Safe")


@pytest.mark.parametrize(
    "key",
    [None, "other:1", "decision:not-a-number", "decision:-1"],
)
def test_decision_key_parser_rejects_invalid_storage_keys(key):
    assert _decision_id_from_key(key) is None


def test_high_water_can_be_read_without_repair(ctx):
    seed(ctx, 3)
    ctx.kv.set(COUNTER_KEY, 1)
    assert discover_high_water(ctx, repair=False) == (3, 1)
    assert ctx.kv.get(COUNTER_KEY) == 1


def test_high_water_and_allocator_reject_out_of_range_values(ctx):
    ctx.kv.set(COUNTER_KEY, MAX_DECISION_ID + 1)
    with pytest.raises(StorageStateError):
        discover_high_water(ctx)
    ctx.kv.set(COUNTER_KEY, MAX_DECISION_ID)
    with pytest.raises(StorageStateError):
        allocate_decision_id(ctx)


def test_allocator_rejects_invalid_increment_result(ctx, monkeypatch):
    monkeypatch.setattr(ctx.kv, "increment", lambda *args, **kwargs: True)
    with pytest.raises(StorageStateError):
        allocate_decision_id(ctx)


def test_allocator_collision_probe_advances_without_overwrite(ctx, monkeypatch):
    seed(ctx, 1)
    ctx.kv.set(COUNTER_KEY, 0)
    monkeypatch.setattr(decisionbook, "discover_high_water", lambda *args, **kwargs: (0, 1))
    assert allocate_decision_id(ctx) == 2
    assert ctx.kv.get(decision_key(1))["id"] == 1


def test_allocator_repairs_sparse_high_water_before_incrementing(ctx):
    seed(ctx, 1001)
    ctx.kv.set(COUNTER_KEY, 0)
    assert allocate_decision_id(ctx) == 1002
    assert ctx.kv.get(COUNTER_KEY) == 1002


def test_install_lifecycle_repairs_existing_ledger(ctx):
    seed(ctx, 1)
    install(ctx)
    assert ctx.kv.get(SCHEMA_KEY) == 1
    assert status_counts(ctx) == (1, 0)


def test_count_rebuild_refuses_impossibly_sparse_ledger(ctx):
    ctx.kv.set(COUNTER_KEY, MAX_LEDGER_SCAN + 1)
    with pytest.raises(StorageStateError):
        status_counts(ctx)


def test_count_update_closed_branch_and_invalid_counter_result(ctx, monkeypatch):
    ctx.kv.set(SCHEMA_KEY, 1)
    ctx.kv.set(OPEN_COUNT_KEY, 1)
    ctx.kv.set(CLOSED_COUNT_KEY, 0)
    _finish_count_update(ctx, opened=-1, closed=1)
    assert ctx.kv.get(OPEN_COUNT_KEY) == 0
    assert ctx.kv.get(CLOSED_COUNT_KEY) == 1

    monkeypatch.setattr(ctx.kv, "increment", lambda *args, **kwargs: "invalid")
    _finish_count_update(ctx, opened=1)
    assert ctx.kv.get(COUNTS_STATE_KEY) != "ready"


def test_ready_status_counts_use_valid_cached_metadata(ctx, monkeypatch):
    seed(ctx, 1)
    seed(ctx, 2)
    seed(ctx, 3, closed=True)
    ctx.kv.set(OPEN_COUNT_KEY, 2)
    ctx.kv.set(CLOSED_COUNT_KEY, 1)
    ctx.kv.set(COUNTED_KEYS_KEY, 3)
    ctx.kv.set(COUNTS_STATE_KEY, COUNTS_READY)
    monkeypatch.setattr(
        decisionbook,
        "_rebuild_status_counts",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("valid cached counts must not rebuild")
        ),
    )
    assert status_counts(ctx) == (2, 1)


def test_post_commit_count_repair_failure_is_isolated(ctx, monkeypatch):
    monkeypatch.setattr(
        decisionbook,
        "_rebuild_status_counts",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    _repair_counts_after_commit(ctx)


def test_view_and_list_validation_and_schema_paths(ctx):
    decision_view(ctx, interaction(id=True))
    assert "positive whole number" in ctx.interaction.responses[-1]["content"]
    ctx.kv.set(SCHEMA_KEY, 2)
    decision_view(ctx, interaction(id=1))
    assert "schema" in ctx.interaction.responses[-1]["content"]
    decision_list(ctx, interaction(status="invalid"))
    assert "Status must be" in ctx.interaction.responses[-1]["content"]


def test_list_number_parsing_and_unavailable_pagination_state(ctx, monkeypatch):
    seed(ctx, 1)
    decision_list(ctx, interaction(limit=True))
    assert "whole number" in ctx.interaction.responses[-1]["content"]
    decision_list(ctx, interaction(limit="not-a-number"))
    assert "whole number" in ctx.interaction.responses[-1]["content"]
    decision_list(ctx, interaction(page=2))
    assert "does not exist" in ctx.interaction.responses[-1]["content"]

    original = ctx.kv.set

    def fail_ui(key, value, **kwargs):
        if key.startswith("ui:list:"):
            raise RuntimeError("offline")
        return original(key, value, **kwargs)

    monkeypatch.setattr(ctx.kv, "set", fail_ui)
    decision_list(ctx, interaction())
    assert ctx.interaction.responses[-1]["components"]


def test_corrupt_list_state_is_rejected(ctx):
    cases = [
        {"query": "x" * 81, "status": "all", "limit": 5, "high_water": 1},
        {"query": "", "status": "bad", "limit": 5, "high_water": 1},
        {"query": "", "status": "all", "limit": 99, "high_water": 1},
        {"query": "", "status": "all", "limit": 5, "high_water": "bad"},
    ]
    for index, value in enumerate(cases):
        token = f"validtok{index}"
        ctx.kv.set(f"ui:list:{token}", value)
        assert _load_list_state(ctx, token) is None
    assert _save_list_state(ctx, query="", status="all", limit=5, high_water=1)


def test_prepare_close_missing_closed_and_schema_paths(ctx):
    decision_command(ctx, subcommand("close", id=99))
    assert "not found" in ctx.interaction.responses[-1]["content"]

    seed(ctx, 1, closed=True)
    decision_command(ctx, subcommand("close", id=1))
    assert "already closed" in ctx.interaction.responses[-1]["content"]

    ctx.kv.set(SCHEMA_KEY, 2)
    decision_command(ctx, subcommand("close", id=1))
    assert "storage is repaired" in ctx.interaction.responses[-1]["content"]


def test_close_missing_validation_schema_and_race_guard_fallback(ctx, monkeypatch):
    decision_close(ctx, interaction(id=99, outcome="Done"))
    assert "not found" in ctx.interaction.responses[-1]["content"]
    decision_close(ctx, interaction(id=True, outcome="Done"))
    assert "positive whole number" in ctx.interaction.responses[-1]["content"]

    seed(ctx, 1)
    ctx.kv.set(SCHEMA_KEY, 2)
    decision_close(ctx, interaction(id=1, outcome="Done"))
    assert "storage is repaired" in ctx.interaction.responses[-1]["content"]

    ctx.kv.set(SCHEMA_KEY, 1)
    monkeypatch.setattr(
        ctx.ephemeral,
        "dedup",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    decision_close(ctx, interaction(id=1, outcome="Done"))
    assert ctx.kv.get(decision_key(1))["status"] == "closed"


def test_root_dispatch_invalid_and_remaining_action_branches(ctx):
    decision_command(ctx, interaction())
    assert "Choose one" in ctx.interaction.responses[-1]["content"]
    decision_command(ctx, {"options": [{"name": "delete", "type": 1}]})
    assert "unavailable" in ctx.interaction.responses[-1]["content"]

    seed(ctx, 1)
    decision_command(ctx, subcommand("list"))
    assert ctx.interaction.responses[-1]["embeds"]
    decision_command(ctx, subcommand("view", id=1))
    assert ctx.interaction.responses[-1]["embeds"]


def test_list_component_malformed_and_storage_failure_paths(ctx, monkeypatch):
    decision_list_component(ctx, {"custom_id": None})
    assert "invalid" in ctx.interaction.responses[-1]["content"]
    decision_list_component(ctx, component("decision:page:noseparator"))
    assert "invalid" in ctx.interaction.responses[-1]["content"]

    monkeypatch.setattr(
        ctx.kv,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    decision_list_component(ctx, component("decision:page:validtoken:1"))
    assert "could not update" in ctx.interaction.responses[-1]["content"]


def test_dashboard_parameter_and_time_fallbacks():
    assert _dashboard_integer(True, default=7, maximum=10) == 7
    assert _dashboard_integer(float("inf"), default=7, maximum=10) == 7
    assert _dashboard_integer(-5, default=7, minimum=0, maximum=10) == 0
    assert _dashboard_time(None) == "—"
    assert _dashboard_time("not-a-date") == "Unavailable"
