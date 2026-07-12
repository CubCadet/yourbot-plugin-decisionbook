from __future__ import annotations

from typing import Any

from conftest import AUTHOR_ID, subcommand
from yourbot_sdk import ActionRow, Button, Context, TextInput
from yourbot_sdk._context import _InteractionApi
from yourbot_sdk._transport import Transport

from decisionbook import decision_command


class SpyTransport(Transport):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        return {}


def interaction_api(interaction_id: str) -> tuple[SpyTransport, _InteractionApi]:
    transport = SpyTransport()
    context = Context(
        server_id="server-1",
        plugin_id="decisionbook",
        version="test",
        capabilities=["interaction:respond"],
        transport=transport,
    )
    context._current_interaction_id = interaction_id
    return transport, context.interaction


def test_component_builders_emit_valid_discord_shapes():
    row = ActionRow(
        Button("Previous", custom_id="decision:page:token:1", style="secondary"),
        Button("Next", custom_id="decision:page:token:2", style="primary"),
    ).to_dict()
    assert row["type"] == 1
    assert [item["type"] for item in row["components"]] == [2, 2]
    assert [item["style"] for item in row["components"]] == [2, 1]
    assert all(len(item["custom_id"]) <= 100 for item in row["components"])


def test_real_sdk_serializes_components_and_mention_policy():
    transport, api = interaction_api("interaction-1")
    api.respond(
        embeds=[{"title": "Decision"}],
        components=[ActionRow(Button("Close", custom_id="decision:close:1"))],
        allowed_mentions={"parse": []},
        update_message=True,
    )
    method, payload = transport.calls[-1]
    assert method == "interaction.respond"
    assert payload["_interaction_id"] == "interaction-1"
    assert payload["components"][0]["type"] == 1
    assert payload["components"][0]["components"][0]["custom_id"] == "decision:close:1"
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["update_message"] is True


def test_real_sdk_wraps_bare_modal_fields_in_action_rows():
    transport, api = interaction_api("interaction-2")
    api.send_modal(
        title="Record a decision",
        custom_id="decision:add",
        fields=[
            TextInput("Title", "title", min_length=1, max_length=100),
            TextInput("Reason", "reason", style="paragraph", max_length=1000),
        ],
    )
    method, payload = transport.calls[-1]
    assert method == "interaction.send_modal"
    assert payload["custom_id"] == "decision:add"
    assert len(payload["components"]) == 2
    assert all(row["type"] == 1 for row in payload["components"])
    assert all(row["components"][0]["type"] == 4 for row in payload["components"])


def test_each_root_action_makes_one_initial_action(ctx):
    decision_command(ctx, subcommand("add", user_id=AUTHOR_ID))
    assert len(ctx.interaction.modals_sent) == 1
    assert len(ctx.interaction.responses) == 0

    second = type(ctx)(
        server_id=ctx.server_id,
        plugin_id=ctx.plugin_id,
        version=ctx.version,
        capabilities=ctx.capabilities,
    )
    decision_command(second, subcommand("help", user_id=AUTHOR_ID))
    assert len(second.interaction.responses) == 1
    assert len(second.interaction.modals_sent) == 0
