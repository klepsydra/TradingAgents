#!/usr/bin/env python3
"""Low-priority monthly backfill: market-compare (+ full analysis if missing).

Single-file job (ROBIN market-compare backfill). Queue order:

1. Symbols with a successful TradingAgents analysis in the past 7 days that
   still lack a successful ``market_compare`` — compare only (once per symbol).
2. Then symbols present in *any* brokerage/paper portfolio that have *no*
   successful analysis yet — full ``tradingagents`` analyze (compare auto-runs
   after).

Within each phase, symbols are ordered by descending market cap (yfinance
``marketCap``, else ETF ``totalAssets``). Never processes the same symbol
twice in one invocation.

Designed to be gentle on the host:
  * ``nice`` / ``ionice`` when launched via ``--install-cron`` wrapper
  * flock so only one instance runs
  * sequential work, optional sleep between symbols
  * ``--dry-run`` / ``--limit`` for safe ops

Cron (monthly, PT wall clock on shimazu)::

    15 3 2 * *  …/scripts/backfill_market_compare_monthly.py --cron

Usage::

    python scripts/backfill_market_compare_monthly.py --dry-run
    python scripts/backfill_market_compare_monthly.py --limit 3
    python scripts/backfill_market_compare_monthly.py --install-cron
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("TRADINGAGENTS_ROOT", Path(__file__).resolve().parents[1]))
AGENTROBIN_ROOT = Path(
    os.environ.get("AGENTROBIN_ROOT", "/home/marckoby/dev/agentRobin")
)
RUNS_DIR = ROOT / "data" / "site_runs"
STATE_DIR = ROOT / "state" / "market_compare_backfill"
LOG_DIR = ROOT / "logs"
LOCK_PATH = Path(
    os.environ.get(
        "TRADINGAGENTS_BACKFILL_LOCK",
        "/tmp/tradingagents_market_compare_backfill.lock",
    )
)
CRON_TAG = "TradingAgents market-compare monthly backfill"
DEFAULT_CRON = "15 3 2 * *"  # 02nd of each month, 03:15 local (PT on shimazu)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    line = f"{iso_now()} {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "market_compare_backfill.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_runs() -> list[dict]:
    rows: list[dict] = []
    if not RUNS_DIR.is_dir():
        return rows
    for path in RUNS_DIR.glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(row, dict):
            row["_path"] = str(path)
            rows.append(row)
    return rows


def latest_ok_run(runs: list[dict], ticker: str, *, since: datetime | None = None) -> dict | None:
    ticker = ticker.upper()
    candidates: list[tuple[datetime, dict]] = []
    for row in runs:
        if str(row.get("status") or "").lower() != "ok":
            continue
        if str(row.get("ticker") or "").upper() != ticker:
            continue
        ts = parse_ts(row.get("finished_at") or row.get("created_at"))
        if ts is None:
            continue
        if since is not None and ts < since:
            continue
        candidates.append((ts, row))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def symbols_needing_compare(runs: list[dict], *, since: datetime) -> list[str]:
    """OK analyses in the window whose newest run lacks market_compare=ok."""
    recent_ok: set[str] = set()
    for row in runs:
        if str(row.get("status") or "").lower() != "ok":
            continue
        t = str(row.get("ticker") or "").upper()
        if not t:
            continue
        ts = parse_ts(row.get("finished_at") or row.get("created_at"))
        if ts is None or ts < since:
            continue
        recent_ok.add(t)
    out: list[str] = []
    for t in recent_ok:
        newest = latest_ok_run(runs, t, since=since)
        if not newest:
            continue
        mc = (newest.get("market_compare") or {}).get("status")
        if mc != "ok":
            out.append(t)
    return out


def symbols_with_any_ok_analysis(runs: list[dict]) -> set[str]:
    out: set[str] = set()
    for row in runs:
        if str(row.get("status") or "").lower() != "ok":
            continue
        t = str(row.get("ticker") or "").upper()
        if t:
            out.add(t)
    return out


def bridge_cli(*args: str, timeout: int = 180) -> Any:
    cmd = ["ruby", str(AGENTROBIN_ROOT / "scripts" / "tradingagents_bridge.rb"), *args]
    env = os.environ.copy()
    env["AGENTROBIN_ROOT"] = str(AGENTROBIN_ROOT)
    proc = subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=timeout, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or proc.stdout or f"exit {proc.returncode}")[-2000:]
        )
    return json.loads(proc.stdout)


def portfolio_tickers() -> dict[str, str | None]:
    """Map ticker → a sample account that holds it (for analyze --account)."""
    accounts = bridge_cli("accounts")
    if not isinstance(accounts, list):
        return {}
    held: dict[str, str | None] = {}
    for acct in accounts:
        account = str((acct or {}).get("account") or "").strip()
        if not account:
            continue
        try:
            book = bridge_cli("portfolio", "--account", account, "--strict", timeout=240)
        except Exception as exc:  # noqa: BLE001
            log(f"portfolio skip {account}: {exc}")
            continue
        positions = (book or {}).get("positions") if isinstance(book, dict) else None
        if not isinstance(positions, list):
            continue
        for pos in positions:
            t = str((pos or {}).get("ticker") or "").strip().upper()
            if t and t not in held:
                held[t] = account
    return held


def market_size(ticker: str) -> float:
    """Desc-sort key: equity marketCap, else ETF totalAssets, else 0."""
    try:
        import yfinance as yf  # local import keeps --help fast

        tk = yf.Ticker(ticker)
        info = {}
        try:
            info = tk.info or {}
        except Exception:  # noqa: BLE001
            info = {}
        for key in ("marketCap", "totalAssets", "enterpriseValue"):
            val = info.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return float(val)
        fi = getattr(tk, "fast_info", None)
        mc = getattr(fi, "market_cap", None) if fi is not None else None
        if isinstance(mc, (int, float)) and mc > 0:
            return float(mc)
    except Exception:  # noqa: BLE001
        return 0.0
    return 0.0


def rank_by_market_cap(tickers: list[str]) -> list[tuple[str, float]]:
    """Fetch sizes concurrently; return [(ticker, size)] sorted desc."""
    sizes: dict[str, float] = {}
    if not tickers:
        return []
    workers = min(8, max(1, len(tickers)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(market_size, t): t for t in tickers}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                sizes[t] = float(fut.result() or 0.0)
            except Exception:  # noqa: BLE001
                sizes[t] = 0.0
    ranked = sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked


def save_run(run: dict) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"{run['id']}.json"
    path.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")


def run_compare_for_symbol(runs: list[dict], ticker: str, *, since: datetime, dry_run: bool) -> str:
    newest = latest_ok_run(runs, ticker, since=since) or latest_ok_run(runs, ticker)
    if not newest:
        return "skip:no-ok-run"
    mc = (newest.get("market_compare") or {}).get("status")
    if mc == "ok":
        return "skip:compare-ok"
    if dry_run:
        return f"dry-run:compare:{newest.get('id')}"

    # Ensure API keys from .env are visible to panelists.
    load_dotenv()
    sys.path.insert(0, str(ROOT))
    from tradingagents.market_compare.pipeline import run_market_compare

    run_market_compare(newest)
    save_run(newest)
    status = (newest.get("market_compare") or {}).get("status")
    return f"compare:{status}:{newest.get('id')}"


def _load_site_server():
    """Import scripts/site_server.py by path (not a package)."""
    import importlib.util

    path = ROOT / "scripts" / "site_server.py"
    spec = importlib.util.spec_from_file_location("tradingagents_site_server", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_full_analysis(ticker: str, account: str | None, *, dry_run: bool) -> str:
    if dry_run:
        return f"dry-run:analyze:{ticker}:account={account or '-'}"

    load_dotenv()
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("TRADINGAGENTS_ROOT", str(ROOT))
    os.environ.setdefault("AGENTROBIN_ROOT", str(AGENTROBIN_ROOT))

    site = _load_site_server()
    from tradingagents.market_time import market_today_iso
    import secrets

    analysis_date = market_today_iso()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    run = {
        "id": run_id,
        "status": "running",
        "ticker": ticker,
        "account": account,
        "analysts": os.environ.get(
            "TRADINGAGENTS_BACKFILL_ANALYSTS", "market,news,fundamentals"
        ),
        "date": analysis_date,
        "created_at": iso_now(),
        "started_at": iso_now(),
        "viewer": "market-compare-backfill",
        "log": "",
        "error": None,
        "rating": None,
        "conclusion": None,
        "backfill": True,
    }
    save_run(run)

    cmd = [
        str(ROOT / ".venv" / "bin" / "tradingagents")
        if (ROOT / ".venv" / "bin" / "tradingagents").is_file()
        else "tradingagents",
        "--ticker",
        ticker,
        "--date",
        analysis_date,
        "--analysts",
        run["analysts"],
        "--no-checkpoint",
        "--save",
        "--no-show",
        "--html",
    ]
    if account:
        cmd.extend(["--account", account])

    env = site.load_site_env()
    timeout = int(os.environ.get("TRADINGAGENTS_ANALYZE_TIMEOUT", "1800"))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
        run["log"] = (proc.stdout or "")[-20000:]
        run["stderr"] = (proc.stderr or "")[-8000:]
        run["exit_code"] = proc.returncode
        run["status"] = "ok" if proc.returncode == 0 else "failed"
        if proc.returncode != 0:
            run["error"] = (proc.stderr or proc.stdout or "failed")[-2000:]
    except Exception as exc:  # noqa: BLE001
        run["status"] = "failed"
        run["error"] = str(exc)[:1500]
    run["finished_at"] = iso_now()
    site.enrich_run_artifacts(run)
    save_run(run)

    if run.get("status") == "ok":
        from tradingagents.market_compare.pipeline import run_market_compare

        for k, v in env.items():
            if (
                k.endswith("_API_KEY")
                or k.endswith("_API_KEY2")
                or k.endswith("_API_KEY3")
                or k in ("CURSOR_API_KEY", "GOOGLE_API_KEY")
                or k.startswith("TRADINGAGENTS_MARKET_COMPARE_")
            ):
                os.environ.setdefault(k, v)
        run_market_compare(run)
        save_run(run)
        mc = (run.get("market_compare") or {}).get("status")
        return f"analyze:ok:compare:{mc}:{run_id}"
    return f"analyze:failed:{run_id}:{run.get('error')}"


def acquire_lock() -> Any:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = LOCK_PATH.open("w", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise SystemExit(f"another backfill holds {LOCK_PATH}")
    fh.write(f"{os.getpid()} {iso_now()}\n")
    fh.flush()
    return fh


def install_cron() -> None:
    """Idempotently install monthly crontab entry for this script."""
    py = (
        str(ROOT / ".venv" / "bin" / "python3")
        if (ROOT / ".venv" / "bin" / "python3").is_file()
        else sys.executable
    )
    script = str(Path(__file__).resolve())
    # Low priority wrapper: nice + ionice when available.
    cmd = (
        f"cd {ROOT} && /usr/bin/nice -n 19 "
        f"/usr/bin/ionice -c3 {py} {script} --cron "
        f">>{LOG_DIR}/market_compare_backfill.cron.log 2>&1"
    )
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    existing = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True, check=False
    )
    text = existing.stdout if existing.returncode == 0 else ""
    begin = f"# BEGIN {CRON_TAG}"
    end = f"# END {CRON_TAG}"
    block = f"{begin}\n{DEFAULT_CRON} {cmd}\n{end}\n"
    if begin in text and end in text:
        pre, rest = text.split(begin, 1)
        _, post = rest.split(end, 1)
        text = pre.rstrip("\n") + "\n" + block + post.lstrip("\n")
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += block
    proc = subprocess.run(["crontab", "-"], input=text, text=True, capture_output=True)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr or proc.stdout or "crontab install failed")
    log(f"installed cron: {DEFAULT_CRON} ({CRON_TAG})")
    print(block)


def build_queue(runs: list[dict], *, days: int) -> list[dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    compare_syms = symbols_needing_compare(runs, since=since)
    analyzed = symbols_with_any_ok_analysis(runs)
    held = portfolio_tickers()
    analyze_syms = sorted(t for t in held if t not in analyzed)

    # Once per symbol across the whole job: compare phase wins if in both.
    compare_set = set(compare_syms)
    analyze_syms = [t for t in analyze_syms if t not in compare_set]

    log(f"phase1 need_compare ({days}d): {len(compare_syms)}")
    log(f"phase2 need_analyze (portfolio gaps): {len(analyze_syms)}")

    ranked_compare = rank_by_market_cap(compare_syms)
    ranked_analyze = rank_by_market_cap(analyze_syms)

    queue: list[dict[str, Any]] = []
    for t, size in ranked_compare:
        queue.append(
            {
                "ticker": t,
                "action": "compare",
                "market_size": size,
                "account": held.get(t),
            }
        )
    for t, size in ranked_analyze:
        queue.append(
            {
                "ticker": t,
                "action": "analyze",
                "market_size": size,
                "account": held.get(t),
            }
        )
    return queue


def persist_queue_snapshot(queue: list[dict[str, Any]], *, dry_run: bool) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = STATE_DIR / f"queue_{stamp}{'_dry' if dry_run else ''}.json"
    path.write_text(
        json.dumps({"created_at": iso_now(), "dry_run": dry_run, "queue": queue}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Plan only; no LLM/CLI work")
    parser.add_argument("--limit", type=int, default=0, help="Max symbols to process (0=all)")
    parser.add_argument("--days", type=int, default=7, help="Lookback for recent analyses")
    parser.add_argument(
        "--sleep",
        type=float,
        default=float(os.environ.get("TRADINGAGENTS_BACKFILL_SLEEP", "5")),
        help="Seconds between symbols (low-priority pacing)",
    )
    parser.add_argument("--cron", action="store_true", help="Cron mode (implies flock)")
    parser.add_argument(
        "--install-cron",
        action="store_true",
        help="Install/refresh monthly crontab entry and exit",
    )
    parser.add_argument(
        "--compare-only",
        action="store_true",
        help="Skip portfolio full-analysis phase",
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="Skip recent-compare phase",
    )
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    load_dotenv()
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if args.install_cron:
        install_cron()
        return 0

    lock_fh = None
    if args.cron or not args.dry_run:
        lock_fh = acquire_lock()

    try:
        log(
            f"start dry_run={args.dry_run} limit={args.limit} days={args.days} "
            f"root={ROOT}"
        )
        runs = load_runs()
        queue = build_queue(runs, days=args.days)
        if args.compare_only:
            queue = [q for q in queue if q["action"] == "compare"]
        if args.analyze_only:
            queue = [q for q in queue if q["action"] == "analyze"]
        if args.limit and args.limit > 0:
            queue = queue[: args.limit]

        snap = persist_queue_snapshot(queue, dry_run=args.dry_run)
        log(f"queue n={len(queue)} snapshot={snap}")
        for i, item in enumerate(queue, 1):
            log(
                f"  {i:03d} {item['action']:7s} {item['ticker']:8s} "
                f"size={item['market_size']:.0f} account={item.get('account') or '-'}"
            )

        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        for item in queue:
            ticker = item["ticker"]
            if ticker in seen:
                continue
            seen.add(ticker)
            action = item["action"]
            try:
                if action == "compare":
                    detail = run_compare_for_symbol(
                        runs, ticker, since=since, dry_run=args.dry_run
                    )
                else:
                    detail = run_full_analysis(
                        ticker, item.get("account"), dry_run=args.dry_run
                    )
                log(f"done {ticker} {action} -> {detail}")
                results.append({"ticker": ticker, "action": action, "detail": detail})
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                log(f"FAIL {ticker} {action}: {err}")
                traceback.print_exc()
                results.append(
                    {"ticker": ticker, "action": action, "detail": f"error:{err}"}
                )
            if args.sleep > 0 and not args.dry_run:
                time.sleep(args.sleep)

        summary_path = STATE_DIR / f"last_run.json"
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(
                {
                    "finished_at": iso_now(),
                    "dry_run": args.dry_run,
                    "results": results,
                    "queue_snapshot": str(snap),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        log(f"finished processed={len(results)} summary={summary_path}")
        return 0
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            finally:
                lock_fh.close()


if __name__ == "__main__":
    raise SystemExit(main())
