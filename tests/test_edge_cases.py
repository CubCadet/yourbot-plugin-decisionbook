from __future__ import annotations

import pytest
from conftest import AUTHOR_ID, CHANNEL_ID, component, interaction, invoke, modal, subcommand

import decisionbook
from core import InputError, decision_key, make_record
from decisionbook import (
    CLOSED_COUNT_KEY,
    COUNTED_KEYS_KEY,
    COUNTED_REVISION_KEY,
    COUNTER_KEY,
    COUNTS_DIRTY,
    COUNTS_READY,
    COUNTS_STATE_KEY,
    LEDGER_REVISION_KEY,
    MALFORMED_COUNT_KEY,
    MAX_DECISION_ID,
    OPEN_COUNT_KEY,
    SCHEMA_KEY,
    StorageStateError,
    _dashboard_integer,
    _dashboard_time,
    _decision_id_from_key,
    _finish_count_update,
    _load_list_state,
    _options,
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
    ctx.kv.set(SCHEMA_KEY, 2)
    record = make_record(
        decision_id,
        title=f"Decision {decision_id}",
        choice="Choice",
        reason="Reason",
        tags=tags,
        author_id=AUTHOR_ID,
        channel_id=CHANNEL_ID,
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
    assert ctx.kv.get(SCHEMA_KEY) == 2
    assert status_counts(ctx) == (1, 0)


def test_count_rebuild_handles_a_high_counter_without_scanning_empty_id_gaps(ctx):
    ctx.kv.set(COUNTER_KEY, 20_001)
    assert status_counts(ctx) == (0, 0)
    assert ctx.kv.get(COUNTER_KEY) == 20_001


def test_count_update_closed_branch_and_invalid_counter_result(ctx, monkeypatch):
    ctx.kv.set(SCHEMA_KEY, 2)
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
    ctx.kv.set(MALFORMED_COUNT_KEY, 0)
    ctx.kv.set(LEDGER_REVISION_KEY, 0)
    ctx.kv.set(COUNTED_REVISION_KEY, 0)
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
    original_set = ctx.kv.set

    def fail_counted_revision(key, value, **kwargs):
        if key == COUNTED_REVISION_KEY:
            raise RuntimeError("offline")
        return original_set(key, value, **kwargs)

    monkeypatch.setattr(ctx.kv, "set", fail_counted_revision)
    _finish_count_update(ctx, opened=1)
    assert ctx.kv.get(OPEN_COUNT_KEY) == 1
    assert ctx.kv.get(COUNTS_STATE_KEY) == COUNTS_DIRTY


def test_view_and_list_validation_and_schema_paths(ctx):
    delivery = invoke(ctx, decision_view, interaction(id=True))
    assert "positive whole number" in delivery["content"]
    ctx.kv.set(SCHEMA_KEY, 3)
    delivery = invoke(ctx, decision_view, interaction(id=1))
    assert "schema" in delivery["content"]
    delivery = invoke(ctx, decision_list, interaction(status="invalid"))
    assert "Status must be" in delivery["content"]


def test_list_number_parsing_and_unavailable_pagination_state(ctx, monkeypatch):
    seed(ctx, 1)
    delivery = invoke(ctx, decision_list, interaction(limit=True))
    assert "whole number" in delivery["content"]
    delivery = invoke(ctx, decision_list, interaction(limit="not-a-number"))
    assert "whole number" in delivery["content"]
    delivery = invoke(ctx, decision_list, interaction(page=2))
    assert "does not exist" in delivery["content"]

    original = ctx.kv.set

    def fail_ui(key, value, **kwargs):
        if key.startswith("ui:list:"):
            raise RuntimeError("offline")
        return original(key, value, **kwargs)

    monkeypatch.setattr(ctx.kv, "set", fail_ui)
    for decision_id in range(2, 7):
        seed(ctx, decision_id)
    delivery = invoke(ctx, decision_list, interaction())
    assert delivery["components"]
    rows = [row.to_dict() for row in delivery["components"]]
    labels = [item["label"] for row in rows for item in row["components"]]
    assert "Next" not in labels


def test_corrupt_list_state_is_rejected(ctx):
    token = "validtoken"
    base = {
        "token": token,
        "query": "",
        "status": "all",
        "limit": 5,
        "match_ids": [2, 1],
        "actor_id": AUTHOR_ID,
        "channel_id": CHANNEL_ID,
    }
    cases = [
        {**base, "query": "x" * 81},
        {**base, "status": "bad"},
        {**base, "limit": 99},
        {**base, "match_ids": [1, 2]},
        {**base, "match_ids": [2, 2]},
        {**base, "actor_id": "2"},
        {**base, "channel_id": "2"},
        {**base, "token": "othertoken"},
    ]
    for value in cases:
        ctx.kv.set(f"ui:list:{AUTHOR_ID}:{CHANNEL_ID}", value)
        assert (
            _load_list_state(
                ctx,
                token,
                actor_id=AUTHOR_ID,
                channel_id=CHANNEL_ID,
            )
            is None
        )
    saved_token = _save_list_state(
        ctx,
        query="",
        status="all",
        limit=5,
        match_ids=[2, 1],
        actor_id=AUTHOR_ID,
        channel_id=CHANNEL_ID,
    )
    assert saved_token is not None
    assert _load_list_state(
        ctx,
        saved_token,
        actor_id=AUTHOR_ID,
        channel_id=CHANNEL_ID,
    ) == {
        "query": "",
        "status": "all",
        "limit": 5,
        "match_ids": [2, 1],
        "actor_id": AUTHOR_ID,
        "channel_id": CHANNEL_ID,
    }


def test_prepare_close_missing_closed_and_schema_paths(ctx):
    decision_command(ctx, subcommand("close", id=99))
    sent = ctx.interaction.modals_sent[-1]
    delivery = invoke(
        ctx,
        decisionbook.decision_close_modal,
        modal(sent["custom_id"], **{sent["fields"][0].custom_id: "No result"}),
    )
    assert "not found" in delivery["content"]

    seed(ctx, 1, closed=True)
    decision_command(ctx, subcommand("close", id=1))
    sent = ctx.interaction.modals_sent[-1]
    delivery = invoke(
        ctx,
        decisionbook.decision_close_modal,
        modal(sent["custom_id"], **{sent["fields"][0].custom_id: "Already done"}),
    )
    assert "already closed" in delivery["content"]

    ctx.kv.set(SCHEMA_KEY, 3)
    decision_command(ctx, subcommand("close", id=1))
    sent = ctx.interaction.modals_sent[-1]
    delivery = invoke(
        ctx,
        decisionbook.decision_close_modal,
        modal(sent["custom_id"], **{sent["fields"][0].custom_id: "Cannot close"}),
    )
    assert "storage is repaired" in delivery["content"]


def test_close_missing_validation_schema_and_race_guard_failure(ctx, monkeypatch):
    delivery = invoke(ctx, decision_close, interaction(id=99, outcome="Done"))
    assert "not found" in delivery["content"]
    delivery = invoke(ctx, decision_close, interaction(id=True, outcome="Done"))
    assert "positive whole number" in delivery["content"]

    seed(ctx, 1)
    ctx.kv.set(SCHEMA_KEY, 3)
    delivery = invoke(ctx, decision_close, interaction(id=1, outcome="Done"))
    assert "storage is repaired" in delivery["content"]

    ctx.kv.set(SCHEMA_KEY, 2)
    monkeypatch.setattr(
        ctx.ephemeral,
        "dedup",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    delivery = invoke(ctx, decision_close, interaction(id=1, outcome="Done"))
    assert ctx.kv.get(decision_key(1))["status"] == "open"
    assert "cannot safely close" in delivery["content"]


def test_root_dispatch_invalid_and_remaining_action_branches(ctx):
    delivery = invoke(ctx, decision_command, interaction())
    assert "Choose one" in delivery["content"]
    delivery = invoke(
        ctx,
        decision_command,
        {
            "options": [{"name": "delete", "type": 1}],
            "channel_id": CHANNEL_ID,
            "user_id": AUTHOR_ID,
        },
    )
    assert "unavailable" in delivery["content"]

    seed(ctx, 1)
    delivery = invoke(ctx, decision_command, subcommand("list"))
    assert delivery["embeds"]
    delivery = invoke(ctx, decision_command, subcommand("view", id=1))
    assert delivery["embeds"]


def test_list_component_malformed_and_storage_failure_paths(ctx, monkeypatch):
    delivery = invoke(
        ctx,
        decision_list_component,
        {"custom_id": None, "channel_id": CHANNEL_ID, "user_id": AUTHOR_ID},
    )
    assert "invalid" in delivery["content"]
    delivery = invoke(ctx, decision_list_component, component("decision:page:noseparator"))
    assert "invalid" in delivery["content"]

    monkeypatch.setattr(
        ctx.kv,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    delivery = invoke(
        ctx,
        decision_list_component,
        component("decision:page:validtoken:1"),
    )
    assert "could not update" in delivery["content"]


def test_dashboard_parameter_and_time_fallbacks():
    assert _dashboard_integer(True, default=7, maximum=10) == 7
    assert _dashboard_integer(float("inf"), default=7, maximum=10) == 7
    assert _dashboard_integer(-5, default=7, minimum=0, maximum=10) == 0
    assert _dashboard_time(None) == "—"
    assert _dashboard_time("not-a-date") == "Unavailable"
