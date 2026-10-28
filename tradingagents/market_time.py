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
from datetime import date, datetime
from zoneinfo import ZoneInfo

_DEFAULT_TZ = "America/Los_Angeles"


def market_tz_name() -> str:
    for key in ("TRADINGAGENTS_MARKET_TZ", "AGENTROBIN_TIMEZONE", "TZ"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return raw
    return _DEFAULT_TZ


def market_tz() -> ZoneInfo:
    return ZoneInfo(market_tz_name())


def market_today() -> date:
    """Today in the Webbit / agentRobin operator timezone."""
    return datetime.now(market_tz()).date()


def market_today_iso() -> str:
    return market_today().isoformat()
