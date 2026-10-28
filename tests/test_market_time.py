"""Analysis-date "today" follows Webbit / agentRobin timezone prefs."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cli import prompts
from tradingagents import market_time


@pytest.mark.unit
def test_market_today_defaults_to_los_angeles(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_MARKET_TZ", raising=False)
    monkeypatch.delenv("AGENTROBIN_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    assert market_time.market_tz_name() == "America/Los_Angeles"
    assert market_time.market_today() == datetime.now(ZoneInfo("America/Los_Angeles")).date()


@pytest.mark.unit
def test_market_today_prefers_agentrobin_timezone(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_MARKET_TZ", raising=False)
    monkeypatch.setenv("AGENTROBIN_TIMEZONE", "America/Los_Angeles")
    monkeypatch.setenv("TZ", "UTC")
    assert market_time.market_tz_name() == "America/Los_Angeles"
    assert market_time.market_today() == datetime.now(ZoneInfo("America/Los_Angeles")).date()


@pytest.mark.unit
def test_market_today_explicit_override(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_MARKET_TZ", "America/New_York")
    monkeypatch.setenv("AGENTROBIN_TIMEZONE", "America/Los_Angeles")
    assert market_time.market_tz_name() == "America/New_York"
    assert market_time.market_today() == datetime.now(ZoneInfo("America/New_York")).date()


@pytest.mark.unit
def test_parse_analysis_date_accepts_operator_today(monkeypatch):
    today = date(2026, 10, 28)
    monkeypatch.setattr(market_time, "market_today", lambda: today)
    assert prompts.parse_analysis_date("2026-10-28") == "2026-10-28"
    with pytest.raises(ValueError, match="future"):
        prompts.parse_analysis_date((today + timedelta(days=1)).isoformat())
