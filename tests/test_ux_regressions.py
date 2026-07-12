from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, cast

import pytest
from conftest import AUTHOR_ID, OTHER_ID
from yourbot_sdk import KvQuotaError
from yourbot_sdk.testing import MockContext, make_event

from core import close_record, decision_key, make_record
from decisionbook import (
    DECISION_PREFIX,
    LIST_COMPONENT_PREFIX,
    LIST_STATE_PREFIX,
    SCHEMA_KEY,
    decision_add_modal,
    decision_close_component,
    decision_close_modal,
    decision_command,
    decision_help,
    decision_list,
    decision_list_component,
    decision_view,
    get_recent_decisions,
    status_counts,
)

GUILD_ID = "100000000000000099"
CHANNEL_A = "200000000000000001"
CHANNEL_B = "200000000000000002"


def interaction(
    *,
    user_id: str = AUTHOR_ID,
    channel_id: str = CHANNEL_A,
    **options: Any,
) -> dict[str, Any]:
    return make_event(
        "interaction_create",
        command_name="decision",
        guild_id=GUILD_ID,
        channel_id=channel_id,
        user_id=user_id,
        options=[{"name": name, "value": value} for name, value in options.items()],
    )


def subcommand(
    name: str,
    *,
    user_id: str = AUTHOR_ID,
    channel_id: str = CHANNEL_A,
    **options: Any,
) -> dict[str, Any]:
    return make_event(
        "interaction_create",
        command_name="decision",
        guild_id=GUILD_ID,
        channel_id=channel_id,
        user_id=user_id,
        options=[
            {
                "name": name,
                "type": 1,
                "options": [
                    {
                        "name": option_name,
                        "type": 4 if isinstance(value, int) else 3,
                        "value": value,
                    }
                    for option_name, value in options.items()
                ],
            }
        ],
    )


def component(
    custom_id: str,
    *,
    user_id: str = AUTHOR_ID,
    channel_id: str = CHANNEL_A,
) -> dict[str, Any]:
    return make_event(
        "interaction_create",
        interaction_type=3,
        custom_id=custom_id,
        guild_id=GUILD_ID,
        channel_id=channel_id,
        user_id=user_id,
    )


def modal(
    custom_id: str,
    *,
    user_id: str = AUTHOR_ID,
    channel_id: str = CHANNEL_A,
    **values: Any,
) -> dict[str, Any]:
    return make_event(
        "interaction_create",
        interaction_type=5,
        custom_id=custom_id,
        guild_id=GUILD_ID,
        channel_id=channel_id,
        user_id=user_id,
        modal_values=values,
    )


def seed(
    ctx: MockContext,
    decision_id: int,
    *,
    title: str = "Routine decision",
    status: str = "open",
    channel_id: str = CHANNEL_A,
) -> dict[str, Any]:
    ctx.kv.set(SCHEMA_KEY, 2)
    record = make_record(
        decision_id,
        title=title,
        choice="Approved",
        reason="This was the best available option.",
        tags="regression",
        author_id=AUTHOR_ID,
        channel_id=channel_id,
        created_at="2026-07-11T18:30:00+00:00",
    )
    if status == "closed":
        record = close_record(
            record,
            actor_id=AUTHOR_ID,
            outcome="Completed",
            closed_at="2026-07-12T18:30:00+00:00",
        )
    ctx.kv.set(decision_key(decision_id), record)
    return record


def invoke(
    ctx: MockContext,
    handler: Callable[..., None],
    *args: Any,
) -> dict[str, Any]:
    response_count = len(ctx.interaction.responses)
    followup_count = len(ctx.interaction.followups)
    handler(ctx, *args)
    if len(ctx.interaction.followups) > followup_count:
        return ctx.interaction.followups[-1]
    if len(ctx.interaction.responses) > response_count:
        return ctx.interaction.responses[-1]
    raise AssertionError("The interaction handler did not deliver a response or follow-up.")


def component_rows(delivery: dict[str, Any]) -> list[dict[str, Any]]:
    rows = delivery.get("components") or []
    return [row if isinstance(row, dict) else row.to_dict() for row in rows]


def button_custom_id(delivery: dict[str, Any], label: str) -> str:
    for row in component_rows(delivery):
        for item in row.get("components", []):
            if item.get("label") == label:
                custom_id = item.get("custom_id")
                assert isinstance(custom_id, str)
                return custom_id
    raise AssertionError(f"No {label!r} button was rendered.")


def embed_description(delivery: dict[str, Any]) -> str:
    embeds = delivery.get("embeds") or []
    assert embeds, "Expected an embed response."
    return str(embeds[0].get("description") or "")


def rendered(delivery: dict[str, Any]) -> str:
    return f"{delivery.get('content') or ''} {delivery.get('embeds') or ''}"


