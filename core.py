"""Pure DecisionBook domain rules: normalization, validation, schema, and rendering."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = 1
KEY_WIDTH = 12
SCAN_LIMIT = 500
MAX_TAGS = 5
MAX_MULTILINE_LINES = 12
LIMITS = {
    "title": 100,
    "choice": 500,
    "reason": 1000,
    "outcome": 500,
    "tags": 150,
    "query": 80,
}

OPEN_COLOR = 0xC99A2E
CLOSED_COLOR = 0x2D7A5E
INK_COLOR = 0x172033
DISCORD_SNOWFLAKE_MAX = (1 << 64) - 1

_BIDI_AND_ZERO_WIDTH = {
    "\u061c",
    "\u200b",
    "\u200c",
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2060",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
    "\ufeff",
}
_TAG_SEPARATORS = re.compile(r"[\s_]+")
_MULTI_HYPHEN = re.compile(r"-+")
_POSITIVE_INTEGER = re.compile(r"[0-9]+")
_DISCORD_SNOWFLAKE = re.compile(r"[1-9][0-9]{0,19}")
_RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_DISCORD_MARKDOWN = re.compile(r"([\\`*_~|\[\]()<>{}#])")
_DISCORD_LINE_MARKER = re.compile(r"(?m)^(\s*)([-+])(?=\s)")
_MULTILINE_FIELDS = {"choice", "reason", "outcome"}


class InputError(ValueError):
    """A safe, user-facing validation error."""


def utc_now() -> str:
    """Return an RFC 3339-compatible, timezone-aware UTC timestamp."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _emoji_base(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2300 <= codepoint <= 0x23FF
        or 0x2600 <= codepoint <= 0x27BF
        or 0x2B00 <= codepoint <= 0x2BFF
    )


def _joined_emoji_zwj(text: str, index: int) -> bool:
    left = index - 1
    right = index + 1
    while left >= 0 and (
        unicodedata.category(text[left]).startswith("M") or 0x1F3FB <= ord(text[left]) <= 0x1F3FF
    ):
        left -= 1
    while right < len(text) and unicodedata.category(text[right]).startswith("M"):
        right += 1
    return left >= 0 and right < len(text) and _emoji_base(text[left]) and _emoji_base(text[right])


def _normalized_characters(value: Any, *, multiline: bool) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    if multiline:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\u2028", "\n").replace("\u2029", "\n")
    cleaned: list[str] = []
    for index, char in enumerate(text):
        if multiline and char == "\n":
            cleaned.append(char)
            continue
        if not multiline and char in {"\r", "\n"}:
            cleaned.append(" ")
            continue
        if char == "\t":
            cleaned.append(" ")
            continue
        if char in _BIDI_AND_ZERO_WIDTH:
            continue
        if char == "\u200d":
            if _joined_emoji_zwj(text, index):
                cleaned.append(char)
            continue
        category = unicodedata.category(char)
        if category in {"Cc", "Cf"}:
            continue
        cleaned.append(char)
    return "".join(cleaned)


def normalize_text(value: Any) -> str:
    """Normalize a single-line value while retaining international text and emoji."""
    return " ".join(_normalized_characters(value, multiline=False).split())


def normalize_multiline_text(value: Any) -> str:
    """Normalize bounded prose while preserving useful lines and paragraph breaks."""
    raw_lines = _normalized_characters(value, multiline=True).split("\n")
    lines: list[str] = []
    for raw_line in raw_lines:
        line = " ".join(raw_line.split())
        if line or (lines and lines[-1]):
            lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def neutralize_broadcast_mentions(value: str) -> str:
    """Defense in depth; allowed_mentions remains the primary ping control."""
    return re.sub(r"@(everyone|here)\b", r"＠\1", value, flags=re.IGNORECASE)


