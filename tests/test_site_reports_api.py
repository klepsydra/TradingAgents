"""Unit tests for report summary helpers used by quote pages / cadence."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "scripts" / "site_server.py"


def _load_site():
    spec = importlib.util.spec_from_file_location("site_server", SITE)
    mod = importlib.util.module_from_spec(spec)
    # Avoid binding the HTTP server on import side effects — site_server only
    # defines helpers until main().
    sys.modules["site_server"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_summarize_run_includes_public_urls(tmp_path, monkeypatch):
    mod = _load_site()
    monkeypatch.setattr(mod, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(mod, "PUBLIC_URL", "https://TradingAgents.quiltyapp.com")
    run = {
        "id": "abc",
        "status": "ok",
        "ticker": "VOO",
        "rating": "Buy",
        "conclusion": "Looks fine",
        "date": "2026-11-04",
        "report_html": "/tmp/x.html",
    }
    summary = mod.summarize_run(run)
    assert summary["rating"] == "Buy"
    assert summary["run_url"] == "/runs/abc"
    assert summary["public_url"] == "https://TradingAgents.quiltyapp.com/runs/abc"
    assert summary["has_report"] is True


def test_latest_report_prefers_ok_with_rating(tmp_path, monkeypatch):
    mod = _load_site()
    monkeypatch.setattr(mod, "RUNS_DIR", tmp_path)
    (tmp_path / "old.json").write_text(
        '{"id":"1","status":"failed","ticker":"VOO","created_at":"2026-01-01T00:00:00+00:00"}\n'
    )
    (tmp_path / "new.json").write_text(
        '{"id":"2","status":"ok","ticker":"VOO","rating":"Hold","conclusion":"x",'
        '"created_at":"2026-11-04T00:00:00+00:00","finished_at":"2026-11-04T01:00:00+00:00"}\n'
    )
    # mtime order: touch new last
    import os, time

    os.utime(tmp_path / "old.json", (1, 1))
    os.utime(tmp_path / "new.json", None)
    latest = mod.latest_report_for_ticker("VOO")
    assert latest is not None
    assert latest["id"] == "2"
    assert latest["rating"] == "Hold"
