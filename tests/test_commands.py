from __future__ import annotations

from copy import deepcopy

from conftest import (
    AUTHOR_ID,
    CHANNEL_ID,
    OTHER_ID,
    component,
    interaction,
    invoke,
    modal,
    subcommand,
)

from core import decision_key, make_record
from decisionbook import (
    ADD_MODAL_ID,
    CLOSE_MODAL_ID,
    COUNTER_KEY,
    SCHEMA_KEY,
    allocate_decision_id,
    decision_add,
    decision_add_modal,
    decision_close,
    decision_close_component,
    decision_close_modal,
    decision_command,
    decision_help,
    decision_list,
    decision_list_component,
    decision_view,
    decision_view_component,
    install,
    load_record_window,
    status_counts,
)


def add_event(user_id=AUTHOR_ID, **overrides):
    values = {
        "title": "Weekly event time",
        "choice": "Saturday at 8 PM",
        "reason": "Best overlap across regions",
        "tags": "events, scheduling",
    }
    values.update(overrides)
    return interaction(user_id=user_id, **values)


def seed(ctx, decision_id: int, *, title: str | None = None, status: str = "open"):
    ctx.kv.set(SCHEMA_KEY, 2)
    item = make_record(
        decision_id,
        title=title or f"Decision {decision_id}",
        choice=f"Choice {decision_id}",
        reason="Reason",
        tags="tag",
        author_id=AUTHOR_ID,
        channel_id=CHANNEL_ID,
        created_at="2026-07-11T18:30:00+00:00",
    )
    if status == "closed":
        item["status"] = "closed"
        item["closure"] = {
            "outcome": "Done",
            "closed_by": AUTHOR_ID,
            "closed_at": "2026-07-12T18:30:00+00:00",
        }
    ctx.kv.set(decision_key(decision_id), item)
    return item


def component_dicts(delivery):
    return [row.to_dict() for row in delivery["components"]]


def test_root_add_opens_accessible_modal(ctx):
    decision_command(ctx, subcommand("add"))
    sent = ctx.interaction.modals_sent[-1]
    assert sent["custom_id"] == ADD_MODAL_ID
    assert [field.custom_id for field in sent["fields"]] == [
        "title",
        "choice",
        "reason",
        "tags",
    ]
    assert sent["fields"][1].style == sent["fields"][2].style == "paragraph"
    assert sent["fields"][0].max_length == 100
    assert sent["fields"][2].max_length == 1000
    assert ctx.interaction.responses == []


def test_add_modal_stores_record_and_publicly_responds(ctx):
    delivery = invoke(
        ctx,
        decision_add_modal,
        modal(
            ADD_MODAL_ID,
            title="Weekly event time",
            choice="Saturday at 8 PM",
            reason="Best overlap",
            tags="events, scheduling",
        ),
    )
    stored = ctx.kv.get(decision_key(1))
    assert stored["id"] == 1
    assert stored["author_id"] == AUTHOR_ID
    assert stored["channel_id"] == CHANNEL_ID
    assert stored["tags"] == ["events", "scheduling"]
    assert delivery["ephemeral"] is False
    assert delivery["allowed_mentions"] == {"parse": []}
    assert delivery["embeds"][0]["title"].startswith("Decision #1")
    assert component_dicts(delivery)[0]["components"][0]["custom_id"] == "decision:close:1"
    assert status_counts(ctx) == (1, 0)


def test_ids_are_monotonic(ctx):
    decision_add(ctx, add_event(title="One"))
    decision_add(ctx, add_event(title="Two"))
    assert ctx.kv.get(decision_key(1))["title"] == "One"
    assert ctx.kv.get(decision_key(2))["title"] == "Two"


def test_missing_or_stale_counter_never_overwrites_existing_decision(ctx):
    original = seed(ctx, 1, title="Immutable original")
    ctx.kv.delete(COUNTER_KEY)
    decision_add(ctx, add_event(title="New decision"))
    assert ctx.kv.get(decision_key(1)) == original
    assert ctx.kv.get(decision_key(2))["title"] == "New decision"

    seed(ctx, 3, title="Third")
    ctx.kv.set(COUNTER_KEY, 1)
    decision_add(ctx, add_event(title="Fourth"))
    assert ctx.kv.get(decision_key(3))["title"] == "Third"
    assert ctx.kv.get(decision_key(4))["title"] == "Fourth"


