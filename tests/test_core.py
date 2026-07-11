from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from core import (
    SCHEMA_VERSION,
    InputError,
    close_record,
    decision_embed,
    decision_key,
    discord_timestamp,
    discord_user_mention,
    display_multiline_text,
    display_text,
    escape_discord_markdown,
    filter_records,
    list_embed,
    make_record,
    matching_records,
    normalize_multiline_text,
    normalize_text,
    parse_decision_id,
    parse_discord_id,
    parse_record,
    parse_rfc3339,
    parse_tags,
    record_matches,
    validate_field,
)


def record(decision_id: int = 1, **overrides):
    data = make_record(
        decision_id,
        title="Event time",
        choice="Saturday at 8 PM",
        reason="Best regional overlap",
        tags="events, scheduling",
        author_id="42",
        created_at="2026-07-11T18:30:00+00:00",
    )
    data.update(overrides)
    return data


def test_nfkc_normalization():
    assert normalize_text("ＡＢＣ １２３") == "ABC 123"


def test_control_bidi_and_zero_width_are_removed():
    assert normalize_text("a\x00\u202eb\u200bc\ufeffd") == "abcd"


def test_whitespace_collapses_and_emoji_survives():
    assert normalize_text("  hello\n\t world  🎉 ") == "hello world 🎉"


def test_joined_emoji_survives_while_other_format_controls_do_not():
    family = "👨‍👩‍👧‍👦"
    assert normalize_text(f" {family} \u2066hidden\u2069 ") == f"{family} hidden"
    assert normalize_text("a\u200db") == "ab"


def test_multiline_normalization_preserves_bounded_paragraph_structure():
    source = "  First line  \r\nsecond\tline\n\n\n  Final 👨‍👩‍👧‍👦  "
    expected = "First line\nsecond line\n\nFinal 👨‍👩‍👧‍👦"
    assert normalize_multiline_text(source) == expected
    assert normalize_text(source) == "First line second line Final 👨‍👩‍👧‍👦"


def test_multiline_fields_are_bounded_but_single_line_fields_collapse_lines():
    assert validate_field("choice", "one\ntwo") == "one\ntwo"
    assert validate_field("title", "one\ntwo") == "one two"
    with pytest.raises(InputError, match="too many lines"):
        validate_field("reason", "\n".join(f"line {index}" for index in range(13)))


def test_broadcast_mentions_are_neutralized_for_display():
    output = display_text("Hi @everyone and @HERE")
    assert "@everyone" not in output.lower()
    assert "@here" not in output.lower()
    assert "＠everyone" in output.lower()


def test_discord_markdown_is_escaped_for_display():
    source = r"\\ **bold** _under_ `code` ||spoiler|| [link](https://example.test) <@42>"
    output = display_text(source)
    for token in (r"\*", r"\_", r"\`", r"\|", r"\[", r"\(", r"\<"):
        assert token in output
    assert escape_discord_markdown("plain") == "plain"


@pytest.mark.parametrize("field", ["title", "choice", "reason", "outcome"])
def test_empty_required_fields_rejected(field):
    with pytest.raises(InputError):
        validate_field(field, " \u200b ")


@pytest.mark.parametrize(
    "field,maximum",
    [("title", 100), ("choice", 500), ("reason", 1000), ("outcome", 500)],
)
def test_exact_limits_accepted_and_over_limit_rejected(field, maximum):
    assert len(validate_field(field, "x" * maximum)) == maximum
    with pytest.raises(InputError):
        validate_field(field, "x" * (maximum + 1))


def test_tags_normalize_dedupe_and_preserve_order():
    assert parse_tags(" Staff Team, staff_team, Release!!,  QA  ") == [
        "staff-team",
        "release",
        "qa",
    ]


def test_tags_preserve_canonical_international_letters_and_numbers():
    assert parse_tags("Équipe, 開発, हिंदी, équipe") == ["équipe", "開発", "हिंदी"]


