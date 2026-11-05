"""Daily per-ticker analyze coalesce (≤2 / day)."""

from zoneinfo import ZoneInfo

from tradingagents.site_run_limits import (
    run_day_key,
    select_coalesce_target,
    sort_runs_newest_first,
)


LA = ZoneInfo("America/Los_Angeles")


def test_run_day_key_prefers_date_field():
    assert (
        run_day_key(
            {"date": "2026-11-05", "created_at": "2026-11-06T08:00:00+00:00"},
            market_tz=LA,
        )
        == "2026-11-05"
    )


def test_run_day_key_from_created_at_market_tz():
    # 2026-11-06 07:00 UTC = 2026-11-05 23:00 PT
    assert (
        run_day_key(
            {"created_at": "2026-11-06T07:00:00+00:00"},
            market_tz=LA,
        )
        == "2026-11-05"
    )


def test_coalesce_in_flight_over_cap():
    runs = [
        {"id": "b", "status": "queued", "created_at": "2026-11-05T18:00:00+00:00"},
        {"id": "a", "status": "ok", "created_at": "2026-11-05T12:00:00+00:00"},
    ]
    target, reason = select_coalesce_target(runs, max_per_day=2)
    assert reason == "in_flight"
    assert target["id"] == "b"


def test_coalesce_daily_cap_uses_most_recent():
    runs = [
        {"id": "older", "status": "ok", "created_at": "2026-11-05T10:00:00+00:00"},
        {"id": "newer", "status": "failed", "created_at": "2026-11-05T16:00:00+00:00"},
    ]
    target, reason = select_coalesce_target(runs, max_per_day=2)
    assert reason == "daily_cap"
    assert target["id"] == "newer"


def test_allows_second_when_one_finished():
    runs = [{"id": "one", "status": "ok", "created_at": "2026-11-05T10:00:00+00:00"}]
    target, reason = select_coalesce_target(runs, max_per_day=2)
    assert target is None
    assert reason is None


def test_allows_first_empty():
    assert select_coalesce_target([], max_per_day=2) == (None, None)


def test_sort_newest_first():
    rows = sort_runs_newest_first(
        [
            {"id": "a", "created_at": "2026-11-05T10:00:00+00:00"},
            {"id": "b", "created_at": "2026-11-05T12:00:00+00:00"},
        ]
    )
    assert [r["id"] for r in rows] == ["b", "a"]