def escape_discord_markdown(value: str) -> str:
    """Escape user-controlled Discord markdown without escaping safe Unicode."""
    escaped = _DISCORD_MARKDOWN.sub(r"\\\1", value)
    return _DISCORD_LINE_MARKER.sub(r"\1\\\2", escaped)


def validate_field(name: str, value: Any, *, required: bool = True) -> str:
    text = normalize_multiline_text(value) if name in _MULTILINE_FIELDS else normalize_text(value)
    if required and not text:
        raise InputError(f"{name.capitalize()} is required and cannot be blank.")
    maximum = LIMITS[name]
    if len(text) > maximum:
        raise InputError(
            f"{name.capitalize()} is too long ({len(text)} characters). "
            f"Keep it to {maximum} characters or fewer."
        )
    if name in _MULTILINE_FIELDS and text.count("\n") + 1 > MAX_MULTILINE_LINES:
        raise InputError(
            f"{name.capitalize()} has too many lines. "
            f"Keep it to {MAX_MULTILINE_LINES} lines or fewer."
        )
    return text


def _canonicalize_tag(value: Any) -> str:
    source = normalize_text(value).lower()
    characters: list[str] = []
    for char in source:
        category = unicodedata.category(char)[0]
        valid_mark = category == "M" and characters and characters[-1] != "-"
        if category in {"L", "N"} or char == "-" or valid_mark:
            characters.append(char)
        elif char == "_" or char.isspace():
            characters.append("-")
    tag = _MULTI_HYPHEN.sub("-", "".join(characters)).strip("-")
    if not any(unicodedata.category(char)[0] in {"L", "N"} for char in tag):
        return ""
    return tag


def parse_tags(value: Any) -> list[str]:
    raw = validate_field("tags", value, required=False)
    if not raw:
        return []
    tags: list[str] = []
    for candidate in raw.split(","):
        tag = _canonicalize_tag(_TAG_SEPARATORS.sub("-", normalize_text(candidate)))
        if not tag:
            continue
        if len(tag) > 24:
            raise InputError(
                f"Tag '{tag[:24]}…' is too long. Tags may contain at most 24 characters."
            )
        if tag not in tags:
            tags.append(tag)
    if not tags:
        raise InputError("Tags were supplied, but none contained usable letters or numbers.")
    if len(tags) > MAX_TAGS:
        raise InputError(f"Use at most {MAX_TAGS} tags.")
    return tags


def parse_decision_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise InputError("Decision ID must be a positive whole number.")
    if isinstance(value, int):
        decision_id = value
    else:
        candidate = value.strip()
        if not _POSITIVE_INTEGER.fullmatch(candidate):
            raise InputError("Decision ID must be a positive whole number.")
        decision_id = int(candidate)
    if decision_id < 1:
        raise InputError("Decision ID must be a positive whole number.")
    return decision_id


def decision_key(decision_id: int) -> str:
    return f"decision:{parse_decision_id(decision_id):0{KEY_WIDTH}d}"


def parse_discord_id(value: Any) -> str:
    """Return a canonical unsigned 64-bit Discord snowflake string."""
    if isinstance(value, bool):
        raise InputError("Discord did not provide a valid user identity. Please try again.")
    if isinstance(value, int):
        candidate = str(value)
    elif isinstance(value, str):
        candidate = value.strip()
    else:
        raise InputError("Discord did not provide a valid user identity. Please try again.")
    if not _DISCORD_SNOWFLAKE.fullmatch(candidate):
        raise InputError("Discord did not provide a valid user identity. Please try again.")
    if int(candidate) > DISCORD_SNOWFLAKE_MAX:
        raise InputError("Discord did not provide a valid user identity. Please try again.")
    return candidate


