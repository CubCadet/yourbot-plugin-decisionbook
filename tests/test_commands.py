from __future__ import annotations

from copy import deepcopy

from conftest import AUTHOR_ID, OTHER_ID, component, interaction, modal, subcommand

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
    load_record_window,
    ready,
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


def response(ctx):
    return ctx.interaction.responses[-1]


def seed(ctx, decision_id: int, *, title: str | None = None, status: str = "open"):
    item = make_record(
        decision_id,
        title=title or f"Decision {decision_id}",
        choice=f"Choice {decision_id}",
        reason="Reason",
        tags="tag",
        author_id=AUTHOR_ID,
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


def component_dicts(ctx):
    return [row.to_dict() for row in response(ctx)["components"]]


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
    decision_add_modal(
        ctx,
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
    assert stored["tags"] == ["events", "scheduling"]
    assert response(ctx)["ephemeral"] is False
    assert response(ctx)["allowed_mentions"] == {"parse": []}
    assert response(ctx)["embeds"][0]["title"].startswith("Decision #1")
    assert component_dicts(ctx)[0]["components"][0]["custom_id"] == "decision:close:1"
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
    decision_add(ctx, add_event(title=""))
    assert ctx.kv.get(COUNTER_KEY) is None
    assert response(ctx)["ephemeral"] is True


def test_invalid_actor_is_rejected_before_storage(ctx):
    decision_add(ctx, add_event(user_id="author-1"))
    assert ctx.kv.count(prefix="decision:") == 0
    assert "valid user identity" in response(ctx)["content"]


def test_future_schema_blocks_ready_and_mutation(ctx):
    ctx.kv.set(SCHEMA_KEY, 2)
    try:
        ready(ctx)
    except Exception as exc:
        assert "schema" in str(exc).lower()
    else:
        raise AssertionError("ready must fail closed for an incompatible schema")
    decision_add(ctx, add_event())
    assert ctx.kv.count(prefix="decision:") == 0
    assert "repair" in response(ctx)["content"].lower()


def test_storage_failure_never_claims_success(ctx, monkeypatch):
    original = ctx.kv.set

    def fail(key, value, **kwargs):
        if key.startswith("decision:"):
            raise RuntimeError("storage offline")
        return original(key, value, **kwargs)

    monkeypatch.setattr(ctx.kv, "set", fail)
    decision_add(ctx, add_event())
    assert response(ctx)["ephemeral"] is True
    assert "could not store" in response(ctx)["content"]
    assert "Nothing was saved" in response(ctx)["content"]


def test_response_failure_after_add_reports_committed_state(ctx, monkeypatch):
    original = ctx.interaction.respond
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transport dropped")
        return original(*args, **kwargs)

    monkeypatch.setattr(ctx.interaction, "respond", fail_once)
    decision_add(ctx, add_event())
    assert ctx.kv.get(decision_key(1))["id"] == 1
    assert "was saved" in response(ctx)["content"]
    assert "ID 1" in response(ctx)["content"]


def test_view_open_and_closed_records(ctx):
    decision_add(ctx, add_event())
    decision_view(ctx, interaction(id=1))
    assert "Decision #1" in response(ctx)["embeds"][0]["title"]
    decision_close(ctx, interaction(id=1, outcome="It worked"))
    decision_view(ctx, interaction(id=1))
    assert any(field["name"] == "Outcome" for field in response(ctx)["embeds"][0]["fields"])


def test_view_distinguishes_missing_from_malformed(ctx):
    decision_view(ctx, interaction(id=99))
    assert "not found" in response(ctx)["content"]
    ctx.kv.set(decision_key(1), {"schema_version": 1, "id": 1})
    decision_view(ctx, interaction(id=1))
    assert "temporarily unavailable" in response(ctx)["content"]


def test_key_payload_id_mismatch_never_renders_spoofed_id(ctx):
    bad = seed(ctx, 1)
    bad["id"] = 999
    ctx.kv.set(decision_key(1), bad)
    decision_view(ctx, interaction(id=1))
    assert response(ctx)["embeds"] is None
    assert "integrity checks" in response(ctx)["content"]


def test_list_is_newest_first_searchable_and_explicit(ctx):
    decision_add(ctx, add_event(title="Alpha", tags="release"))
    decision_add(ctx, add_event(title="Beta", reason="Security review", tags="security"))
    decision_list(ctx, interaction(query="security", status="all", limit=10))
    description = response(ctx)["embeds"][0]["description"]
    assert "Open · #2" in description
    assert "#1" not in description
    assert "Showing 1–1 of 1" in response(ctx)["embeds"][0]["footer"]["text"]


def test_empty_ledger_and_no_matches_have_distinct_guidance(ctx):
    decision_list(ctx, interaction())
    assert "No decisions have been recorded" in response(ctx)["content"]
    seed(ctx, 1)
    decision_list(ctx, interaction(query="does-not-exist"))
    assert "No decisions match" in response(ctx)["content"]
    assert "shorter query" in response(ctx)["content"]


def test_list_distinguishes_unavailable_records_from_no_matches(ctx):
    ctx.kv.set(decision_key(1), {"schema_version": 1, "id": 1})
    decision_list(ctx, interaction())
    assert "none are currently available" in response(ctx)["content"]
    assert "integrity checks" in response(ctx)["content"]


def test_list_limit_and_query_are_strict(ctx):
    seed(ctx, 1)
    decision_list(ctx, interaction(limit=999))
    assert "whole number from 1 to 10" in response(ctx)["content"]
    decision_list(ctx, interaction(query="x" * 81))
    assert "too long" in response(ctx)["content"]


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
    decision_list(ctx, interaction(limit=10))
    description = response(ctx)["embeds"][0]["description"]
    assert "#501" in description
    assert "#1" not in description
    assert max(observed_batches) <= 50
    assert "newest 500 of 501" in response(ctx)["embeds"][0]["footer"]["text"]


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


def test_list_buttons_page_in_place_and_view_privately(ctx):
    for decision_id in range(1, 13):
        seed(ctx, decision_id)
    decision_list(ctx, interaction(limit=5))
    rows = component_dicts(ctx)
    assert rows[0]["components"][0]["label"] == "View #12"
    nav = rows[-1]["components"]
    next_id = next(item["custom_id"] for item in nav if item["label"] == "Next")

    decision_list_component(ctx, component(next_id))
    assert response(ctx)["update_message"] is True
    assert "#7" in response(ctx)["embeds"][0]["description"]
    assert "#12" not in response(ctx)["embeds"][0]["description"]

    view_id = component_dicts(ctx)[0]["components"][0]["custom_id"]
    decision_view_component(ctx, component(view_id))
    assert response(ctx)["ephemeral"] is True
    assert "Decision #7" in response(ctx)["embeds"][0]["title"]


def test_expired_or_hostile_list_control_fails_safely(ctx):
    decision_list_component(ctx, component("decision:page:missing-token:2"))
    assert "expired" in response(ctx)["content"]
    decision_list_component(ctx, component("decision:page:../../bad:2"))
    assert "invalid" in response(ctx)["content"]


def test_close_command_and_button_open_prefilled_modal_for_author(ctx):
    decision_add(ctx, add_event())
    decision_command(ctx, subcommand("close", id=1))
    sent = ctx.interaction.modals_sent[-1]
    assert sent["custom_id"] == CLOSE_MODAL_ID
    assert sent["fields"][0].value == "1"
    assert sent["fields"][1].style == "paragraph"

    decision_close_component(ctx, component("decision:close:1"))
    assert ctx.interaction.modals_sent[-1]["fields"][0].value == "1"


def test_non_author_cannot_open_or_submit_close(ctx):
    decision_add(ctx, add_event())
    before = deepcopy(ctx.kv.get(decision_key(1)))
    decision_close_component(ctx, component("decision:close:1", user_id=OTHER_ID))
    assert response(ctx)["ephemeral"] is True
    decision_close_modal(
        ctx,
        modal(CLOSE_MODAL_ID, user_id=OTHER_ID, id="1", outcome="Hijacked"),
    )
    assert ctx.kv.get(decision_key(1)) == before


def test_close_modal_commits_and_preserves_original_fields(ctx):
    decision_add(ctx, add_event())
    before = deepcopy(ctx.kv.get(decision_key(1)))
    decision_close_modal(ctx, modal(CLOSE_MODAL_ID, id="1", outcome="Attendance improved"))
    after = ctx.kv.get(decision_key(1))
    for key in ("title", "choice", "reason", "tags", "author_id", "created_at"):
        assert after[key] == before[key]
    assert after["status"] == "closed"
    assert status_counts(ctx) == (0, 1)
    assert response(ctx)["ephemeral"] is False


def test_reclose_is_explicit_and_ignores_invalid_replacement(ctx):
    decision_add(ctx, add_event())
    decision_close(ctx, interaction(id=1, outcome="First"))
    decision_close(ctx, interaction(id=1, outcome="x" * 501))
    assert ctx.kv.get(decision_key(1))["closure"]["outcome"] == "First"
    assert "already closed" in response(ctx)["content"]
    assert response(ctx)["ephemeral"] is True


def test_response_failure_after_close_reports_committed_state(ctx, monkeypatch):
    decision_add(ctx, add_event())
    original = ctx.interaction.respond
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transport dropped")
        return original(*args, **kwargs)

    monkeypatch.setattr(ctx.interaction, "respond", fail_once)
    decision_close(ctx, interaction(id=1, outcome="Done"))
    assert ctx.kv.get(decision_key(1))["status"] == "closed"
    assert "was saved" in response(ctx)["content"]


def test_metric_and_log_failures_never_break_commands(ctx, monkeypatch):
    monkeypatch.setattr(
        ctx.metrics,
        "record",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError()),
    )
    monkeypatch.setattr(ctx, "log", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError()))
    decision_add(ctx, add_event())
    assert ctx.kv.get(decision_key(1))["id"] == 1
    assert response(ctx)["embeds"][0]["title"].startswith("Decision #1")


def test_root_dispatch_supports_options_and_command_options(ctx):
    decision_command(ctx, subcommand("help"))
    assert response(ctx)["ephemeral"] is True
    decision_command(ctx, subcommand("view", option_key="command_options", id=999))
    assert "not found" in response(ctx)["content"]


def test_help_is_private_mentions_disabled_and_actionable(ctx):
    decision_help(ctx, interaction())
    assert response(ctx)["ephemeral"] is True
    assert response(ctx)["allowed_mentions"] == {"parse": []}
    assert "Quick start" in response(ctx)["content"]
    assert "/decision add" in response(ctx)["content"]
