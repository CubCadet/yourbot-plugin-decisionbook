from __future__ import annotations

from copy import deepcopy

import pytest

from core import (
    InputError,
    close_record,
    decision_embed,
    display_text,
    list_embed,
    make_record,
    normalize_text,
    parse_decision_id,
    parse_record,
)


def base():
    return make_record(
        1,
        title="Title",
        choice="Choice",
        reason="Reason",
        tags="one,two",
        author_id="42",
        created_at="2026-07-11T00:00:00+00:00",
    )


def test_regression_unknown_same_schema_fields_survive_closure():
    item = base()
    item["migration_hint"] = {"source": "future"}
    closed = close_record(item, actor_id="42", outcome="Done")
    assert closed["migration_hint"] == {"source": "future"}


def test_regression_future_schema_never_renders_as_current():
    item = base()
    item["schema_version"] = 2
    assert parse_record(item) is None


def test_regression_embed_rendering_does_not_mutate_nested_record():
    item = base()
    before = deepcopy(item)
    decision_embed(item)
    assert item == before


def test_regression_closed_record_requires_complete_closure():
    item = base()
    item["status"] = "closed"
    item["closure"] = {"outcome": "Done"}
    assert parse_record(item) is None


@pytest.mark.parametrize("value", [True, 1.0, 1.9, float("inf"), float("nan")])
def test_regression_decision_id_never_coerces_bool_or_float(value):
    with pytest.raises(InputError):
        parse_decision_id(value)


def test_regression_parse_record_handles_infinity_without_raising():
    item = base()
    item["id"] = float("inf")
    assert parse_record(item) is None


def test_regression_joined_family_emoji_remains_joined():
    family = "👨‍👩‍👧‍👦"
    assert normalize_text(family) == family
    assert family in display_text(family)


def test_regression_markdown_payload_cannot_format_embed_content():
    item = base()
    item.update(
        title="**Admin notice**",
        choice="[click me](https://example.test)",
        reason="||hidden|| `code` <@123>",
    )
    embed = decision_embed(item)
    assert r"\*\*Admin notice\*\*" in embed["title"]
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields["Decision"] == r"\[click me\]\(https://example.test\)"
    assert fields["Reason"] == r"\|\|hidden\|\| \`code\` \<@123\>"


def test_regression_embed_uses_resolved_identity_and_localized_timestamp_tokens():
    embed = decision_embed(base())
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields["Status"] == "Open"
    assert fields["Recorded by"] == "<@42>"
    assert fields["Recorded"].startswith("<t:")
    assert fields["Recorded"].endswith(":F>")
    assert "42" not in embed["footer"]["text"]
    assert "2026-" not in embed["footer"]["text"]


def test_regression_list_has_text_status_not_icon_only():
    embed = list_embed([base()], total=1)
    assert "📖 **Open" in embed["description"]
    assert "Showing 1–1 of 1" in embed["footer"]["text"]


def test_regression_noncanonical_tags_are_not_rendered_from_storage():
    item = base()
    item["tags"] = ["safe", "`injection`"]
    assert parse_record(item) is None


def test_regression_international_tags_are_not_erased():
    item = make_record(
        2,
        title="International release",
        choice="Ship",
        reason="Ready",
        tags="Équipe, 開発",
        author_id="42",
        created_at="2026-07-11T00:00:00Z",
    )
    assert item["tags"] == ["équipe", "開発"]
    assert parse_record(item)["tags"] == ["équipe", "開発"]


def test_regression_detailed_reason_keeps_paragraphs_in_storage_and_embed():
    item = make_record(
        2,
        title="Structured reasoning",
        choice="Option A\nOption B was declined",
        reason="Constraint one\n\nConstraint two",
        tags="",
        author_id="42",
        created_at="2026-07-11T00:00:00Z",
    )
    assert item["reason"] == "Constraint one\n\nConstraint two"
    fields = {field["name"]: field["value"] for field in decision_embed(item)["fields"]}
    assert fields["Decision"] == "Option A\nOption B was declined"
    assert fields["Reason"] == "Constraint one\n\nConstraint two"


def test_regression_closed_by_must_match_original_author():
    item = base()
    item["status"] = "closed"
    item["closure"] = {
        "outcome": "Done",
        "closed_by": "43",
        "closed_at": "2026-07-12T00:00:00Z",
    }
    assert parse_record(item) is None


def test_regression_naive_or_backdated_closure_is_rejected():
    item = base()
    for timestamp in ("2026-07-12T00:00:00", "2026-07-10T00:00:00Z"):
        item["status"] = "closed"
        item["closure"] = {"outcome": "Done", "closed_by": "42", "closed_at": timestamp}
        assert parse_record(item) is None
