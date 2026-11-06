"""Calendar "today" for analysis dates — same zone as Webbit / agentRobin.

Webbit remembers the operator zone as ``AGENTROBIN_TIMEZONE`` (default
``America/Los_Angeles``, see ``AgentrobinTimezone`` and
``config/agentrobin.env``). TradingAgents must use that same preference so a
queued ``--date`` is never rejected as "in the future" relative to the site
the user is looking at.

Override order:
1. ``TRADINGAGENTS_MARKET_TZ`` — explicit TradingAgents override
2. ``AGENTROBIN_TIMEZONE`` — shared Webbit / agentRobin preference
3. ``TZ`` — process timezone
4. ``America/Los_Angeles`` — same default as agentRobin / webbit
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DEFAULT_TZ = "America/Los_Angeles"


def market_tz_name() -> str:
    for key in ("TRADINGAGENTS_MARKET_TZ", "AGENTROBIN_TIMEZONE", "TZ"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return raw
    return _DEFAULT_TZ


def market_tz() -> ZoneInfo:
    name = market_tz_name()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(_DEFAULT_TZ)


def market_today() -> date:
    """Today in the Webbit / agentRobin operator timezone."""
    return datetime.now(market_tz()).date()


def market_today_iso() -> str:
    return market_today().isoformat()


def parse_instant(value: object) -> datetime | None:
    """Parse an ISO-8601 / RFC3339 instant; naive values are treated as UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_local(
    value: object,
    *,
    timespec: str = "seconds",
    with_tz_name: bool = False,
) -> str:
    """Format an instant for UI tables in the operator timezone (Pacific by default).

    Returns ``YYYY-MM-DD HH:MM:SS`` (no trailing Z — this is wall time in
    ``market_tz``). Optional `` PT``/zone abbrev suffix when ``with_tz_name``.
    """
    dt = parse_instant(value)
    if dt is None:
        return ""
    local = dt.astimezone(market_tz())
    if timespec == "minutes":
        stamp = local.strftime("%Y-%m-%d %H:%M")
    else:
        stamp = local.strftime("%Y-%m-%d %H:%M:%S")
    if not with_tz_name:
        return stamp
    # %Z is PST/PDT for America/Los_Angeles; fall back to the IANA name.
    abbrev = local.strftime("%Z") or market_tz_name()
    return f"{stamp} {abbrev}"
