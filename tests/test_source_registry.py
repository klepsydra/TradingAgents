"""Settings compare-source registry (ROBIN-1906)."""

from __future__ import annotations

import json
from pathlib import Path

from tradingagents.market_compare import source_registry as reg


def test_infer_and_apply_template():
    tmpl = reg.infer_url_template("https://robinhood.com/news/NOW", "NOW")
    assert tmpl == "https://robinhood.com/news/{TICKER}"
    assert reg.apply_url_template(tmpl, "AAPL") == "https://robinhood.com/news/AAPL"


def test_builtin_defs_include_rh_news():
    ids = {d["id"] for d in reg.builtin_source_defs()}
    assert "robinhood-news" in ids
    assert "tipranks" in ids


def test_store_update_and_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_SITE_SETTINGS", str(tmp_path / "compare_sources.json"))
    monkeypatch.setenv("TRADINGAGENTS_RUNS_DIR", str(tmp_path / "runs"))
    (tmp_path / "runs").mkdir()
    store = reg.load_store()
    assert store["sources"]
    sid = "robinhood-news"
    updated = reg.update_source(
        sid,
        {
            "display_name": "RH News Feed",
            "enabled": False,
            "url_template": "https://robinhood.com/news/{TICKER}",
        },
    )
    assert updated["display_name"] == "RH News Feed"
    assert updated["enabled"] is False
    assert "Robinhood news" in reg.disabled_source_names()
    catalog = reg.effective_source_catalog("AAPL")
    assert all(r["name"] != "Robinhood news" for r in catalog)


def test_record_and_backfill(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_SITE_SETTINGS", str(tmp_path / "compare_sources.json"))
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setenv("TRADINGAGENTS_RUNS_DIR", str(runs))
    reg.load_store()
    run = {
        "id": "20261105T000000Z-test01",
        "ticker": "AAPL",
        "created_at": "2026-11-05T12:00:00+00:00",
        "market_compare": {
            "status": "ok",
            "finished_at": "2026-11-05T12:05:00+00:00",
            "source_urls": [
                {"name": "Robinhood news", "url": "https://robinhood.com/news/AAPL", "group": "media"}
            ],
            "gather": {
                "page_hits": [
                    {
                        "name": "Robinhood news",
                        "url": "https://robinhood.com/news/AAPL",
                        "ok": True,
                        "skipped": False,
                    }
                ]
            },
        },
    }
    (runs / f"{run['id']}.json").write_text(json.dumps(run) + "\n", encoding="utf-8")
    result = reg.backfill_from_runs(reset_stats=True)
    assert result["compare_runs"] == 1
    src = reg.get_source("robinhood-news")
    assert src is not None
    assert (src.get("stats") or {}).get("use_count", 0) >= 1
    assert run["id"] in (src.get("stats") or {}).get("report_ids", [])
    reports = reg.source_reports("robinhood-news")
    assert any(r.get("id") == run["id"] for r in reports["reports"])
