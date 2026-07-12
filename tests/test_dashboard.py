from __future__ import annotations

import pytest
from conftest import AUTHOR_ID, CHANNEL_ID

import decisionbook
from core import decision_key, make_record
from decisionbook import (
    SCHEMA_KEY,
    get_closed_decisions,
    get_onboarding,
    get_open_decisions,
    get_recent_decisions,
    get_storage_health,
    get_total_decisions,
    status_counts,
)


def seed(ctx, decision_id, status="open"):
    ctx.kv.set(SCHEMA_KEY, 2)
    item = make_record(
        decision_id,
        title=f"Title {decision_id}",
        choice="A useful decision",
        reason="Reason",
        tags="tag",
        author_id=AUTHOR_ID,
        channel_id=CHANNEL_ID,
        created_at="2026-07-11T00:00:00+00:00",
    )
    if status == "closed":
        item["status"] = "closed"
        item["closure"] = {
            "outcome": "Done",
            "closed_by": AUTHOR_ID,
            "closed_at": "2026-07-20T00:00:00+00:00",
        }
    ctx.kv.set(decision_key(decision_id), item)


def test_dashboard_onboarding_and_empty_state(ctx):
    assert status_counts(ctx) == (0, 0)
    assert "Decisions remembered" in get_onboarding(ctx, {})["markdown"]
    health = get_storage_health(ctx, {})
    assert health["level"] == "info"
    assert "healthy" in health["message"].lower()
    assert get_total_decisions(ctx, {}) == {"value": 0}
    assert get_open_decisions(ctx, {}) == {"value": 0}
    assert get_closed_decisions(ctx, {}) == {"value": 0}
    assert get_recent_decisions(ctx, {}) == {"rows": [], "total": 0}


def test_dashboard_counts_statuses_exactly(ctx):
    seed(ctx, 1, "open")
    seed(ctx, 2, "closed")
    seed(ctx, 3, "open")
    assert status_counts(ctx) == (2, 1)
    assert get_total_decisions(ctx, {})["value"] == 3
    assert get_open_decisions(ctx, {})["value"] == 2
    assert get_closed_decisions(ctx, {})["value"] == 1


def test_dashboard_fails_fast_instead_of_rebuilding_dirty_counts(ctx, monkeypatch):
    seed(ctx, 1)
    monkeypatch.setattr(
        ctx.kv,
        "get_many",
        lambda _keys: (_ for _ in ()).throw(AssertionError("dashboard attempted a repair scan")),
    )

    with pytest.raises(decisionbook.StorageStateError, match="require repair"):
        get_recent_decisions(ctx, {})


def test_storage_health_reports_a_concurrent_write_as_retryable(ctx, monkeypatch):
    monkeypatch.setattr(
        decisionbook,
        "status_counts",
        lambda _ctx, **_kwargs: (_ for _ in ()).throw(decisionbook.StorageBusyError("writing")),
    )

    health = get_storage_health(ctx, {})
    assert health["level"] == "warn"
    assert "refresh" in health["message"].lower()
    assert "write is finishing" in health["message"].lower()


def test_storage_health_fails_closed_for_invalid_integrity_metadata(ctx, monkeypatch):
    real_get = ctx.kv.get
    monkeypatch.setattr(decisionbook, "status_counts", lambda _ctx, **_kwargs: (1, 0))
    monkeypatch.setattr(
        ctx.kv,
        "get",
        lambda key: "invalid" if key == decisionbook.MALFORMED_COUNT_KEY else real_get(key),
    )

    health = get_storage_health(ctx, {})
    assert health["level"] == "error"
    assert "needs repair" in health["message"].lower()


def test_sparse_dashboard_directly_loads_only_the_requested_valid_page(ctx, monkeypatch):
    seed(ctx, 1)
    seed(ctx, 1001)
    assert status_counts(ctx) == (2, 0)
    real_get_many = ctx.kv.get_many
    fetched: list[list[str]] = []

    def capture(keys):
        fetched.append(list(keys))
        return real_get_many(keys)

    monkeypatch.setattr(ctx.kv, "get_many", capture)
    result = get_recent_decisions(ctx, {"page": 2, "page_size": 1})

    assert [row["id"] for row in result["rows"]] == [1]
    assert fetched == [[decision_key(1)]]


