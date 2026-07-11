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


@pytest.fixture
def ctx() -> MockContext:
    return MockContext(
        server_id="100000000000000099",
        plugin_id="decisionbook",
        version="0.2.0",
        capabilities=["interaction:respond", "storage:kv"],
    )


def interaction(command: str = "decision", *, user_id: str = AUTHOR_ID, **options):
    return make_event(
        "interaction_create",
        command_name=command,
        user_id=user_id,
        options=[{"name": key, "value": value} for key, value in options.items()],
    )


def subcommand(name: str, *, user_id: str = AUTHOR_ID, option_key: str = "options", **options):
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
        **{option_key: nested},
    )


def modal(custom_id: str, *, user_id: str = AUTHOR_ID, **values):
    return make_event(
        "interaction_create",
        interaction_type=5,
        custom_id=custom_id,
        user_id=user_id,
        modal_values=values,
    )


def component(custom_id: str, *, user_id: str = AUTHOR_ID):
    return make_event(
        "interaction_create",
        interaction_type=3,
        custom_id=custom_id,
        user_id=user_id,
    )
