"""Orchestrate gather + multi-LLM panel into a run's market_compare blob."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .gather import gather_market_views
from .panelists import run_panel
from .sources import asset_kind, source_catalog


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def market_compare_status(run: dict | None) -> str:
    mc = (run or {}).get("market_compare") or {}
    return str(mc.get("status") or "absent")


def _synthesize_executive(panel: list[dict], research_parsed: dict | None) -> dict[str, Any]:
    """Lightweight merge of successful panelist JSON objects."""
    ok = [p for p in panel if p.get("ok") and isinstance(p.get("parsed"), dict)]
    alignments = [p["parsed"].get("alignment") for p in ok if p["parsed"].get("alignment")]
    street = [p["parsed"].get("street_consensus") for p in ok if p["parsed"].get("street_consensus")]
    ta = [p["parsed"].get("tradingagents_rating") for p in ok if p["parsed"].get("tradingagents_rating")]

    coincide: list[dict] = []
    differ: list[dict] = []
    per_source: list[dict] = []
    takes: list[str] = []
    for p in ok:
        parsed = p["parsed"]
        for item in parsed.get("coincide") or []:
            if isinstance(item, dict):
                coincide.append({**item, "via": p.get("provider")})
        for item in parsed.get("differ") or []:
            if isinstance(item, dict):
                differ.append({**item, "via": p.get("provider")})
        for item in parsed.get("per_source") or []:
            if isinstance(item, dict):
                per_source.append({**item, "via": p.get("provider")})
        take = parsed.get("executive_take")
        if take:
            takes.append(f"**{p.get('provider')}**: {take}")

    research_summary = ""
    if isinstance(research_parsed, dict):
        research_summary = str(research_parsed.get("consensus_summary") or "")
        if research_parsed.get("consensus_rating"):
            street.append(research_parsed.get("consensus_rating"))

    def mode(vals: list) -> str:
        vals = [v for v in vals if v]
        if not vals:
            return "Unknown"
        return max(set(vals), key=vals.count)

    return {
        "street_consensus": mode(street),
        "tradingagents_rating": mode(ta),
        "alignment": mode(alignments),
        "research_summary": research_summary,
        "coincide": coincide[:12],
        "differ": differ[:12],
        "per_source": per_source[:24],
        "panel_takes": takes,
        "panelists_ok": [p.get("provider") for p in ok],
        "panelists_failed": [
            {"provider": p.get("provider"), "error": p.get("error")}
            for p in panel
            if not p.get("ok")
        ],
    }


def run_market_compare(run: dict) -> dict:
    """Mutates and returns run with market_compare filled. Safe to call repeatedly."""
    ticker = str(run.get("ticker") or "").strip().upper()
    mc = {
        "status": "running",
        "started_at": iso_now(),
        "ticker": ticker,
        "asset_kind": asset_kind(ticker),
        "source_urls": source_catalog(ticker),
    }
    run["market_compare"] = mc

    if not ticker:
        mc.update({"status": "failed", "error": "ticker missing", "finished_at": iso_now()})
        return run

    ta_decision = "\n".join(
        filter(
            None,
            [
                f"Rating: {run.get('rating')}" if run.get("rating") else "",
                run.get("decision") or "",
                run.get("conclusion") or "",
            ],
        )
    ).strip()
    if not ta_decision:
        ta_decision = f"TradingAgents run {run.get('id')} has no captured decision text yet."

    try:
        gather = gather_market_views(ticker)
        mc["gather"] = {
            "research_ok": bool((gather.get("research") or {}).get("ok")),
            "research_error": (gather.get("research") or {}).get("error"),
            "research_model": (gather.get("research") or {}).get("model"),
            "citations": (gather.get("research") or {}).get("citations") or [],
            "parsed": (gather.get("research") or {}).get("parsed"),
            "page_hits": [
                {
                    "name": s.get("name"),
                    "url": s.get("url"),
                    "ok": s.get("ok"),
                    "http_status": s.get("http_status"),
                    "keywords": s.get("keywords"),
                    "snippet": (s.get("snippet") or "")[:600],
                }
                for s in (gather.get("page_snippets") or [])
            ],
        }
        # Keep a short raw for debugging without bloating forever
        if (gather.get("research") or {}).get("raw"):
            mc["gather"]["research_raw"] = (gather["research"]["raw"])[:12000]

        panel = run_panel(ticker=ticker, ta_decision=ta_decision, market_gather=gather)
        mc["panel"] = [
            {
                "provider": p.get("provider"),
                "model": p.get("model"),
                "ok": p.get("ok"),
                "error": p.get("error"),
                "parsed": p.get("parsed"),
                "raw": (p.get("raw") or "")[:8000] if p.get("ok") and not p.get("parsed") else None,
            }
            for p in panel
        ]
        mc["summary"] = _synthesize_executive(panel, (gather.get("research") or {}).get("parsed"))
        mc["status"] = "ok" if any(p.get("ok") for p in panel) or mc["gather"].get("research_ok") else "failed"
        if mc["status"] == "failed":
            mc["error"] = "gather and panelists all failed"
    except Exception as exc:  # noqa: BLE001
        mc["status"] = "failed"
        mc["error"] = f"{type(exc).__name__}: {exc}"[:1500]

    mc["finished_at"] = iso_now()
    run["market_compare"] = mc
    return run