def test_collision_probe_is_fail_safe(ctx):
    seed(ctx, 1)
    ctx.kv.set(COUNTER_KEY, 0)
    assert allocate_decision_id(ctx) == 2
    assert ctx.kv.get(decision_key(1))["id"] == 1


def test_validation_failure_does_not_allocate_id(ctx):
    delivery = invoke(ctx, decision_add, add_event(title=""))
    assert ctx.kv.get(COUNTER_KEY) is None
    assert delivery["ephemeral"] is True


def test_invalid_actor_is_rejected_before_storage(ctx):
    delivery = invoke(ctx, decision_add, add_event(user_id="author-1"))
    assert ctx.kv.count(prefix="decision:") == 0
    assert "valid user identity" in delivery["content"]


def test_future_schema_blocks_install_and_mutation(ctx):
    ctx.kv.set(SCHEMA_KEY, 3)
    try:
        install(ctx)
    except Exception as exc:
        assert "schema" in str(exc).lower()
    else:
        raise AssertionError("install must fail closed for an incompatible schema")
    delivery = invoke(ctx, decision_add, add_event())
    assert ctx.kv.count(prefix="decision:") == 0
    assert "repair" in delivery["content"].lower()


def test_storage_failure_never_claims_success(ctx, monkeypatch):
    original = ctx.kv.set

    def fail(key, value, **kwargs):
        if key.startswith("decision:"):
            raise RuntimeError("storage offline")
        return original(key, value, **kwargs)

    monkeypatch.setattr(ctx.kv, "set", fail)
    delivery = invoke(ctx, decision_add, add_event())
    assert delivery["ephemeral"] is True
    assert "could not store" in delivery["content"]
    assert "Nothing was saved" in delivery["content"]


