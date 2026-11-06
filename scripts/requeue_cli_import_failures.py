#!/usr/bin/env python3
"""Re-queue site analyses that failed on the TradingAgents console-script glitch.

The broken `.venv/bin/tradingagents` shim raised::

    ModuleNotFoundError: No module named 'cli'

after mise drifted the interpreter away from the venv (ROBIN-1911). This
single-file repair:

1. Scans ``data/site_runs/*.json`` for that signature.
2. Dedupes by ticker (keeps the newest failed row's account / analysts).
3. Skips tickers that already have a later ``ok`` run.
4. Re-queues one at a time (``force=True``) so LLM/API load stays polite.
5. Optionally syncs agentRobin quote-page caches after each success.

Examples::

    # Preview only
    .venv/bin/python scripts/requeue_cli_import_failures.py --dry-run

    # Repair + sync caches (default)
    .venv/bin/python scripts/requeue_cli_import_failures.py

    # Skip agentRobin sync
    .venv/bin/python scripts/requeue_cli_import_failures.py --no-sync
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TRADINGAGENTS_ROOT", str(ROOT))
os.environ.setdefault(
    "AGENTROBIN_ROOT",
    os.environ.get("AGENTROBIN_ROOT", "/home/marckoby/dev/agentRobin"),
)

# Load site .env before importing site_server (API keys, accounts, …).
_ENV = ROOT / ".env"
if _ENV.is_file():
    for _line in _ENV.read_text(encoding="utf-8").splitlines():
        _s = _line.strip()
        if not _s or _s.startswith("#") or "=" not in _s:
            continue
        _k, _v = _s.split("=", 1)
        _k, _v = _k.strip(), _v.strip().strip('"').strip("'")
        if _k and not os.environ.get(_k):
            os.environ[_k] = _v

from scripts import site_server as site  # noqa: E402

CLI_GLITCH_MARKERS = (
    "No module named 'cli'",
    "from cli.main import app",
)


def _error_blob(run: dict) -> str:
    return "\n".join(
        str(run.get(k) or "")
        for k in ("error", "conclusion", "stderr", "log")
    )


def is_cli_import_failure(run: dict) -> bool:
    if str(run.get("status") or "").lower() != "failed":
        return False
    blob = _error_blob(run)
    return any(m in blob for m in CLI_GLITCH_MARKERS)


def later_ok_exists(ticker: str, after_iso: str | None) -> dict | None:
    """Return the newest ok run for ticker finished at/after the failure."""
    best: dict | None = None
    best_ts = ""
    for row in site.list_runs(ticker=ticker, limit=200):
        if str(row.get("status") or "").lower() != "ok":
            continue
        fin = str(row.get("finished_at") or row.get("created_at") or "")
        if after_iso and fin < after_iso:
            continue
        if fin >= best_ts:
            best_ts = fin
            best = row
    return best


def discover_failures() -> list[dict]:
    """Newest cli-glitch failure per ticker, oldest-first for stable replay."""
    by_ticker: dict[str, dict] = {}
    for path in site.RUNS_DIR.glob("*.json"):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not is_cli_import_failure(run):
            continue
        ticker = str(run.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        prev = by_ticker.get(ticker)
        stamp = str(run.get("finished_at") or run.get("created_at") or "")
        prev_stamp = (
            str(prev.get("finished_at") or prev.get("created_at") or "") if prev else ""
        )
        if prev is None or stamp >= prev_stamp:
            by_ticker[ticker] = run
    items = list(by_ticker.values())
    items.sort(key=lambda r: str(r.get("finished_at") or r.get("created_at") or ""))
    return items


def sync_agentrobin(ticker: str) -> dict | None:
    root = Path(os.environ["AGENTROBIN_ROOT"])
    script = root / "scripts" / "run_tradingagents_owned_reports.rb"
    if not script.is_file():
        print(f"  sync skip: missing {script}", flush=True)
        return None
    proc = subprocess.run(
        ["ruby", str(script), "--sync-only", ticker],
        cwd=str(root),
        capture_output=True,
        text=True,
        env={**os.environ, "AGENTROBIN_ROOT": str(root)},
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        print(
            f"  sync failed exit={proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '')[:400]}",
            flush=True,
        )
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(f"  sync non-json: {(proc.stdout or '')[:400]}", flush=True)
        return None


def wait_run(run_id: str, *, timeout_s: int, poll_s: float) -> dict:
    deadline = time.time() + timeout_s
    last: dict = {}
    while time.time() < deadline:
        cur = site.load_run(run_id) or {}
        last = cur
        st = str(cur.get("status") or "")
        print(
            f"  poll {run_id} status={st or '…'} exit={cur.get('exit_code')}",
            flush=True,
        )
        if st in ("ok", "failed"):
            return cur
        time.sleep(poll_s)
    last = dict(last)
    last["status"] = last.get("status") or "timeout"
    last["error"] = (last.get("error") or "") + f"\nwait timed out after {timeout_s}s"
    return last


def plan_repairs(failures: list[dict]) -> list[dict]:
    plan: list[dict] = []
    for fail in failures:
        ticker = str(fail.get("ticker") or "").upper()
        finished = str(fail.get("finished_at") or fail.get("created_at") or "")
        healed = later_ok_exists(ticker, finished)
        entry = {
            "ticker": ticker,
            "failed_id": fail.get("id"),
            "account": fail.get("account"),
            "analysts": fail.get("analysts") or "market,news,fundamentals",
            "viewer_orig": fail.get("viewer"),
            "failed_at": finished,
            "action": "requeue",
            "healed_by": None,
        }
        if healed:
            entry["action"] = "sync_only"
            entry["healed_by"] = healed.get("id")
            entry["healed_rating"] = healed.get("rating")
        plan.append(entry)
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without queueing or syncing",
    )
    ap.add_argument(
        "--no-sync",
        action="store_true",
        help="Do not call agentRobin --sync-only after successes",
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("TRADINGAGENTS_ANALYZE_TIMEOUT", "1800")),
        help="Per-run wait seconds (default: TRADINGAGENTS_ANALYZE_TIMEOUT or 1800)",
    )
    ap.add_argument(
        "--poll",
        type=float,
        default=15.0,
        help="Seconds between status polls (default: 15)",
    )
    ap.add_argument(
        "--pause",
        type=float,
        default=5.0,
        help="Seconds to pause between successful requeues (default: 5)",
    )
    ap.add_argument(
        "--viewer",
        default="cli-glitch-repair",
        help="viewer label written on new runs",
    )
    args = ap.parse_args()

    failures = discover_failures()
    plan = plan_repairs(failures)
    print(
        json.dumps(
            {
                "glitch_failures": len(failures),
                "plan": [
                    {
                        "ticker": p["ticker"],
                        "action": p["action"],
                        "failed_id": p["failed_id"],
                        "account": p["account"],
                        "healed_by": p.get("healed_by"),
                    }
                    for p in plan
                ],
            },
            indent=2,
        ),
        flush=True,
    )
    if args.dry_run:
        return 0

    results: list[dict] = []
    for i, item in enumerate(plan):
        ticker = item["ticker"]
        row: dict = {"ticker": ticker, "action": item["action"]}
        if item["action"] == "sync_only":
            print(
                f"[{i + 1}/{len(plan)}] {ticker}: already ok via {item['healed_by']}"
                f" ({item.get('healed_rating')})",
                flush=True,
            )
            if not args.no_sync:
                synced = sync_agentrobin(ticker)
                row["synced"] = bool(synced)
                row["cache_status"] = (synced or {}).get("status")
                row["cache_run_id"] = (synced or {}).get("run_id")
            results.append(row)
            continue

        print(
            f"[{i + 1}/{len(plan)}] {ticker}: requeue account={item['account']} "
            f"analysts={item['analysts']} (was {item['failed_id']})",
            flush=True,
        )
        run = site.queue_analyze(
            ticker=ticker,
            account=item["account"],
            analysts=item["analysts"],
            viewer=args.viewer,
            force=True,
        )
        run_id = str(run.get("id") or "")
        row["queued_id"] = run_id
        print(f"  queued {run_id} status={run.get('status')}", flush=True)
        finished = wait_run(run_id, timeout_s=args.timeout, poll_s=args.poll)
        row["status"] = finished.get("status")
        row["rating"] = finished.get("rating")
        row["error"] = (finished.get("error") or "")[:300] or None
        print(
            f"  done status={row['status']} rating={row['rating']}",
            flush=True,
        )
        if row["status"] == "ok" and not args.no_sync:
            synced = sync_agentrobin(ticker)
            row["synced"] = bool(synced)
            row["cache_status"] = (synced or {}).get("status")
            row["cache_run_id"] = (synced or {}).get("run_id")
        results.append(row)
        if row["status"] == "ok" and i + 1 < len(plan) and args.pause > 0:
            time.sleep(args.pause)

    summary = {
        "ok": sum(1 for r in results if r.get("status") == "ok" or r.get("action") == "sync_only"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
        "results": results,
    }
    print(json.dumps(summary, indent=2), flush=True)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