def install_defer_guard(
    ctx: MockContext,
    monkeypatch: pytest.MonkeyPatch,
    *,
    baseline: int,
) -> None:
    """Make every KV RPC assert that the interaction acknowledged first."""
    method_names = (
        "get",
        "get_many",
        "count",
        "list",
        "exists",
        "set",
        "set_many",
        "increment",
        "decrement",
        "delete",
    )
    for method_name in method_names:
        original = getattr(ctx.kv, method_name)

        def guarded(
            *args: Any,
            _method_name: str = method_name,
            _original: Callable[..., Any] = original,
            **kwargs: Any,
        ) -> Any:
            assert len(ctx.interaction.defers) > baseline, (
                f"KV {_method_name} ran before the interaction was deferred"
            )
            return _original(*args, **kwargs)

        monkeypatch.setattr(ctx.kv, method_name, guarded)


def test_close_modal_submission_cannot_redirect_the_bound_target(ctx: MockContext) -> None:
    seed(ctx, 1, title="Intended target")
    seed(ctx, 2, title="Must remain open")

    decision_close_component(cast(Any, ctx), component("decision:close:1"))
    sent = ctx.interaction.modals_sent[-1]
    outcome_field = sent["fields"][0].custom_id
    malicious = modal(
        sent["custom_id"],
        id="2",
        **{outcome_field: "Redirected target"},
    )
    invoke(ctx, decision_close_modal, malicious)

    second = ctx.kv.get(decision_key(2))
    assert second["status"] == "open"
    assert "Redirected target" not in str(second)


def test_newer_close_form_invalidates_the_older_without_redirecting_it(
    ctx: MockContext,
) -> None:
    seed(ctx, 1, title="First target")
    seed(ctx, 2, title="Second target")

    decision_close_component(cast(Any, ctx), component("decision:close:1"))
    first_modal = ctx.interaction.modals_sent[-1]
    first_outcome = first_modal["fields"][0].custom_id

    decision_close_component(cast(Any, ctx), component("decision:close:2"))
    second_modal = ctx.interaction.modals_sent[-1]
    second_outcome = second_modal["fields"][0].custom_id
    assert first_outcome != second_outcome

    stale = invoke(
        ctx,
        decision_close_modal,
        modal(first_modal["custom_id"], **{first_outcome: "First outcome"}),
    )
    assert "expired, or was replaced" in rendered(stale)
    assert ctx.kv.get(decision_key(1))["status"] == "open"
    assert ctx.kv.get(decision_key(2))["status"] == "open"

    invoke(
        ctx,
        decision_close_modal,
        modal(second_modal["custom_id"], **{second_outcome: "Second outcome"}),
    )
    assert ctx.kv.get(decision_key(2))["status"] == "closed"
    assert ctx.kv.get(decision_key(1))["status"] == "open"


def test_close_form_rejects_missing_forged_and_ambiguous_nonce_fields(
    ctx: MockContext,
) -> None:
    seed(ctx, 1)
    seed(ctx, 2)
    decision_close_component(cast(Any, ctx), component("decision:close:1"))
    close_modal = ctx.interaction.modals_sent[-1]
    outcome_field = close_modal["fields"][0].custom_id
    token = outcome_field.rsplit(":", 1)[1]

    for values in (
        {"outcome": "Missing nonce"},
        {"outcome:abcdefgh": "Forged nonce"},
        {f"outcome:2:{token}": "Forged target"},
        {outcome_field: "Real field", "outcome:abcdefgh": "Second field"},
    ):
        delivery = invoke(
            ctx,
            decision_close_modal,
            modal(close_modal["custom_id"], **values),
        )
        assert "close form is invalid" in rendered(delivery)
        assert ctx.kv.get(decision_key(1))["status"] == "open"
        assert ctx.kv.get(decision_key(2))["status"] == "open"

    invoke(
        ctx,
        decision_close_modal,
        modal(close_modal["custom_id"], **{outcome_field: "Bound outcome"}),
    )
    assert ctx.kv.get(decision_key(1))["status"] == "closed"


def test_old_text_search_never_reports_a_false_definitive_no_match(ctx: MockContext) -> None:
    seed(ctx, 1, title="Buried needle")
    for decision_id in range(2, 502):
        seed(ctx, decision_id)

    delivery = invoke(ctx, decision_list, interaction(query="buried needle", limit=5))
    text = rendered(delivery).lower()
    found_exact_record = re.search(r"#1(?!\d)", text) is not None
    reports_bounded_search = any(
        phrase in text
        for phrase in ("search incomplete", "newest 500", "not all decisions were searched")
    )
    assert found_exact_record or reports_bounded_search
    assert "no decisions match" not in text