def parse_rfc3339(value: Any) -> str:
    """Validate and return a timezone-aware RFC 3339 timestamp."""
    if not isinstance(value, str) or _RFC3339.fullmatch(value) is None:
        raise InputError("DecisionBook received an invalid timestamp. Please try again.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError("DecisionBook received an invalid timestamp. Please try again.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InputError("DecisionBook received an invalid timestamp. Please try again.")
    return value


def make_record(
    decision_id: int,
    *,
    title: Any,
    choice: Any,
    reason: Any,
    tags: Any,
    author_id: Any,
    created_at: str | None = None,
) -> dict[str, Any]:
    author = parse_discord_id(author_id)
    timestamp = parse_rfc3339(created_at or utc_now())
    return {
        "schema_version": SCHEMA_VERSION,
        "id": parse_decision_id(decision_id),
        "title": validate_field("title", title),
        "choice": validate_field("choice", choice),
        "reason": validate_field("reason", reason),
        "tags": parse_tags(tags),
        "status": "open",
        "author_id": author,
        "created_at": timestamp,
        "closure": None,
    }


def _stored_text(record: dict[str, Any], name: str) -> str:
    value = record.get(name)
    if not isinstance(value, str):
        raise InputError(f"Stored {name} is invalid.")
    return validate_field(name, value)


def _stored_tags(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_TAGS:
        raise InputError("Stored tags are invalid.")
    tags: list[str] = []
    for value_tag in value:
        if (
            not isinstance(value_tag, str)
            or len(value_tag) > 24
            or _canonicalize_tag(value_tag) != value_tag
        ):
            raise InputError("Stored tags are invalid.")
        if value_tag in tags:
            raise InputError("Stored tags are invalid.")
        tags.append(value_tag)
    return tags


def _parse_timestamp(value: Any) -> tuple[str, datetime]:
    timestamp = parse_rfc3339(value)
    return timestamp, datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _stored_closure(
    value: Any,
    *,
    author_id: str,
    created_at: datetime,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError("Stored closure is invalid.")
    closure = deepcopy(value)
    closure["outcome"] = _stored_text(closure, "outcome")
    if not isinstance(closure.get("closed_by"), str):
        raise InputError("Stored closure author is invalid.")
    closure["closed_by"] = parse_discord_id(closure["closed_by"])
    closure["closed_at"], closed_at = _parse_timestamp(closure.get("closed_at"))
    if closure["closed_by"] != author_id or closed_at < created_at:
        raise InputError("Stored closure is invalid.")
    return closure


def _validated_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError("Stored decision is invalid.")
    if type(value.get("schema_version")) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise InputError("Stored decision schema is unsupported.")

    record = deepcopy(value)
    if type(record.get("id")) is not int:
        raise InputError("Stored decision ID is invalid.")
    record["id"] = parse_decision_id(record.get("id"))
    for name in ("title", "choice", "reason"):
        record[name] = _stored_text(record, name)
    record["tags"] = _stored_tags(record.get("tags"))
    if not isinstance(record.get("author_id"), str):
        raise InputError("Stored author is invalid.")
    record["author_id"] = parse_discord_id(record["author_id"])
    record["created_at"], created_at = _parse_timestamp(record.get("created_at"))

    status = record.get("status")
    if status == "open":
        if record.get("closure") is not None:
            raise InputError("Stored open decision has closure data.")
    elif status == "closed":
        record["closure"] = _stored_closure(
            record.get("closure"), author_id=record["author_id"], created_at=created_at
        )
    else:
        raise InputError("Stored status is invalid.")
    return record


def parse_record(value: Any) -> dict[str, Any] | None:
    """Return a normalized defensive copy of a schema-1 record, or None."""
    try:
        return _validated_record(value)
    except (InputError, OverflowError, TypeError, ValueError):
        return None


def close_record(
    record: dict[str, Any], *, actor_id: Any, outcome: Any, closed_at: str | None = None
) -> dict[str, Any]:
    parsed = parse_record(record)
    if parsed is None:
        raise InputError("That decision record is unavailable or uses an unsupported format.")
    actor = parse_discord_id(actor_id)
    if actor != parsed["author_id"]:
        raise PermissionError("Only the person who recorded this decision can close it.")
    if parsed["status"] == "closed":
        return parsed
    updated = deepcopy(parsed)  # retain normalized originals and unknown same-schema fields
    updated["status"] = "closed"
    updated["closure"] = {
        "outcome": validate_field("outcome", outcome),
        "closed_by": actor,
        "closed_at": parse_rfc3339(closed_at or utc_now()),
    }
    closed = parse_record(updated)
    if closed is None:
        raise InputError(
            "DecisionBook could not create a valid closure timestamp. Please try again."
        )
    return closed


def display_text(value: Any, limit: int | None = None) -> str:
    return _display_text(value, limit=limit, multiline=False)


def display_multiline_text(value: Any, limit: int | None = None) -> str:
    """Render normalized multiline prose with Discord markdown escaped."""
    return _display_text(value, limit=limit, multiline=True)


def _display_text(value: Any, *, limit: int | None, multiline: bool) -> str:
    normalized = normalize_multiline_text(value) if multiline else normalize_text(value)
    text = escape_discord_markdown(neutralize_broadcast_mentions(normalized))
    if limit is not None and len(text) > limit:
        if limit < 1:
            return ""
        if limit == 1:
            return "…"
        clipped = text[: limit - 1].rstrip(" \\\n")
        while clipped.endswith("\u200d"):
            clipped = clipped[:-1].rstrip()
        return clipped + "…"
    return text


def discord_user_mention(user_id: Any) -> str:
    """Render a validated user mention; handlers suppress all actual pings."""
    return f"<@{parse_discord_id(user_id)}>"


def discord_timestamp(value: Any, style: str = "F") -> str:
    """Render a validated RFC 3339 value as a Discord-localized timestamp."""
    if style not in {"t", "T", "d", "D", "f", "F", "R"}:
        raise InputError("Discord timestamp style is invalid.")
    _, parsed = _parse_timestamp(value)
    return f"<t:{int(parsed.timestamp())}:{style}>"


def record_matches(record: dict[str, Any], query: str) -> bool:
    needle = normalize_text(query).casefold()
    if not needle:
        return True
    haystack = " ".join(
        [
            str(record["id"]),
            record["title"],
            record["choice"],
            record["reason"],
            *record["tags"],
        ]
    ).casefold()
    return needle in haystack


def _whole_number(value: Any, *, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise InputError(f"{name} must be a whole number.")
    if isinstance(value, str):
        candidate = value.strip()
        if _POSITIVE_INTEGER.fullmatch(candidate) is None:
            raise InputError(f"{name} must be a whole number.")
        result = int(candidate)
    else:
        result = value
    if result < minimum:
        raise InputError(f"{name} must be a whole number of at least {minimum}.")
    return result


def matching_records(
    records: Iterable[dict[str, Any]], *, query: str = "", status: str = "all"
) -> list[dict[str, Any]]:
    """Return every valid match newest-first for pagination and exact totals."""
    normalized_status = normalize_text(status or "all").lower()
    if normalized_status not in {"all", "open", "closed"}:
        raise InputError("Status must be one of: all, open, or closed.")
    clean_query = validate_field("query", query, required=False)
    parsed = [item for raw in records if (item := parse_record(raw)) is not None]
    matches = [
        item
        for item in parsed
        if (normalized_status == "all" or item["status"] == normalized_status)
        and record_matches(item, clean_query)
    ]
    return sorted(matches, key=lambda item: item["id"], reverse=True)


def filter_records(
    records: Iterable[dict[str, Any]],
    *,
    query: str = "",
    status: str = "all",
    limit: int = 5,
    offset: int = 0,
    page: int | None = None,
) -> list[dict[str, Any]]:
    """Return one newest-first result page while retaining the original API."""
    safe_limit = min(_whole_number(limit, name="Limit", minimum=1), 10)
    safe_offset = _whole_number(offset, name="Offset", minimum=0)
    if page is not None:
        if safe_offset:
            raise InputError("Use either page or offset, not both.")
        safe_offset = (_whole_number(page, name="Page", minimum=1) - 1) * safe_limit
    ordered = matching_records(records, query=query, status=status)
    return ordered[safe_offset : safe_offset + safe_limit]


def decision_embed(record: dict[str, Any]) -> dict[str, Any]:
    parsed = parse_record(record)
    if parsed is None:
        raise InputError("That decision record is unavailable or uses an unsupported format.")
    closed = parsed["status"] == "closed"
    fields = [
        {
            "name": "Decision",
            "value": display_multiline_text(parsed["choice"], 1024),
            "inline": False,
        },
        {
            "name": "Reason",
            "value": display_multiline_text(parsed["reason"], 1024),
            "inline": False,
        },
    ]
    if parsed["tags"]:
        fields.append(
            {
                "name": "Tags",
                "value": " ".join(f"`{display_text(tag)}`" for tag in parsed["tags"]),
                "inline": False,
            }
        )
    fields.append({"name": "Status", "value": "Closed" if closed else "Open", "inline": True})
    fields.extend(
        [
            {
                "name": "Recorded by",
                "value": discord_user_mention(parsed["author_id"]),
                "inline": True,
            },
            {
                "name": "Recorded",
                "value": discord_timestamp(parsed["created_at"]),
                "inline": True,
            },
        ]
    )
    if closed:
        fields.extend(
            [
                {
                    "name": "Outcome",
                    "value": display_multiline_text(parsed["closure"]["outcome"], 1024),
                    "inline": False,
                },
                {
                    "name": "Closed by",
                    "value": discord_user_mention(parsed["closure"]["closed_by"]),
                    "inline": True,
                },
                {
                    "name": "Closed",
                    "value": discord_timestamp(parsed["closure"]["closed_at"]),
                    "inline": True,
                },
            ]
        )
    return {
        "title": f"Decision #{parsed['id']} — {display_text(parsed['title'])}",
        "color": CLOSED_COLOR if closed else OPEN_COLOR,
        "fields": fields,
        "footer": {"text": "DecisionBook • Original decision details are immutable."},
    }


def list_embed(
    records: list[dict[str, Any]],
    *,
    query: str = "",
    status: str = "all",
    offset: int = 0,
    total: int | None = None,
) -> dict[str, Any]:
    lines: list[str] = []
    for record in records:
        closed = record["status"] == "closed"
        icon = "✅" if closed else "📖"
        status_text = "Closed" if closed else "Open"
        tags = (
            " " + " ".join(f"`{display_text(tag)}`" for tag in record["tags"])
            if record["tags"]
            else ""
        )
        lines.append(
            f"{icon} **{status_text} · #{record['id']} — {display_text(record['title'], 80)}**\n"
            f"{display_text(record['choice'], 160)}{tags}\n"
            f"{discord_user_mention(record['author_id'])} · "
            f"{discord_timestamp(record['created_at'], 'R')}"
        )
    filters = f"Status: {normalize_text(status or 'all').lower()}"
    if normalize_text(query):
        filters += f" • Search: {display_text(query, 80)}"
    safe_offset = _whole_number(offset, name="Offset", minimum=0)
    known_total = (
        safe_offset + len(records)
        if total is None
        else _whole_number(total, name="Total", minimum=0)
    )
    known_total = max(known_total, safe_offset + len(records))
    first = safe_offset + 1 if records else 0
    last = safe_offset + len(records)
    return {
        "title": "DecisionBook",
        "description": "\n\n".join(lines)[:4096],
        "color": INK_COLOR,
        "footer": {
            "text": (
                f"{filters} • Showing {first}–{last} of {known_total} • "
                "Use /decision view with an ID for the full record."
            )
        },
    }
