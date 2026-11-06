"""Persistent registry + usage stats for market-compare research sources.

Settings UI (ROBIN-1906) lists every gather source with URL templates, samples,
edit/disable, and usage (count, last used, daily/weekly). Stats are recorded
when a compare finishes and can be backfilled from existing ``site_runs``.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .sources import source_catalog

_LOCK = threading.RLock()
_SAMPLE_STOCK = "AAPL"
_SAMPLE_ETF = "VOO"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def registry_path() -> Path:
    root = Path(
        os.environ.get(
            "TRADINGAGENTS_SITE_SETTINGS",
            Path(os.environ.get("TRADINGAGENTS_ROOT", Path.home() / "dev" / "TradingAgents"))
            / "data"
            / "site_settings",
        )
    )
    # If TRADINGAGENTS_SITE_SETTINGS points at a file, use it; else dir/compare_sources.json
    if root.suffix.lower() == ".json":
        root.parent.mkdir(parents=True, exist_ok=True)
        return root
    root.mkdir(parents=True, exist_ok=True)
    return root / "compare_sources.json"


def runs_dir() -> Path:
    return Path(
        os.environ.get(
            "TRADINGAGENTS_RUNS_DIR",
            Path(os.environ.get("TRADINGAGENTS_ROOT", Path.home() / "dev" / "TradingAgents"))
            / "data"
            / "site_runs",
        )
    )


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    return s or "source"


def infer_url_template(url: str, ticker: str) -> str:
    t = (ticker or "").strip()
    if not t or not url:
        return url or ""
    out = url
    # Longer forms first (avoid partial replace issues).
    for needle, token in (
        (t.upper(), "{TICKER}"),
        (t.lower(), "{ticker}"),
    ):
        out = out.replace(needle, token)
    return out


def apply_url_template(template: str, ticker: str) -> str:
    t = (ticker or "").strip().upper()
    tl = t.lower()
    return (
        (template or "")
        .replace("{TICKER}", t)
        .replace("{ticker}", tl)
        .replace("{symbol}", t)
        .replace("{Symbol}", t)
    )


def empty_stats() -> dict[str, Any]:
    return {
        "use_count": 0,
        "ok_count": 0,
        "skip_count": 0,
        "last_used_at": None,
        "daily": {},
        "weekly": {},
        "report_ids": [],
        "parsed_urls": {},
    }


def builtin_source_defs() -> list[dict[str, Any]]:
    """Derive stable source definitions from the live catalog (stock + ETF shapes)."""
    by_id: dict[str, dict[str, Any]] = {}
    for sample, kind_hint in ((_SAMPLE_STOCK, "stock"), (_SAMPLE_ETF, "etf")):
        for row in source_catalog(sample):
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            sid = slugify(name)
            url = str(row.get("url") or "")
            template = infer_url_template(url, sample)
            sample_url = apply_url_template(template, sample)
            group = str(row.get("group") or "media")
            existing = by_id.get(sid)
            if existing is None:
                by_id[sid] = {
                    "id": sid,
                    "name": name,
                    "display_name": name,
                    "group": group,
                    "url_template": template,
                    "sample_url": sample_url,
                    "sample_ticker": sample,
                    "asset_kinds": [kind_hint],
                    "enabled": True,
                    "added_at": None,  # filled on first persist
                    "notes": "",
                    "builtin": True,
                }
            else:
                if kind_hint not in existing["asset_kinds"]:
                    existing["asset_kinds"].append(kind_hint)
                # Prefer stock template when both exist under same name (rare).
                if kind_hint == "stock":
                    existing["url_template"] = template
                    existing["sample_url"] = sample_url
                    existing["sample_ticker"] = sample
    return sorted(by_id.values(), key=lambda r: (r.get("group") or "", r.get("name") or ""))


def _default_store() -> dict[str, Any]:
    now = iso_now()
    sources = []
    for row in builtin_source_defs():
        sources.append(
            {
                **row,
                "added_at": now,
                "stats": empty_stats(),
            }
        )
    return {
        "version": 1,
        "updated_at": now,
        "backfilled_at": None,
        "sources": sources,
    }


def load_store(*, ensure: bool = True) -> dict[str, Any]:
    path = registry_path()
    with _LOCK:
        if not path.is_file():
            store = _default_store()
            if ensure:
                _write_store_unlocked(store)
            return store
        try:
            store = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            store = _default_store()
            if ensure:
                _write_store_unlocked(store)
            return store
        if not isinstance(store, dict):
            store = _default_store()
        store.setdefault("sources", [])
        store = merge_builtins(store)
        return store


def _write_store_unlocked(store: dict[str, Any]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = dict(store)
    payload["updated_at"] = iso_now()
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def save_store(store: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        _write_store_unlocked(store)
        return store


def merge_builtins(store: dict[str, Any]) -> dict[str, Any]:
    """Ensure newly added catalog sources appear in the registry."""
    existing = {str(s.get("id")): s for s in store.get("sources") or [] if isinstance(s, dict)}
    now = iso_now()
    changed = False
    for builtin in builtin_source_defs():
        sid = builtin["id"]
        if sid not in existing:
            row = {**builtin, "added_at": now, "stats": empty_stats()}
            store.setdefault("sources", []).append(row)
            existing[sid] = row
            changed = True
        else:
            row = existing[sid]
            # Keep user display_name / enabled / notes / url_template overrides;
            # refresh canonical name/group/sample when missing.
            if not row.get("name"):
                row["name"] = builtin["name"]
                changed = True
            if not row.get("group"):
                row["group"] = builtin["group"]
                changed = True
            if not row.get("url_template"):
                row["url_template"] = builtin["url_template"]
                row["sample_url"] = builtin["sample_url"]
                changed = True
            if not row.get("added_at"):
                row["added_at"] = now
                changed = True
            row.setdefault("stats", empty_stats())
            row["builtin"] = True
    if changed:
        store["updated_at"] = now
    return store


def list_sources(*, include_disabled: bool = True) -> list[dict[str, Any]]:
    store = load_store()
    rows = [dict(s) for s in store.get("sources") or [] if isinstance(s, dict)]
    if not include_disabled:
        rows = [s for s in rows if s.get("enabled", True)]
    rows.sort(key=lambda r: (not r.get("enabled", True), r.get("group") or "", r.get("display_name") or r.get("name") or ""))
    return rows


def get_source(source_id: str) -> dict[str, Any] | None:
    sid = slugify(source_id) if source_id else ""
    for row in list_sources(include_disabled=True):
        if row.get("id") == sid or slugify(str(row.get("name") or "")) == sid:
            return row
    return None


def update_source(source_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    sid = (source_id or "").strip()
    with _LOCK:
        store = load_store()
        target = None
        for row in store.get("sources") or []:
            if not isinstance(row, dict):
                continue
            if row.get("id") == sid or slugify(str(row.get("name") or "")) == sid:
                target = row
                break
        if target is None:
            raise KeyError(f"source not found: {sid}")

        if "display_name" in patch:
            name = str(patch.get("display_name") or "").strip()
            if name:
                target["display_name"] = name
        if "url_template" in patch:
            tmpl = str(patch.get("url_template") or "").strip()
            if tmpl:
                target["url_template"] = tmpl
                sample_ticker = str(target.get("sample_ticker") or _SAMPLE_STOCK)
                target["sample_url"] = apply_url_template(tmpl, sample_ticker)
        if "sample_url" in patch and patch.get("sample_url") is not None:
            target["sample_url"] = str(patch.get("sample_url") or "").strip()
        if "enabled" in patch:
            target["enabled"] = bool(patch.get("enabled"))
        if "notes" in patch and patch.get("notes") is not None:
            target["notes"] = str(patch.get("notes") or "")[:2000]
        if "group" in patch and patch.get("group"):
            target["group"] = str(patch.get("group")).strip()[:64]

        _write_store_unlocked(store)
        return dict(target)


def disabled_source_names() -> set[str]:
    return {
        str(s.get("name") or "")
        for s in list_sources(include_disabled=True)
        if not s.get("enabled", True)
    }


def display_name_map() -> dict[str, str]:
    """Canonical catalog name → display_name override."""
    out: dict[str, str] = {}
    for s in list_sources(include_disabled=True):
        name = str(s.get("name") or "")
        disp = str(s.get("display_name") or name)
        if name and disp and disp != name:
            out[name] = disp
    return out


def template_overrides() -> dict[str, str]:
    out: dict[str, str] = {}
    builtins = {b["id"]: b for b in builtin_source_defs()}
    for s in list_sources(include_disabled=True):
        sid = str(s.get("id") or "")
        tmpl = str(s.get("url_template") or "")
        builtin = builtins.get(sid)
        if tmpl and (not builtin or tmpl != builtin.get("url_template")):
            out[str(s.get("name") or "")] = tmpl
    return out


def _day_week_keys(when: datetime | None = None) -> tuple[str, str]:
    dt = when or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    day = dt.date().isoformat()
    iso = dt.isocalendar()
    week = f"{iso.year}-W{iso.week:02d}"
    return day, week


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def record_source_use(
    *,
    source_name: str,
    url: str | None,
    run_id: str | None,
    ok: bool | None = None,
    skipped: bool | None = None,
    when: str | None = None,
) -> None:
    """Increment usage for one source on a compare run."""
    name = (source_name or "").strip()
    if not name:
        return
    sid = slugify(name)
    when_dt = _parse_iso(when) or datetime.now(timezone.utc)
    day, week = _day_week_keys(when_dt)
    with _LOCK:
        store = load_store()
        target = None
        for row in store.get("sources") or []:
            if not isinstance(row, dict):
                continue
            if row.get("id") == sid or str(row.get("name") or "") == name:
                target = row
                break
        if target is None:
            # Unknown / renamed source — create a stub entry.
            target = {
                "id": sid,
                "name": name,
                "display_name": name,
                "group": "unknown",
                "url_template": infer_url_template(url or "", "AAPL") if url else "",
                "sample_url": url or "",
                "sample_ticker": "AAPL",
                "asset_kinds": [],
                "enabled": True,
                "added_at": iso_now(),
                "notes": "Seen in gather; not in builtin catalog",
                "builtin": False,
                "stats": empty_stats(),
            }
            store.setdefault("sources", []).append(target)

        stats = target.setdefault("stats", empty_stats())
        stats["use_count"] = int(stats.get("use_count") or 0) + 1
        if skipped:
            stats["skip_count"] = int(stats.get("skip_count") or 0) + 1
        elif ok:
            stats["ok_count"] = int(stats.get("ok_count") or 0) + 1
        ts = when_dt.isoformat()
        prev = stats.get("last_used_at")
        if not prev or str(prev) < ts:
            stats["last_used_at"] = ts
        daily = stats.setdefault("daily", {})
        weekly = stats.setdefault("weekly", {})
        daily[day] = int(daily.get(day) or 0) + 1
        weekly[week] = int(weekly.get(week) or 0) + 1
        # Trim frequency maps
        for key in sorted(daily.keys())[:-60]:
            daily.pop(key, None)
        for key in sorted(weekly.keys())[:-26]:
            weekly.pop(key, None)
        if run_id:
            ids = stats.setdefault("report_ids", [])
            if run_id not in ids:
                ids.append(run_id)
            # Cap remembered report links
            if len(ids) > 500:
                stats["report_ids"] = ids[-500:]
        if url:
            parsed = stats.setdefault("parsed_urls", {})
            entry = parsed.get(url) or {"count": 0, "last_seen": None, "ok": None}
            entry["count"] = int(entry.get("count") or 0) + 1
            entry["last_seen"] = ts
            if ok is not None:
                entry["ok"] = bool(ok)
            parsed[url] = entry
            # Cap remembered material URLs
            if len(parsed) > 200:
                # Drop oldest by last_seen
                ordered = sorted(
                    parsed.items(),
                    key=lambda kv: str((kv[1] or {}).get("last_seen") or ""),
                )
                for stale_url, _ in ordered[: len(parsed) - 200]:
                    parsed.pop(stale_url, None)
        _write_store_unlocked(store)


def record_compare_usage(run: dict[str, Any]) -> None:
    """Record all page_hits / source_urls from a finished market_compare blob."""
    run_id = str(run.get("id") or "")
    mc = run.get("market_compare") or {}
    if not isinstance(mc, dict):
        return
    when = mc.get("finished_at") or mc.get("started_at") or run.get("finished_at") or run.get("created_at")
    gather = mc.get("gather") or {}
    hits = gather.get("page_hits") or []
    # Only page-level gather hits count as "used / contributed" (not merely listed
    # in the catalog for that run). Remembered material URLs come from these hits.
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        name = str(hit.get("name") or "").strip()
        if not name or name == "error":
            continue
        record_source_use(
            source_name=name,
            url=str(hit.get("url") or "") or None,
            run_id=run_id or None,
            ok=bool(hit.get("ok")),
            skipped=bool(hit.get("skipped")),
            when=when,
        )


def frequency_summary(stats: dict[str, Any] | None) -> dict[str, Any]:
    stats = stats or {}
    daily = stats.get("daily") or {}
    weekly = stats.get("weekly") or {}
    day, week = _day_week_keys()
    # last 7 days sum
    days_sorted = sorted(daily.keys())[-7:]
    weeks_sorted = sorted(weekly.keys())[-4:]
    return {
        "today": int(daily.get(day) or 0),
        "this_week": int(weekly.get(week) or 0),
        "last_7_days": sum(int(daily.get(d) or 0) for d in days_sorted),
        "last_4_weeks": sum(int(weekly.get(w) or 0) for w in weeks_sorted),
        "daily": {k: daily[k] for k in days_sorted},
        "weekly": {k: weekly[k] for k in weeks_sorted},
    }


def source_reports(source_id: str) -> dict[str, Any]:
    src = get_source(source_id)
    if not src:
        raise KeyError(f"source not found: {source_id}")
    stats = src.get("stats") or {}
    report_ids = list(stats.get("report_ids") or [])
    reports: list[dict[str, Any]] = []
    root = runs_dir()
    for rid in reversed(report_ids):
        path = root / f"{rid}.json"
        if not path.is_file():
            reports.append({"id": rid, "missing": True})
            continue
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            reports.append({"id": rid, "missing": True})
            continue
        mc = run.get("market_compare") or {}
        gather = mc.get("gather") or {}
        hits = [
            h
            for h in (gather.get("page_hits") or [])
            if isinstance(h, dict)
            and (
                str(h.get("name") or "") == str(src.get("name") or "")
                or slugify(str(h.get("name") or "")) == src.get("id")
            )
        ]
        reports.append(
            {
                "id": rid,
                "ticker": run.get("ticker"),
                "status": run.get("status"),
                "rating": run.get("rating"),
                "created_at": run.get("created_at"),
                "compare_status": mc.get("status"),
                "run_url": f"/runs/{rid}",
                "report_url": f"/runs/{rid}/report" if (run.get("report_html") or run.get("report_md") or mc.get("status") == "ok") else None,
                "hits": [
                    {
                        "url": h.get("url"),
                        "ok": h.get("ok"),
                        "skipped": h.get("skipped"),
                        "skip_reason": h.get("skip_reason"),
                        "http_status": h.get("http_status"),
                        "from_cache": h.get("from_cache"),
                        "snippet": (h.get("snippet") or "")[:280],
                    }
                    for h in hits
                ],
            }
        )
    parsed_urls = [
        {"url": u, **(meta if isinstance(meta, dict) else {})}
        for u, meta in sorted(
            (stats.get("parsed_urls") or {}).items(),
            key=lambda kv: str((kv[1] or {}).get("last_seen") or ""),
            reverse=True,
        )
    ]
    return {
        "source": {
            **src,
            "frequency": frequency_summary(stats),
        },
        "reports": reports,
        "parsed_urls": parsed_urls,
    }


def backfill_from_runs(*, limit: int | None = None, reset_stats: bool = True) -> dict[str, Any]:
    """Rebuild usage stats from on-disk site runs that have market_compare data."""
    with _LOCK:
        store = load_store()
        if reset_stats:
            for row in store.get("sources") or []:
                if isinstance(row, dict):
                    row["stats"] = empty_stats()
            _write_store_unlocked(store)

    root = runs_dir()
    paths = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if limit is not None:
        paths = paths[-limit:]
    scanned = 0
    with_compare = 0
    for path in paths:
        scanned += 1
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        mc = run.get("market_compare")
        if not isinstance(mc, dict) or not mc:
            continue
        if mc.get("status") not in ("ok", "failed", "running"):
            # still count ok/failed primarily; skip empty placeholders
            if not (mc.get("gather") or mc.get("source_urls")):
                continue
        with_compare += 1
        if not run.get("id"):
            run["id"] = path.stem
        record_compare_usage(run)

    with _LOCK:
        store = load_store()
        store["backfilled_at"] = iso_now()
        _write_store_unlocked(store)

    return {
        "ok": True,
        "scanned_runs": scanned,
        "compare_runs": with_compare,
        "sources": len(list_sources(include_disabled=True)),
        "backfilled_at": store.get("backfilled_at"),
        "registry_path": str(registry_path()),
    }


def effective_source_catalog(ticker: str) -> list[dict[str, Any]]:
    """Catalog for gather: apply disable + rename + template overrides."""
    disabled = disabled_source_names()
    renames = display_name_map()
    templates = template_overrides()
    out: list[dict[str, Any]] = []
    for row in source_catalog(ticker):
        name = str(row.get("name") or "")
        if name in disabled:
            continue
        item = dict(row)
        if name in templates:
            item["url"] = apply_url_template(templates[name], ticker)
        if name in renames:
            item["name"] = renames[name]
            item["canonical_name"] = name
        item["source_id"] = slugify(name)
        out.append(item)
    return out
