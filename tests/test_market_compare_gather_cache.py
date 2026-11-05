"""Offline tests for market-compare gather cache + paywall skip (ROBIN-1894)."""

from __future__ import annotations

import json
import time

from tradingagents.market_compare import gather


def test_looks_paywalled_status_codes():
    assert gather._looks_paywalled(403, "", "thin")
    assert gather._looks_paywalled(401, "", "x" * 500)
    assert gather._looks_paywalled(429, "", "x" * 500)
    assert not gather._looks_paywalled(200, "<html>ok</html>", "Buy rating from TipRanks with plenty of text " * 20)


def test_looks_paywalled_soft_wall_thin_snippet():
    body = "Subscribe to continue reading this premium article"
    assert gather._looks_paywalled(200, body, "short teaser")
    assert not gather._looks_paywalled(
        200,
        body,
        "Analysts raised the price target after earnings beat expectations " * 10,
    )


def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_MARKET_COMPARE_CACHE", str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_MARKET_COMPARE_CACHE_DAYS", "7")
    url = "https://example.com/quote/AAPL"
    row = {
        "name": "Example",
        "url": url,
        "group": "analyst",
        "ok": True,
        "snippet": "Consensus Buy",
        "keywords": ["buy"],
        "http_status": 200,
    }
    gather.write_cached_snippet(row)
    hit = gather.read_cached_snippet(url)
    assert hit is not None
    assert hit["from_cache"] is True
    assert hit["snippet"] == "Consensus Buy"
    assert hit["ok"] is True

    # Expire
    path = gather._cache_path(url)
    stale = json.loads(path.read_text())
    stale["cached_at"] = time.time() - 8 * 86400
    path.write_text(json.dumps(stale))
    assert gather.read_cached_snippet(url) is None


def test_fetch_reuses_cache_on_paywall(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_MARKET_COMPARE_CACHE", str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_MARKET_COMPARE_CACHE_DAYS", "7")

    url = "https://example.com/stocks/aapl"
    gather.write_cached_snippet(
        {
            "name": "TipRanks",
            "url": url,
            "group": "analyst",
            "ok": True,
            "snippet": "cached buy rating explanation",
            "keywords": ["buy", "analyst"],
            "http_status": 200,
        }
    )

    monkeypatch.setattr(
        gather,
        "source_catalog",
        lambda _t: [{"name": "TipRanks", "url": url, "group": "analyst", "asset_kind": "stock"}],
    )
    monkeypatch.setattr(gather, "_http_get", lambda _url, timeout=12.0: (403, "Access Denied"))

    pages = gather.fetch_page_snippets("AAPL", max_pages=1)
    assert len(pages) == 1
    assert pages[0]["ok"] is True
    assert pages[0]["from_cache"] is True
    assert pages[0]["snippet"] == "cached buy rating explanation"
    assert pages[0].get("skipped_live") is True


def test_fetch_skips_without_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_MARKET_COMPARE_CACHE", str(tmp_path))
    url = "https://example.com/paywall/aapl"
    monkeypatch.setattr(
        gather,
        "source_catalog",
        lambda _t: [{"name": "Seeking Alpha", "url": url, "group": "analyst", "asset_kind": "stock"}],
    )
    monkeypatch.setattr(
        gather,
        "_http_get",
        lambda _url, timeout=12.0: (200, "<html>Subscribe to continue</html>"),
    )
    pages = gather.fetch_page_snippets("AAPL", max_pages=1)
    assert pages[0]["ok"] is False
    assert pages[0]["skipped"] is True
    assert pages[0]["skip_reason"] == "paywall"