def test_invalid_tags_and_tag_count_rejected():
    with pytest.raises(InputError):
        parse_tags("!!!, 🌟")
    with pytest.raises(InputError):
        parse_tags("one,two,three,four,five,six")


def test_long_tag_rejected():
    with pytest.raises(InputError):
        parse_tags("x" * 25)


def test_key_format_is_stable_and_padded():
    assert decision_key(1) == "decision:000000000001"
    assert decision_key(123) == "decision:000000000123"


@pytest.mark.parametrize("value", [1, 42, "1", " 0042 "])
def test_decision_id_accepts_only_integer_or_decimal_string(value):
    assert parse_decision_id(value) == int(value)


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        1.0,
        1.9,
        float("inf"),
        float("-inf"),
        float("nan"),
        None,
        "",
        "0",
        "-1",
        "+1",
        "1.0",
    ],
)
def test_decision_id_rejects_coercible_or_nonpositive_values(value):
    with pytest.raises(InputError, match="positive whole number"):
        parse_decision_id(value)


def test_discord_snowflakes_are_canonical_and_unsigned_64_bit():
    maximum = (1 << 64) - 1
    assert parse_discord_id(42) == "42"
    assert parse_discord_id(str(maximum)) == str(maximum)
    for invalid in (True, 1.0, "01", "0", "-1", str(maximum + 1), "author-1", None):
        with pytest.raises(InputError, match="valid user identity"):
            parse_discord_id(invalid)


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-11T18:30:00Z",
        "2026-07-11T18:30:00.123456+00:00",
        "2026-07-11T14:30:00-04:00",
    ],
)
def test_rfc3339_parser_accepts_aware_timestamps(value):
    assert parse_rfc3339(value) == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "2026-07-11T18:30:00",
        "2026-07-11 18:30:00+00:00",
        "2026-02-30T00:00:00Z",
        "2026-07-11T25:00:00Z",
    ],
)
def test_rfc3339_parser_rejects_naive_or_invalid_timestamps(value):
    with pytest.raises(InputError, match="invalid timestamp"):
        parse_rfc3339(value)


def test_make_record_has_version_and_aware_timestamp():
    item = make_record(1, title="T", choice="C", reason="R", tags="", author_id="42")
    assert item["schema_version"] == SCHEMA_VERSION
    assert datetime.fromisoformat(item["created_at"]).tzinfo is not None


def test_make_record_rejects_invalid_author_and_supplied_timestamp():
    with pytest.raises(InputError):
        make_record(1, title="T", choice="C", reason="R", tags="", author_id="not-a-snowflake")
    with pytest.raises(InputError):
        make_record(
            1,
            title="T",
            choice="C",
            reason="R",
            tags="",
            author_id="42",
            created_at="2026-07-11T18:30:00",
        )


def test_parser_rejects_malformed_and_future_records():
    assert parse_record(None) is None
    assert parse_record({"schema_version": 99}) is None
    broken = record()
    broken["title"] = ""
    assert parse_record(broken) is None
    oversized = record()
    oversized["reason"] = "x" * 1001
    assert parse_record(oversized) is None
    too_many_tags = record()
    too_many_tags["tags"] = ["a", "b", "c", "d", "e", "f"]
    assert parse_record(too_many_tags) is None


@pytest.mark.parametrize("bad_id", [True, 1.0, 1.9, float("inf"), "1", "1.0"])
def test_parser_rejects_non_integer_record_ids_without_raising(bad_id):
    item = record()
    item["id"] = bad_id
    assert parse_record(item) is None


def test_parser_normalizes_required_text_on_a_copy():
    original = record(title="  Team\u200b choice  ", choice="  ship\n now ", reason="  because  ")
    parsed = parse_record(original)
    assert parsed is not None
    assert parsed["title"] == "Team choice"
    assert parsed["choice"] == "ship\nnow"
    assert parsed["reason"] == "because"
    assert original["title"] == "  Team\u200b choice  "


