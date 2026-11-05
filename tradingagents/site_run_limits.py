"""Daily per-ticker analyze caps for the TradingAgents site.

Policy (home Analyze / ``/api/analyze`` / ``/start``):

* At most ``max_per_day`` analyses per ticker per operator market day
  (``America/Los_Angeles`` via ``market_today_iso``, unless overridden).
* Repeated requests while a run is ``queued``/``running`` coalesce into that
  in-flight run (most recent).
* Once the daily cap is reached, further requests coalesce into the most
  recent run for that ticker/day instead of starting another.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_MAX_ANALYSES_PER_TICKER_DAY = 2
_ACTIVE = frozenset({"queued", "running"})


def run_day_key(run: dict[str, Any], *, market_tz: ZoneInfo) -> str | None:
    """Calendar day for a run: analysis ``date`` if set, else ``created_at`` in market TZ."""
    date_field = str(run.get("date") or "").strip()
    if len(date_field) >= 10 and date_field[4] == "-" and date_field[7] == "-":
        return date_field[:10]
    created = str(run.get("created_at") or "").strip()
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(market_tz).date().isoformat()
    except ValueError:
        return None


def sort_runs_newest_first(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(run: dict[str, Any]) -> str:
        return str(run.get("created_at") or run.get("id") or "")

    return sorted(runs, key=key, reverse=True)


def select_coalesce_target(
    runs_today: list[dict[str, Any]],
    *,
    max_per_day: int = DEFAULT_MAX_ANALYSES_PER_TICKER_DAY,
) -> tuple[dict[str, Any] | None, str | None]:
    """Pick an existing run to reuse, or ``(None, None)`` if a new analyze is allowed.

    Reasons:
    * ``in_flight`` — a queued/running run already exists (combine into most recent active)
    * ``daily_cap`` — already ``max_per_day`` analyses today (combine into most recent)
    """
    if max_per_day < 1:
        max_per_day = 1
    ordered = sort_runs_newest_first(list(runs_today or []))
    if not ordered:
        return None, None
    active = [r for r in ordered if str(r.get("status") or "").lower() in _ACTIVE]
    if active:
        return active[0], "in_flight"
    if len(ordered) >= max_per_day:
        return ordered[0], "daily_cap"
    return None, None