def test_exact_numeric_search_loads_an_old_id_directly(ctx: MockContext) -> None:
    for decision_id in range(1, 502):
        seed(ctx, decision_id)

    delivery = invoke(ctx, decision_list, interaction(query="1", limit=5))
    assert re.search(r"#1(?!\d)", rendered(delivery))
    assert "Showing 1–1 of 1" in rendered(delivery)


def test_limit_one_pagination_can_reach_page_five_hundred(ctx: MockContext) -> None:
    for decision_id in range(1, 501):
        seed(ctx, decision_id)

    first = invoke(ctx, decision_list, interaction(limit=1))
    next_id = button_custom_id(first, "Next")
    payload = next_id.removeprefix(LIST_COMPONENT_PREFIX)
    token, separator, _ = payload.rpartition(":")
    assert separator and token

    last = invoke(ctx, decision_list_component, component(f"{LIST_COMPONENT_PREFIX}{token}:500"))
    assert re.search(r"#1(?!\d)", embed_description(last))
    assert "Showing 500–500 of 500" in rendered(last)


def test_status_filtered_pages_keep_the_original_result_snapshot(ctx: MockContext) -> None:
    for decision_id in range(1, 4):
        seed(ctx, decision_id)

    first = invoke(ctx, decision_list, interaction(status="open", limit=1))
    next_id = button_custom_id(first, "Next")
    payload = next_id.removeprefix(LIST_COMPONENT_PREFIX)
    token, separator, _ = payload.rpartition(":")
    assert separator and token

    second = ctx.kv.get(decision_key(2))
    ctx.kv.set(
        decision_key(2),
        close_record(
            second,
            actor_id=AUTHOR_ID,
            outcome="Closed after page one rendered",
            closed_at="2026-07-12T18:30:00+00:00",
        ),
    )

    page_two = invoke(
        cast(Any, ctx),
        decision_list_component,
        component(f"{LIST_COMPONENT_PREFIX}{token}:2"),
    )
    description = embed_description(page_two)
    assert re.search(r"#2(?!\d)", description)
    assert not re.search(r"#1(?!\d)", description)


def test_dashboard_page_mode_has_no_silent_page_four_hundred_clamp(
    ctx: MockContext,
) -> None:
    for decision_id in range(1, 502):
        seed(ctx, decision_id)
    assert status_counts(cast(Any, ctx)) == (501, 0)

    result = get_recent_decisions(
        cast(Any, ctx),
        {"discord_srv_id": GUILD_ID, "page": 501, "page_size": 1},
    )
    assert result["total"] == 501
    assert [row["id"] for row in result["rows"]] == [1]


def test_one_page_list_does_not_consume_a_pagination_state_key(ctx: MockContext) -> None:
    seed(ctx, 1)
    invoke(ctx, decision_list, interaction(limit=10))
    assert ctx.kv.count(prefix=LIST_STATE_PREFIX) == 0


def test_list_controls_are_bound_to_the_requesting_user_and_channel(
    ctx: MockContext,
) -> None:
    seed(ctx, 1)
    seed(ctx, 2)
    first = invoke(ctx, decision_list, interaction(limit=1))
    next_id = button_custom_id(first, "Next")

    foreign_user = invoke(
        ctx,
        decision_list_component,
        component(next_id, user_id=OTHER_ID),
    )
    assert foreign_user.get("ephemeral") is True
    assert not foreign_user.get("embeds")
    assert not foreign_user.get("update_message", False)

    foreign_channel = invoke(
        ctx,
        decision_list_component,
        component(next_id, channel_id=CHANNEL_B),
    )
    assert foreign_channel.get("ephemeral") is True
    assert not foreign_channel.get("embeds")
    assert not foreign_channel.get("update_message", False)