@pytest.mark.parametrize(
    "tags",
    [["QA"], ["qa", "qa"], ["-qa"], ["qa-"], ["two--hyphens"], ["🎉"], [1]],
)
def test_parser_requires_canonical_unique_stored_tags(tags):
    item = record()
    item["tags"] = tags
    assert parse_record(item) is None


def test_parser_accepts_canonical_unicode_stored_tags():
    item = record()
    item["tags"] = ["équipe", "開発", "हिंदी"]
    assert parse_record(item)["tags"] == ["équipe", "開発", "हिंदी"]


@pytest.mark.parametrize("author", [True, 1, "01", "author-1", str(1 << 64)])
def test_parser_requires_canonical_stored_author_snowflake(author):
    item = record()
    item["author_id"] = author
    assert parse_record(item) is None


@pytest.mark.parametrize(
    "timestamp",
    ["2026-07-11T18:30:00", "2026-07-11 18:30:00+00:00", "not-a-date", 123],
)
def test_parser_requires_timezone_aware_rfc3339_creation_time(timestamp):
    item = record()
    item["created_at"] = timestamp
    assert parse_record(item) is None


def test_parser_rejects_boolean_schema_version():
    item = record()
    item["schema_version"] = True
    assert parse_record(item) is None


def test_parser_returns_copy():
    original = record()
    parsed = parse_record(original)
    parsed["title"] = "changed"
    assert original["title"] == "Event time"


def test_parser_preserves_unknown_same_schema_fields_and_nested_closure_fields():
    item = close_record(
        record(extra_future_field={"keep": True}),
        actor_id="42",
        outcome="Done",
        closed_at="2026-07-12T00:00:00Z",
    )
    item["closure"]["review_note"] = {"keep": True}
    parsed = parse_record(item)
    assert parsed is not None
    assert parsed["extra_future_field"] == {"keep": True}
    assert parsed["closure"]["review_note"] == {"keep": True}


def test_close_preserves_original_and_unknown_fields():
    original = record(extra_future_field={"keep": True})
    before = deepcopy(original)
    closed = close_record(
        original,
        actor_id="42",
        outcome="Attendance improved",
        closed_at="2026-07-12T00:00:00+00:00",
    )
    for key in (
        "title",
        "choice",
        "reason",
        "tags",
        "author_id",
        "created_at",
        "extra_future_field",
    ):
        assert closed[key] == before[key]
    assert original == before
    assert closed["closure"]["outcome"] == "Attendance improved"


def test_close_is_author_only_and_idempotent():
    original = record()
    with pytest.raises(PermissionError):
        close_record(original, actor_id="43", outcome="No")
    closed = close_record(
        original, actor_id="42", outcome="First", closed_at="2026-07-12T00:00:00+00:00"
    )
    again = close_record(closed, actor_id="42", outcome="Replacement")
    assert again["closure"]["outcome"] == "First"


def test_close_rejects_invalid_actor_and_impossible_chronology():
    original = record()
    with pytest.raises(InputError, match="valid user identity"):
        close_record(original, actor_id="not-a-user", outcome="No")
    with pytest.raises(InputError, match="valid closure timestamp"):
        close_record(
            original,
            actor_id="42",
            outcome="No",
            closed_at="2026-07-10T00:00:00+00:00",
        )


@pytest.mark.parametrize(
    "closure",
    [
        None,
        {"outcome": "", "closed_by": "42", "closed_at": "2026-07-12T00:00:00Z"},
        {"outcome": "Done", "closed_by": "43", "closed_at": "2026-07-12T00:00:00Z"},
        {"outcome": "Done", "closed_by": "42", "closed_at": "2026-07-10T00:00:00Z"},
        {"outcome": "Done", "closed_by": "42", "closed_at": "not-a-date"},
    ],
)
def test_parser_rejects_invalid_closed_record_relationships(closure):
    item = record(status="closed", closure=closure)
    assert parse_record(item) is None


def test_search_covers_id_text_and_tags_case_insensitively():
    item = record()
    assert record_matches(item, "EVENT")
    assert record_matches(item, "regional")
    assert record_matches(item, "scheduling")
    assert record_matches(item, "1")


