from __future__ import annotations

from conftest import AUTHOR_ID

from core import decision_key, make_record
from decisionbook import (
    get_closed_decisions,
    get_onboarding,
    get_open_decisions,
    get_recent_decisions,
    get_total_decisions,
)


def seed(ctx, decision_id, status="open"):
    item = make_record(
        decision_id,
        title=f"Title {decision_id}",
        choice="A useful decision",
        reason="Reason",
        tags="tag",
        author_id=AUTHOR_ID,
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
    assert "Decisions remembered" in get_onboarding(ctx, {})["markdown"]
    assert get_total_decisions(ctx, {}) == {"value": 0}
    assert get_open_decisions(ctx, {}) == {"value": 0}
    assert get_closed_decisions(ctx, {}) == {"value": 0}
    assert get_recent_decisions(ctx, {}) == {"rows": [], "total": 0}


def test_dashboard_counts_statuses_exactly(ctx):
    seed(ctx, 1, "open")
    seed(ctx, 2, "closed")
    seed(ctx, 3, "open")
    assert get_total_decisions(ctx, {})["value"] == 3
    assert get_open_decisions(ctx, {})["value"] == 2
    assert get_closed_decisions(ctx, {})["value"] == 1


def test_recent_rows_are_newest_first_and_human_readable(ctx):
    seed(ctx, 1)
    seed(ctx, 2)
    result = get_recent_decisions(ctx, {"page_size": 1})
    assert result["total"] == 2
    assert [row["id"] for row in result["rows"]] == [2]
    assert result["rows"][0]["status"] == "Open"
    assert result["rows"][0]["recorded"] == "2026-07-11 00:00 UTC"
    assert result["rows"][0]["author"] == AUTHOR_ID
    assert "tag" in result["rows"][0]["summary"]


def test_dashboard_page_and_offset_pagination_are_distinct(ctx):
    for decision_id in range(1, 61):
        seed(ctx, decision_id)
    first = get_recent_decisions(ctx, {"page": 1, "page_size": 25})
    second = get_recent_decisions(ctx, {"page": 2, "page_size": 25})
    third = get_recent_decisions(ctx, {"offset": 50, "limit": 25})
    assert [row["id"] for row in first["rows"]] == list(range(60, 35, -1))
    assert [row["id"] for row in second["rows"]] == list(range(35, 10, -1))
    assert [row["id"] for row in third["rows"]] == list(range(10, 0, -1))
    assert first["total"] == second["total"] == third["total"] == 60


def test_dashboard_beyond_last_page_and_hostile_params(ctx):
    seed(ctx, 1)
    assert get_recent_decisions(ctx, {"page": 99, "page_size": 25}) == {
        "rows": [],
        "total": 1,
    }
    assert get_recent_decisions(ctx, {"page": "bad", "page_size": "bad"})["total"] == 1
    assert get_recent_decisions(ctx, {"offset": -999, "limit": 999})["rows"][0]["id"] == 1


def test_dashboard_newest_and_totals_work_above_old_boundary(ctx):
    for decision_id in range(1, 502):
        seed(ctx, decision_id)
    result = get_recent_decisions(ctx, {"page": 1, "page_size": 25})
    assert result["total"] == 501
    assert result["rows"][0]["id"] == 501
    assert result["rows"][-1]["id"] == 477
    assert get_total_decisions(ctx, {}) == {"value": 501}


def test_dashboard_skips_malformed_and_mismatched_records(ctx):
    ctx.kv.set(decision_key(1), {"schema_version": 1, "id": 1})
    seed(ctx, 2)
    mismatched = ctx.kv.get(decision_key(2)).copy()
    mismatched["id"] = 999
    ctx.kv.set(decision_key(3), mismatched)
    assert get_total_decisions(ctx, {})["value"] == 1
    result = get_recent_decisions(ctx, {})
    assert [row["id"] for row in result["rows"]] == [2]


def test_dashboard_rows_come_from_one_record_window(ctx, monkeypatch):
    for decision_id in range(1, 4):
        seed(ctx, decision_id)
    calls = 0
    original = ctx.kv.get_many

    def capture(keys):
        nonlocal calls
        calls += 1
        return original(keys)

    monkeypatch.setattr(ctx.kv, "get_many", capture)
    result = get_recent_decisions(ctx, {"page_size": 2})
    assert [row["id"] for row in result["rows"]] == [3, 2]
    # One count rebuild plus one page snapshot; no duplicate page rescan.
    assert calls == 2