def test_response_failure_after_add_reports_committed_state(ctx, monkeypatch):
    original_respond = ctx.interaction.respond
    original_followup = ctx.interaction.followup
    calls = 0

    def fail_once(deliver, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transport dropped")
        return deliver(*args, **kwargs)

    monkeypatch.setattr(
        ctx.interaction,
        "respond",
        lambda *args, **kwargs: fail_once(original_respond, *args, **kwargs),
    )
    monkeypatch.setattr(
        ctx.interaction,
        "followup",
        lambda *args, **kwargs: fail_once(original_followup, *args, **kwargs),
    )
    delivery = invoke(ctx, decision_add, add_event())
    assert ctx.kv.get(decision_key(1))["id"] == 1
    assert "was saved" in delivery["content"]
    assert "ID 1" in delivery["content"]


def test_view_open_and_closed_records(ctx):
    decision_add(ctx, add_event())
    delivery = invoke(ctx, decision_view, interaction(id=1))
    assert "Decision #1" in delivery["embeds"][0]["title"]
    decision_close(ctx, interaction(id=1, outcome="It worked"))
    delivery = invoke(ctx, decision_view, interaction(id=1))
    assert any(field["name"] == "Outcome" for field in delivery["embeds"][0]["fields"])


def test_view_distinguishes_missing_from_malformed(ctx):
    delivery = invoke(ctx, decision_view, interaction(id=99))
    assert "not found" in delivery["content"]
    ctx.kv.set(decision_key(1), {"schema_version": 2, "id": 1})
    delivery = invoke(ctx, decision_view, interaction(id=1))
    assert "temporarily unavailable" in delivery["content"]


def test_key_payload_id_mismatch_never_renders_spoofed_id(ctx):
    bad = seed(ctx, 1)
    bad["id"] = 999
    ctx.kv.set(decision_key(1), bad)
    delivery = invoke(ctx, decision_view, interaction(id=1))
    assert delivery["embeds"] is None
    assert "integrity checks" in delivery["content"]


def test_list_is_newest_first_searchable_and_explicit(ctx):
    decision_add(ctx, add_event(title="Alpha", tags="release"))
    decision_add(ctx, add_event(title="Beta", reason="Security review", tags="security"))
    delivery = invoke(
        ctx,
        decision_list,
        interaction(query="security", status="all", limit=10),
    )
    description = delivery["embeds"][0]["description"]
    assert "Open · #2" in description
    assert "#1" not in description
    assert "Showing 1–1 of 1" in delivery["embeds"][0]["footer"]["text"]


def test_empty_ledger_and_no_matches_have_distinct_guidance(ctx):
    delivery = invoke(ctx, decision_list, interaction())
    assert "No decisions have been recorded" in delivery["content"]
    seed(ctx, 1)
    delivery = invoke(ctx, decision_list, interaction(query="does-not-exist"))
    assert "No decisions match" in delivery["content"]
    assert "shorter query" in delivery["content"]


def test_list_distinguishes_integrity_failure_from_an_empty_ledger(ctx):
    ctx.kv.set(decision_key(1), {"schema_version": 2, "id": 1})
    delivery = invoke(ctx, decision_list, interaction())
    assert "cannot search until its storage state is repaired" in delivery["content"]
    assert "Ledger health" in delivery["content"]
    assert "No decisions have been recorded" not in delivery["content"]


def test_list_limit_and_query_are_strict(ctx):
    seed(ctx, 1)
    delivery = invoke(ctx, decision_list, interaction(limit=999))
    assert "whole number from 1 to 10" in delivery["content"]
    delivery = invoke(ctx, decision_list, interaction(query="x" * 81))
    assert "too long" in delivery["content"]


def test_newest_record_above_old_scan_boundary_is_visible(ctx, monkeypatch):
    for decision_id in range(1, 502):
        seed(ctx, decision_id)
    observed_batches = []
    original = ctx.kv.get_many

    def capture(keys):
        observed_batches.append(len(keys))
        assert len(keys) <= 50
        return original(keys)

    monkeypatch.setattr(ctx.kv, "get_many", capture)
    delivery = invoke(ctx, decision_list, interaction(limit=10))
    description = delivery["embeds"][0]["description"]
    assert "#501" in description
    assert "#1" not in description
    assert max(observed_batches) <= 50
    footer = delivery["embeds"][0]["footer"]["text"]
    assert "Search incomplete" in footer
    assert "newest 500 channel decisions across 500 ledger IDs were searched" in footer


def test_record_window_ignores_kv_list_values_order_and_aggregates_bad_records(ctx):
    for decision_id in range(1, 4):
        seed(ctx, decision_id)
    bad = seed(ctx, 4)
    bad["id"] = 999
    ctx.kv.set(decision_key(4), bad)
    window = load_record_window(ctx)
    assert [item["id"] for item in window.records] == [3, 2, 1]
    assert window.malformed_count == 1
    warnings = [entry for entry in ctx.log_entries if entry["level"] == "warning"]
    assert len([entry for entry in warnings if "skipped" in entry["message"]]) == 1


def test_list_buttons_open_private_pages_and_views(ctx):
    for decision_id in range(1, 13):
        seed(ctx, decision_id)
    delivery = invoke(ctx, decision_list, interaction(limit=5))
    rows = component_dicts(delivery)
    assert rows[0]["components"][0]["label"] == "View #12"
    nav = rows[-1]["components"]
    next_id = next(item["custom_id"] for item in nav if item["label"] == "Next")

    delivery = invoke(ctx, decision_list_component, component(next_id))
    assert delivery["ephemeral"] is True
    assert not delivery.get("update_message", False)
    assert "#7" in delivery["embeds"][0]["description"]
    assert "#12" not in delivery["embeds"][0]["description"]

    view_id = component_dicts(delivery)[0]["components"][0]["custom_id"]
    delivery = invoke(ctx, decision_view_component, component(view_id))
    assert delivery["ephemeral"] is True
    assert "Decision #7" in delivery["embeds"][0]["title"]


def test_expired_or_hostile_list_control_fails_safely(ctx):
    delivery = invoke(
        ctx,
        decision_list_component,
        component("decision:page:missing-token:2"),
    )
    assert "expired" in delivery["content"]
    delivery = invoke(ctx, decision_list_component, component("decision:page:../../bad:2"))
    assert "invalid" in delivery["content"]


def test_close_command_and_button_open_prefilled_modal_for_author(ctx):
    decision_add(ctx, add_event())
    decision_command(ctx, subcommand("close", id=1))
    sent = ctx.interaction.modals_sent[-1]
    assert sent["custom_id"] == CLOSE_MODAL_ID
    assert sent["title"] == "Close Decision #1"
    assert len(sent["fields"]) == 1
    assert sent["fields"][0].custom_id.startswith("outcome:")
    assert sent["fields"][0].style == "paragraph"

    decision_close_component(ctx, component("decision:close:1"))
    sent = ctx.interaction.modals_sent[-1]
    assert sent["title"] == "Close Decision #1"
    assert len(sent["fields"]) == 1
    assert sent["fields"][0].custom_id.startswith("outcome:")


def test_non_author_cannot_open_or_submit_close(ctx):
    decision_add(ctx, add_event())
    before = deepcopy(ctx.kv.get(decision_key(1)))
    decision_close_component(ctx, component("decision:close:1", user_id=OTHER_ID))
    close_modal = ctx.interaction.modals_sent[-1]
    outcome_field = close_modal["fields"][0].custom_id
    delivery = invoke(
        ctx,
        decision_close_modal,
        modal(
            close_modal["custom_id"],
            user_id=OTHER_ID,
            **{outcome_field: "Hijacked"},
        ),
    )
    assert delivery["ephemeral"] is True
    assert "Only the person who recorded" in delivery["content"]
    assert ctx.kv.get(decision_key(1)) == before


def test_close_modal_commits_and_preserves_original_fields(ctx):
    decision_add(ctx, add_event())
    before = deepcopy(ctx.kv.get(decision_key(1)))
    decision_close_component(ctx, component("decision:close:1"))
    close_modal = ctx.interaction.modals_sent[-1]
    outcome_field = close_modal["fields"][0].custom_id
    delivery = invoke(
        ctx,
        decision_close_modal,
        modal(close_modal["custom_id"], **{outcome_field: "Attendance improved"}),
    )
    after = ctx.kv.get(decision_key(1))
    for key in (
        "title",
        "choice",
        "reason",
        "tags",
        "author_id",
        "channel_id",
        "created_at",
    ):
        assert after[key] == before[key]
    assert after["status"] == "closed"
    assert status_counts(ctx) == (0, 1)
    assert delivery["ephemeral"] is False


def test_reclose_is_explicit_and_ignores_invalid_replacement(ctx):
    decision_add(ctx, add_event())
    decision_close(ctx, interaction(id=1, outcome="First"))
    delivery = invoke(ctx, decision_close, interaction(id=1, outcome="x" * 501))
    assert ctx.kv.get(decision_key(1))["closure"]["outcome"] == "First"
    assert "already closed" in delivery["content"]
    assert delivery["ephemeral"] is True


def test_response_failure_after_close_reports_committed_state(ctx, monkeypatch):
    decision_add(ctx, add_event())
    original_respond = ctx.interaction.respond
    original_followup = ctx.interaction.followup
    calls = 0

    def fail_once(deliver, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transport dropped")
        return deliver(*args, **kwargs)

    monkeypatch.setattr(
        ctx.interaction,
        "respond",
        lambda *args, **kwargs: fail_once(original_respond, *args, **kwargs),
    )
    monkeypatch.setattr(
        ctx.interaction,
        "followup",
        lambda *args, **kwargs: fail_once(original_followup, *args, **kwargs),
    )
    delivery = invoke(ctx, decision_close, interaction(id=1, outcome="Done"))
    assert ctx.kv.get(decision_key(1))["status"] == "closed"
    assert "was saved" in delivery["content"]


def test_metric_and_log_failures_never_break_commands(ctx, monkeypatch):
    monkeypatch.setattr(
        ctx.metrics,
        "record",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError()),
    )
    monkeypatch.setattr(ctx, "log", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError()))
    delivery = invoke(ctx, decision_add, add_event())
    assert ctx.kv.get(decision_key(1))["id"] == 1
    assert delivery["embeds"][0]["title"].startswith("Decision #1")


def test_root_dispatch_supports_options_and_command_options(ctx):
    delivery = invoke(ctx, decision_command, subcommand("help"))
    assert delivery["ephemeral"] is True
    delivery = invoke(
        ctx,
        decision_command,
        subcommand("view", option_key="command_options", id=999),
    )
    assert "not found" in delivery["content"]


def test_help_is_private_mentions_disabled_and_actionable(ctx):
    delivery = invoke(ctx, decision_help, interaction())
    assert delivery["ephemeral"] is True
    assert delivery["allowed_mentions"] == {"parse": []}
    assert "Quick start" in delivery["content"]
    assert "/decision add" in delivery["content"]