def test_sparse_dashboard_streams_past_a_canonical_malformed_record(ctx):
    seed(ctx, 1)
    ctx.kv.set(decision_key(1001), {"schema_version": 2, "id": 1001})
    assert status_counts(ctx) == (1, 0)

    result = get_recent_decisions(ctx, {"page": 1, "page_size": 1})

    assert result["total"] == 1
    assert [row["id"] for row in result["rows"]] == [1]


def test_recent_rows_are_newest_first_and_human_readable(ctx):
    seed(ctx, 1)
    seed(ctx, 2)
    assert status_counts(ctx) == (2, 0)
    result = get_recent_decisions(ctx, {"page_size": 1})
    assert result["total"] == 2
    assert [row["id"] for row in result["rows"]] == [2]
    assert result["rows"][0]["status"] == "Open"
    assert result["rows"][0]["recorded"] == "2026-07-11 00:00 UTC"
    assert result["rows"][0]["closed"] == "—"
    assert "tag" in result["rows"][0]["summary"]
    assert set(result["rows"][0]) == {"id", "status", "summary", "recorded", "closed"}


def test_dashboard_page_and_offset_pagination_are_distinct(ctx):
    for decision_id in range(1, 61):
        seed(ctx, decision_id)
    assert status_counts(ctx) == (60, 0)
    first = get_recent_decisions(ctx, {"page": 1, "page_size": 25})
    second = get_recent_decisions(ctx, {"page": 2, "page_size": 25})
    third = get_recent_decisions(ctx, {"offset": 50, "limit": 25})
    assert [row["id"] for row in first["rows"]] == list(range(60, 35, -1))
    assert [row["id"] for row in second["rows"]] == list(range(35, 10, -1))
    assert [row["id"] for row in third["rows"]] == list(range(10, 0, -1))
    assert first["total"] == second["total"] == third["total"] == 60


def test_dashboard_beyond_last_page_and_hostile_params(ctx):
    seed(ctx, 1)
    assert status_counts(ctx) == (1, 0)
    assert get_recent_decisions(ctx, {"page": 99, "page_size": 25}) == {
        "rows": [],
        "total": 1,
    }
    assert get_recent_decisions(ctx, {"page": "bad", "page_size": "bad"})["total"] == 1
    assert get_recent_decisions(ctx, {"offset": -999, "limit": 999})["rows"][0]["id"] == 1


def test_dashboard_newest_and_totals_work_above_old_boundary(ctx):
    for decision_id in range(1, 502):
        seed(ctx, decision_id)
    assert status_counts(ctx) == (501, 0)
    result = get_recent_decisions(ctx, {"page": 1, "page_size": 25})
    assert result["total"] == 501
    assert result["rows"][0]["id"] == 501
    assert result["rows"][-1]["id"] == 477
    assert get_total_decisions(ctx, {}) == {"value": 501}


def test_dashboard_skips_malformed_and_mismatched_records(ctx):
    ctx.kv.set(decision_key(1), {"schema_version": 2, "id": 1})
    seed(ctx, 2)
    mismatched = ctx.kv.get(decision_key(2)).copy()
    mismatched["id"] = 999
    ctx.kv.set(decision_key(3), mismatched)
    assert status_counts(ctx) == (1, 0)
    assert get_total_decisions(ctx, {})["value"] == 1
    result = get_recent_decisions(ctx, {})
    assert [row["id"] for row in result["rows"]] == [2]


def test_dashboard_rows_come_from_one_record_window(ctx, monkeypatch):
    for decision_id in range(1, 4):
        seed(ctx, decision_id)
    assert status_counts(ctx) == (3, 0)
    calls = 0
    original = ctx.kv.get_many

    def capture(keys):
        nonlocal calls
        calls += 1
        return original(keys)

    monkeypatch.setattr(ctx.kv, "get_many", capture)
    result = get_recent_decisions(ctx, {"page_size": 2})
    assert [row["id"] for row in result["rows"]] == [3, 2]
    # Warm count metadata lets the dashboard load only the requested page.
    assert calls == 1