def test_add_submission_defers_before_storage_and_uses_followup(
    ctx: MockContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = len(ctx.interaction.defers)
    install_defer_guard(ctx, monkeypatch, baseline=baseline)
    event = modal(
        "decision:add",
        title="Deferred add",
        choice="Ship it",
        reason="The checks passed",
        tags="release",
    )

    delivery = invoke(ctx, decision_add_modal, event)
    assert len(ctx.interaction.defers) == baseline + 1
    assert ctx.interaction.followups[-1] is delivery
    assert delivery.get("embeds")
    assert delivery.get("allowed_mentions") == {"parse": []}


def test_close_submission_defers_before_storage_and_uses_followup(
    ctx: MockContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(ctx, 1)
    decision_close_component(cast(Any, ctx), component("decision:close:1"))
    close_modal = ctx.interaction.modals_sent[-1]
    outcome_field = close_modal["fields"][0].custom_id
    baseline = len(ctx.interaction.defers)
    install_defer_guard(ctx, monkeypatch, baseline=baseline)

    delivery = invoke(
        ctx,
        decision_close_modal,
        modal(close_modal["custom_id"], **{outcome_field: "Completed successfully"}),
    )
    assert len(ctx.interaction.defers) == baseline + 1
    assert ctx.interaction.followups[-1] is delivery
    assert delivery.get("embeds")
    assert delivery.get("allowed_mentions") == {"parse": []}


def test_list_defers_before_storage_and_uses_followup(
    ctx: MockContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(ctx, 1)
    baseline = len(ctx.interaction.defers)
    install_defer_guard(ctx, monkeypatch, baseline=baseline)

    delivery = invoke(ctx, decision_list, interaction())
    assert len(ctx.interaction.defers) == baseline + 1
    assert ctx.interaction.followups[-1] is delivery
    assert delivery.get("embeds")
    assert delivery.get("allowed_mentions") == {"parse": []}


def test_view_defers_before_storage_and_uses_followup(
    ctx: MockContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(ctx, 1)
    baseline = len(ctx.interaction.defers)
    install_defer_guard(ctx, monkeypatch, baseline=baseline)

    delivery = invoke(ctx, decision_view, interaction(id=1))
    assert len(ctx.interaction.defers) == baseline + 1
    assert ctx.interaction.followups[-1] is delivery
    assert delivery.get("embeds")


def test_pagination_component_defers_before_snapshot_reads(
    ctx: MockContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(ctx, 1)
    seed(ctx, 2)
    first = invoke(ctx, decision_list, interaction(limit=1))
    next_id = button_custom_id(first, "Next")
    baseline = len(ctx.interaction.defers)
    install_defer_guard(ctx, monkeypatch, baseline=baseline)

    delivery = invoke(ctx, decision_list_component, component(next_id))
    assert len(ctx.interaction.defers) == baseline + 1
    assert ctx.interaction.followups[-1] is delivery
    assert delivery.get("ephemeral") is True


def test_close_form_is_shown_before_verification_state_uses_kv(
    ctx: MockContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(ctx, 1)
    baseline = len(ctx.interaction.modals_sent)
    for method_name in ("exists", "count", "set"):
        original = getattr(ctx.kv, method_name)

        def guarded(
            *args: Any,
            _method_name: str = method_name,
            _original: Callable[..., Any] = original,
            **kwargs: Any,
        ) -> Any:
            assert len(ctx.interaction.modals_sent) > baseline, (
                f"KV {_method_name} ran before the close modal was sent"
            )
            return _original(*args, **kwargs)

        monkeypatch.setattr(ctx.kv, method_name, guarded)

    decision_close_component(cast(Any, ctx), component("decision:close:1"))
    assert len(ctx.interaction.modals_sent) == baseline + 1


def test_storage_quota_error_explains_capacity_and_next_action(
    ctx: MockContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_set = ctx.kv.set
    original_set_many = ctx.kv.set_many

    def quota_set(key: str, value: Any, **kwargs: Any) -> None:
        if key.startswith(DECISION_PREFIX):
            raise KvQuotaError("key quota reached")
        original_set(key, value, **kwargs)

    def quota_set_many(entries: dict[str, Any]) -> None:
        if any(key.startswith(DECISION_PREFIX) for key in entries):
            raise KvQuotaError("key quota reached")
        original_set_many(entries)

    monkeypatch.setattr(ctx.kv, "set", quota_set)
    monkeypatch.setattr(ctx.kv, "set_many", quota_set_many)
    event = modal(
        "decision:add",
        title="Over capacity",
        choice="Try to save",
        reason="Exercise quota handling",
        tags="quota",
    )

    delivery = invoke(ctx, decision_add_modal, event)
    text = rendered(delivery).lower()
    assert "storage" in text
    assert any(term in text for term in ("capacity", "full", "limit", "quota", "10,000"))
    assert any(term in text for term in ("administrator", "admin", "remove", "export", "contact"))
    assert delivery.get("allowed_mentions") == {"parse": []}
    assert ctx.kv.count(prefix=DECISION_PREFIX) == 0


def test_modal_and_help_copy_explain_tag_limits_and_immutability(ctx: MockContext) -> None:
    decision_command(cast(Any, ctx), subcommand("add"))
    fields = ctx.interaction.modals_sent[-1]["fields"]
    tags_field = next(field for field in fields if field.custom_id == "tags")
    tag_copy = f"{tags_field.label} {tags_field.placeholder}".lower()
    assert "5" in tag_copy
    assert "24" in tag_copy

    help_delivery = invoke(ctx, decision_help, interaction())
    help_copy = rendered(help_delivery).lower()
    assert "immutable" in help_copy
    assert "five" in help_copy or "5" in help_copy
    assert "24" in help_copy
