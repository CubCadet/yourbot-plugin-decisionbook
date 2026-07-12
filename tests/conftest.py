from __future__ import annotations

import sys
from pathlib import Path

import pytest
from yourbot_sdk.testing import MockContext, make_event

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AUTHOR_ID = "100000000000000001"
OTHER_ID = "100000000000000002"
CHANNEL_ID = "1"
OTHER_CHANNEL_ID = "2"


@pytest.fixture
def ctx() -> MockContext:
    return MockContext(
        server_id="100000000000000099",
        plugin_id="decisionbook",
        version="0.3.1",
        capabilities=["interaction:respond", "storage:kv"],
    )


def interaction(
    command: str = "decision",
    *,
    user_id: str = AUTHOR_ID,
    channel_id: str = CHANNEL_ID,
    **options,
):
    return make_event(
        "interaction_create",
        command_name=command,
        user_id=user_id,
        channel_id=channel_id,
        options=[{"name": key, "value": value} for key, value in options.items()],
    )


def subcommand(
    name: str,
    *,
    user_id: str = AUTHOR_ID,
    channel_id: str = CHANNEL_ID,
    option_key: str = "options",
    **options,
):
    nested = [
        {
            "name": name,
            "type": 1,
            "options": [
                {
                    "name": key,
                    "type": 4 if isinstance(value, int) else 3,
                    "value": value,
                }
                for key, value in options.items()
            ],
        }
    ]
    return make_event(
        "interaction_create",
        command_name="decision",
        user_id=user_id,
        channel_id=channel_id,
        **{option_key: nested},
    )


def modal(
    custom_id: str,
    *,
    user_id: str = AUTHOR_ID,
    channel_id: str = CHANNEL_ID,
    **values,
):
    return make_event(
        "interaction_create",
        interaction_type=5,
        custom_id=custom_id,
        user_id=user_id,
        channel_id=channel_id,
        modal_values=values,
    )


def component(
    custom_id: str,
    *,
    user_id: str = AUTHOR_ID,
    channel_id: str = CHANNEL_ID,
):
    return make_event(
        "interaction_create",
        interaction_type=3,
        custom_id=custom_id,
        user_id=user_id,
        channel_id=channel_id,
    )


def invoke(ctx: MockContext, handler, *args, **kwargs):
    """Call an interaction handler and return the delivery it produced."""
    response_count = len(ctx.interaction.responses)
    followup_count = len(ctx.interaction.followups)
    handler(ctx, *args, **kwargs)
    if len(ctx.interaction.followups) > followup_count:
        return ctx.interaction.followups[-1]
    if len(ctx.interaction.responses) > response_count:
        return ctx.interaction.responses[-1]
    raise AssertionError("The interaction handler did not deliver a response or follow-up.")