def test_filtering_is_newest_first_bounded_and_status_aware():
    open_one = record(1)
    closed_two = close_record(
        record(2), actor_id="42", outcome="Done", closed_at="2026-07-12T00:00:00+00:00"
    )
    newest = record(3, title="Newest")
    assert [x["id"] for x in filter_records([open_one, closed_two, newest], limit=2)] == [3, 2]
    assert [x["id"] for x in filter_records([open_one, closed_two, newest], status="closed")] == [2]
    with pytest.raises(InputError):
        filter_records([open_one], status="invalid")


def test_filtering_supports_offset_and_page_without_expanding_page_size():
    items = [record(index) for index in range(1, 26)]
    assert [item["id"] for item in filter_records(items, limit=10, offset=10)] == list(
        range(15, 5, -1)
    )
    assert [item["id"] for item in filter_records(items, limit=99, page=3)] == list(range(5, 0, -1))
    with pytest.raises(InputError, match="either page or offset"):
        filter_records(items, limit=5, offset=5, page=2)
    with pytest.raises(InputError):
        filter_records(items, limit=1.5)


def test_matching_records_returns_full_sorted_valid_set_for_exact_totals():
    malformed = record(99, title="")
    items = [record(index, title=f"Release {index}") for index in range(1, 14)]
    matches = matching_records([malformed, *items], query="release", status="open")
    assert len(matches) == 13
    assert [item["id"] for item in matches] == list(range(13, 0, -1))


def test_display_truncation_does_not_mutate_record():
    item = record(choice="x" * 500)
    before = deepcopy(item)
    assert display_text(item["choice"], 20).endswith("…")
    decision_embed(item)
    assert item == before


def test_display_truncation_honors_tiny_limits_exactly():
    assert display_text("abc", 0) == ""
    assert display_text("abc", 1) == "…"
    assert len(display_text("***", 3)) <= 3


def test_embed_stays_within_discord_field_limits():
    item = record(title="*" * 100, choice="_" * 500, reason="`" * 1000)
    embed = decision_embed(item)
    assert len(embed["title"]) <= 256
    assert all(len(field["value"]) <= 1024 for field in embed["fields"])


def test_embed_uses_non_pingable_mentions_localized_times_and_text_status():
    item = record(created_at="2026-07-11T18:30:00Z")
    embed = decision_embed(item)
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    unix = int(datetime(2026, 7, 11, 18, 30, tzinfo=UTC).timestamp())
    assert fields["Status"] == "Open"
    assert fields["Recorded by"] == "<@42>"
    assert fields["Recorded"] == f"<t:{unix}:F>"
    assert discord_user_mention("42") == "<@42>"
    assert discord_timestamp("2026-07-11T18:30:00Z", "R") == f"<t:{unix}:R>"


def test_embed_preserves_prose_lines_and_escapes_line_markdown():
    item = record(choice="First\n- unsafe list", reason="Paragraph one\n\n> quote")
    embed = decision_embed(item)
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields["Decision"] == "First\n\\- unsafe list"
    assert fields["Reason"] == "Paragraph one\n\n\\> quote"
    assert display_multiline_text("one\n**two**") == "one\n\\*\\*two\\*\\*"


def test_list_embed_has_text_status_identity_time_and_page_metadata():
    items = [
        record(3),
        close_record(
            record(2),
            actor_id="42",
            outcome="Done",
            closed_at="2026-07-12T00:00:00+00:00",
        ),
    ]
    embed = list_embed(items, offset=10, total=27)
    assert "**Open · #3" in embed["description"]
    assert "**Closed · #2" in embed["description"]
    assert "<@42>" in embed["description"]
    assert "<t:" in embed["description"]
    assert "Showing 11–12 of 27" in embed["footer"]["text"]
    assert "/decision view" in embed["footer"]["text"]
    assert "/decision-view" not in embed["footer"]["text"]
