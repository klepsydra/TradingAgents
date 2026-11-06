#!/usr/bin/env python3
"""TradingAgents site for https://TradingAgents.quiltyapp.com (ROBIN-1685 / 1686).

Local bind 127.0.0.1:8794; nginx terminates TLS. Auth mirrors agents.quiltyapp.com:
webbit Spina SSO → /sso/callback (shared agentRobin state/agent_ops/sso_secret).

Deep-link autolaunch (from webbit quote pages):
  GET /start?ticker=VOO&account=FakeAcct.01  → queues research, redirects to /runs/<id>

Report summary / cadence APIs (session cookie or service token):
  GET  /api/reports/latest?ticker=VOO
  POST /api/reports/refresh  JSON {ticker, account?}
Market-analyst compare (multi-LLM panel vs street/Cortex sources):
  GET  /api/runs/<id>/market-compare
  POST /api/runs/<id>/market-compare  JSON {force?}
  (also auto-queued after a successful analyze; shown on /runs/<id> and /report)
Service token: TRADINGAGENTS_SERVICE_TOKEN or AGENTROBIN_TRADINGAGENTS_BRIDGE_TOKEN
(header X-TradingAgents-Service-Token / X-TradingAgents-Bridge-Token / Bearer).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tradingagents.market_time import market_today_iso, market_tz
from tradingagents.market_compare.pipeline import run_market_compare
from tradingagents.market_compare.render import (
    render_market_compare_section,
    render_report_compare_banner,
)
from tradingagents.market_compare import source_registry as compare_sources
from tradingagents.site_run_limits import (
    DEFAULT_MAX_ANALYSES_PER_TICKER_DAY,
    run_day_key,
    select_coalesce_target,
    sort_runs_newest_first,
)

ROOT = Path(os.environ.get("TRADINGAGENTS_ROOT", Path(__file__).resolve().parents[1]))
AGENTROBIN_ROOT = Path(
    os.environ.get("AGENTROBIN_ROOT", "/home/marckoby/dev/agentRobin")
)
PORT = int(os.environ.get("TRADINGAGENTS_SITE_PORT", "8794"))
BIND = os.environ.get("TRADINGAGENTS_SITE_BIND", "127.0.0.1")
PUBLIC_URL = os.environ.get(
    "TRADINGAGENTS_PUBLIC_URL", "https://TradingAgents.quiltyapp.com"
).rstrip("/")
ALLOWED_PUBLIC_HOSTS = {
    "tradingagents.quiltyapp.com",
    "TradingAgents.quiltyapp.com",
}
SSO_URL = os.environ.get(
    "TRADINGAGENTS_SSO_URL",
    "https://webbit.quiltyapp.com/agentrobin/internal/agents_sso",
)
AUTH = os.environ.get("TRADINGAGENTS_SITE_AUTH", "sso")  # sso | disabled
COOKIE = "tradingagents_session"
ACCOUNT_COOKIE = "tradingagents_account"
SESSION_HOURS = 12
RUNS_DIR = ROOT / "data" / "site_runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)
# Keep compare-source registry under this site's data/ tree.
os.environ.setdefault("TRADINGAGENTS_ROOT", str(ROOT))
os.environ.setdefault("TRADINGAGENTS_RUNS_DIR", str(RUNS_DIR))
WEB_DIR = ROOT / "web"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR = Path(
    os.environ.get(
        "TRADINGAGENTS_RESULTS_DIR",
        Path.home() / ".tradingagents" / "logs",
    )
)

_RATING_RE = re.compile(
    r"(?<![a-z])rating\b[^:\-\u2010-\u2015]*[:\-\u2010-\u2015][\s*]*(\w+)",
    re.IGNORECASE,
)
_RATINGS = {"buy", "overweight", "hold", "underweight", "sell"}


def canonical_public_host(host: str | None) -> str:
    h = (host or "").split(":")[0].strip()
    if h.lower() == "tradingagents.quiltyapp.com" or h in ALLOWED_PUBLIC_HOSTS:
        return "TradingAgents.quiltyapp.com"
    return urllib.parse.urlparse(PUBLIC_URL).hostname or "TradingAgents.quiltyapp.com"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def sso_secret() -> str:
    path = AGENTROBIN_ROOT / "state" / "agent_ops" / "sso_secret"
    if not path.is_file() or path.stat().st_size < 32:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding="utf-8")
        path.chmod(0o600)
    return path.read_text(encoding="utf-8").strip()


def sso_sign(payload: dict) -> str:
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    mac = _b64url(
        hmac.new(sso_secret().encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    )
    return f"{body}.{mac}"


def sso_verify(token: str) -> dict | None:
    try:
        body, mac = token.split(".", 1)
    except ValueError:
        return None
    expected = _b64url(
        hmac.new(sso_secret().encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(expected, mac):
        return None
    try:
        payload = json.loads(_b64url_decode(body))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or int(payload.get("exp", 0)) <= int(time.time()):
        return None
    return payload


def bridge_cli(*args: str) -> dict | list:
    cmd = ["ruby", str(AGENTROBIN_ROOT / "scripts" / "tradingagents_bridge.rb"), *args]
    env = os.environ.copy()
    env["AGENTROBIN_ROOT"] = str(AGENTROBIN_ROOT)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")
    return json.loads(proc.stdout)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def analysis_date_today() -> str:
    """US equity calendar date (not host-local) so CLI accepts site --date near midnight ET."""
    return market_today_iso()


def web_text(name: str) -> str:
    return (WEB_DIR / name).read_text(encoding="utf-8")


def run_path(run_id: str) -> Path:
    return RUNS_DIR / f"{run_id}.json"


def load_run(run_id: str) -> dict | None:
    path = run_path(run_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_run(run: dict) -> None:
    run_path(run["id"]).write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")


def list_runs(*, ticker: str | None = None, limit: int = 80) -> list[dict]:
    items: list[dict] = []
    for path in sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if ticker and str(row.get("ticker") or "").upper() != ticker.upper():
            continue
        items.append(summarize_run(row))
        if len(items) >= limit:
            break
    return items


def summarize_run(run: dict) -> dict:
    """Compact fields for tables / API lists."""
    run_id = run.get("id")
    report_path = f"/runs/{run_id}/report" if run_id and (run.get("report_html") or run.get("report_md")) else None
    out = {
        "id": run_id,
        "status": run.get("status"),
        "ticker": run.get("ticker"),
        "account": run.get("account"),
        "created_at": run.get("created_at"),
        "finished_at": run.get("finished_at"),
        "viewer": run.get("viewer"),
        "rating": run.get("rating"),
        "conclusion": (run.get("conclusion") or "")[:280],
        "date": run.get("date"),
        "error": (run.get("error") or "")[:200] or None,
        "has_report": bool(run.get("report_html") or run.get("report_md")),
        "report_url": run.get("report_url") or report_path,
        "run_url": f"/runs/{run_id}" if run_id else None,
        "public_url": f"{PUBLIC_URL}/runs/{run_id}" if run_id else None,
    }
    if run.get("coalesced"):
        out["coalesced"] = True
        out["coalesce_reason"] = run.get("coalesce_reason")
        out["repeat_requests"] = run.get("repeat_requests")
    return out


def max_analyses_per_ticker_day() -> int:
    raw = (os.environ.get("TRADINGAGENTS_MAX_ANALYSES_PER_TICKER_DAY") or "").strip()
    if not raw:
        return DEFAULT_MAX_ANALYSES_PER_TICKER_DAY
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_ANALYSES_PER_TICKER_DAY


def list_full_runs_for_ticker_day(ticker: str, day: str) -> list[dict]:
    """Full run dicts for ticker on a market calendar day (newest first)."""
    ticker = ticker.strip().upper()
    day = (day or "").strip()[:10]
    tz = market_tz()
    items: list[dict] = []
    for path in RUNS_DIR.glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if str(row.get("ticker") or "").upper() != ticker:
            continue
        if run_day_key(row, market_tz=tz) != day:
            continue
        items.append(row)
    return sort_runs_newest_first(items)


def latest_report_for_ticker(ticker: str) -> dict | None:
    """Most recent completed (ok) run for ticker, else newest run of any status."""
    ticker = ticker.strip().upper()
    if not ticker:
        return None
    rows = list_runs(ticker=ticker, limit=80)
    for row in rows:
        if row.get("status") == "ok" and (row.get("rating") or row.get("has_report") or row.get("conclusion")):
            return row
    for row in rows:
        if row.get("status") == "ok":
            return row
    return rows[0] if rows else None


def service_token() -> str:
    """Shared secret for agentRobin/webbit → site API (cadence + quote summary)."""
    for key in (
        "TRADINGAGENTS_SERVICE_TOKEN",
        "AGENTROBIN_TRADINGAGENTS_BRIDGE_TOKEN",
        "AGENTROBIN_PROXY_AUTH_TOKEN",
    ):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    # Fall back to SSO secret so localhost callers with agent_ops access still work.
    try:
        return sso_secret()
    except Exception:  # noqa: BLE001
        return ""


def _extract_rating(text: str) -> str | None:
    if not text:
        return None
    found = [m.group(1).capitalize() for m in _RATING_RE.finditer(text) if m.group(1).lower() in _RATINGS]
    if found and len(set(found)) == 1:
        return found[0]
    return found[0] if found else None


def _latest_report_dir(ticker: str, after_ts: float | None = None) -> Path | None:
    reports = RESULTS_DIR / "reports"
    if not reports.is_dir():
        # Also check per-ticker date folders for complete_report.*
        ticker_dir = RESULTS_DIR / ticker.upper()
        if ticker_dir.is_dir():
            candidates = sorted(
                ticker_dir.rglob("complete_report.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for md in candidates:
                if after_ts and md.stat().st_mtime < after_ts - 5:
                    continue
                return md.parent
        return None
    prefix = f"{ticker.upper()}_"
    dirs = [
        p
        for p in reports.iterdir()
        if p.is_dir() and p.name.upper().startswith(prefix)
    ]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for d in dirs:
        if after_ts and d.stat().st_mtime < after_ts - 5:
            continue
        if (d / "complete_report.md").is_file() or (d / "complete_report.html").is_file():
            return d
    return dirs[0] if dirs else None


def enrich_run_artifacts(run: dict, *, started_monotonic: float | None = None) -> None:
    """Attach rating / conclusion / report paths from on-disk CLI output."""
    ticker = str(run.get("ticker") or "").upper()
    if not ticker:
        return
    after = None
    if started_monotonic is not None:
        # Convert approx wall time: started_at ISO is better when present.
        pass
    started_at = run.get("started_at")
    after_ts = None
    if started_at:
        try:
            after_ts = datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            after_ts = None

    report_dir = _latest_report_dir(ticker, after_ts=after_ts)
    decision_text = ""
    if report_dir:
        md = report_dir / "complete_report.md"
        html = report_dir / "complete_report.html"
        decision = report_dir / "5_portfolio" / "decision.md"
        if decision.is_file():
            decision_text = decision.read_text(encoding="utf-8", errors="replace")
        elif md.is_file():
            decision_text = md.read_text(encoding="utf-8", errors="replace")
        run["report_dir"] = str(report_dir)
        if md.is_file():
            run["report_md"] = str(md)
        if html.is_file():
            run["report_html"] = str(html)
            run["report_url"] = f"/runs/{run['id']}/report"

    blob = "\n".join(
        [
            decision_text,
            run.get("log") or "",
            run.get("stderr") or "",
        ]
    )
    rating = _extract_rating(decision_text) or _extract_rating(blob)
    if rating:
        run["rating"] = rating
    if decision_text.strip():
        # First meaningful paragraph as conclusion teaser.
        paras = [p.strip() for p in re.split(r"\n\s*\n", decision_text) if p.strip()]
        run["conclusion"] = (paras[0] if paras else decision_text.strip())[:1200]
        run["decision"] = decision_text[-20000:]
    elif run.get("error"):
        run["conclusion"] = str(run["error"])[:500]


def _env_blank(env: dict[str, str], key: str) -> bool:
    return not str(env.get(key, "")).strip()


def load_site_env() -> dict[str, str]:
    """Process env + .env, filling blank unattended CLI knobs the site requires."""
    env = os.environ.copy()
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            # Fill missing or empty keys from .env (empty systemd values used to
            # block setdefault and trip unattended_gaps).
            if k and _env_blank(env, k):
                env[k] = v
    env["AGENTROBIN_ROOT"] = str(AGENTROBIN_ROOT)
    env["TRADINGAGENTS_ROOT"] = str(ROOT)
    # Belt-and-suspenders so `python -m cli.main` works even if the editable
    # install is missing after a mise/python drift (see ROBIN-1907 notes).
    extra = str(ROOT)
    existing = env.get("PYTHONPATH") or ""
    if extra not in existing.split(os.pathsep):
        env["PYTHONPATH"] = extra + ((os.pathsep + existing) if existing else "")
    for key, value in (
        ("TRADINGAGENTS_OUTPUT_LANGUAGE", "English"),
        ("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "1"),
        ("TRADINGAGENTS_MAX_RISK_ROUNDS", "1"),
        ("TRADINGAGENTS_LLM_PROVIDER", "openai"),
        # Match tradingagents.default_config + model_catalog (gpt-4o retired).
        ("TRADINGAGENTS_DEEP_THINK_LLM", "gpt-6-sol"),
        ("TRADINGAGENTS_QUICK_THINK_LLM", "gpt-6-luna"),
        ("TRADINGAGENTS_NO_UPDATE_CHECK", "1"),
    ):
        if _env_blank(env, key):
            env[key] = value
    return env


def analyze_cli_argv() -> list[str]:
    """Prefer ``.venv/bin/python -m cli.main`` over the console script.

    The console script fails with ``ModuleNotFoundError: cli`` when the venv
    interpreter drifts (e.g. mise ``python/latest`` moved 3.14 → 3.15 while
    site-packages stayed on 3.14). Invoking via the venv python + PYTHONPATH
    is more resilient.
    """
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return [str(venv_python), "-m", "cli.main"]
    venv_ta = ROOT / ".venv" / "bin" / "tradingagents"
    if venv_ta.is_file():
        return [str(venv_ta)]
    return ["tradingagents"]


def queue_analyze(
    *,
    ticker: str,
    account: str | None,
    analysts: str,
    viewer: str | None,
    analysis_date: str | None = None,
    watchlist_id: str | None = None,
    force: bool = False,
) -> dict:
    ticker = ticker.strip().upper()
    account = (account or "").strip() or None
    watchlist_id = (watchlist_id or "").strip() or None
    analysts = (analysts or "market,news,fundamentals").strip()
    analysis_date = analysis_date or analysis_date_today()

    # ≤2 analyses / ticker / market day; fold repeats into the most recent.
    if not force:
        today_runs = list_full_runs_for_ticker_day(ticker, analysis_date)
        existing, reason = select_coalesce_target(
            today_runs, max_per_day=max_analyses_per_ticker_day()
        )
        if existing is not None:
            # Re-load to reduce races with the background worker.
            latest = load_run(str(existing.get("id") or "")) or dict(existing)
            latest["repeat_requests"] = int(latest.get("repeat_requests") or 0) + 1
            latest["last_request_at"] = iso_now()
            if viewer:
                latest["last_viewer"] = viewer
            # Book context on a still-queued run is best-effort (worker may
            # already have captured the original account/watchlist).
            if str(latest.get("status") or "").lower() == "queued":
                if account:
                    latest["account"] = account
                if watchlist_id:
                    latest["watchlist_id"] = watchlist_id
                if analysts:
                    latest["analysts"] = analysts
            save_run(latest)
            out = dict(latest)
            out["coalesced"] = True
            out["coalesce_reason"] = reason
            return out

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    run = {
        "id": run_id,
        "status": "queued",
        "ticker": ticker,
        "account": account,
        "watchlist_id": watchlist_id,
        "analysts": analysts,
        "date": analysis_date,
        "created_at": iso_now(),
        "viewer": viewer,
        "log": "",
        "error": None,
        "rating": None,
        "conclusion": None,
        "repeat_requests": 0,
    }
    save_run(run)

    def worker() -> None:
        run["status"] = "running"
        run["started_at"] = iso_now()
        save_run(run)
        cmd = [
            *analyze_cli_argv(),
            "--ticker",
            ticker,
            "--date",
            analysis_date,
            "--analysts",
            analysts,
            "--no-checkpoint",
            "--save",
            "--no-show",
            "--html",
        ]
        portfolio_path = None
        try:
            if watchlist_id:
                book = bridge_cli("watchlist", "--id", watchlist_id)
                # Watchlist is an alternative to Account: feed symbols as the book
                # via --portfolio (do not also pass --account — CLI lets account win).
                wl_meta = book.get("watchlist") or {}
                if not run.get("account") and wl_meta.get("account"):
                    run["account"] = str(wl_meta.get("account"))
                run["watchlist_label"] = wl_meta.get("label")
                portfolio_path = RUNS_DIR / f"{run_id}.portfolio.json"
                portfolio_path.write_text(
                    json.dumps(
                        {
                            "cash": book.get("cash", 0),
                            "currency": book.get("currency") or "USD",
                            "positions": book.get("positions") or [],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                cmd.extend(["--portfolio", str(portfolio_path)])
            elif account:
                cmd.extend(["--account", account])
            proc = subprocess.run(
                cmd,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                env=load_site_env(),
                timeout=int(os.environ.get("TRADINGAGENTS_ANALYZE_TIMEOUT", "1800")),
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
            run["error"] = str(exc)
        finally:
            if portfolio_path is not None:
                try:
                    portfolio_path.unlink(missing_ok=True)
                except OSError:
                    pass
        run["finished_at"] = iso_now()
        enrich_run_artifacts(run)
        save_run(run)
        if run.get("status") == "ok":
            queue_market_compare(run["id"])

    threading.Thread(target=worker, daemon=True).start()
    return run


def queue_market_compare(run_id: str, *, force: bool = False) -> dict | None:
    """Background multi-LLM street-vs-TradingAgents study for a finished run."""
    run = load_run(run_id)
    if not run:
        return None
    mc = run.get("market_compare") or {}
    if not force and mc.get("status") in ("running", "ok", "failed"):
        return run
    run["market_compare"] = {
        **mc,
        "status": "running",
        "started_at": iso_now(),
        "error": None,
    }
    save_run(run)

    def worker() -> None:
        current = load_run(run_id) or run
        try:
            # Ensure site .env keys (CURSOR/XAI/GEMINI/…) are visible to panelists.
            os.environ.update(
                {
                    k: v
                    for k, v in load_site_env().items()
                    if k.endswith("_API_KEY")
                    or k.endswith("_API_KEY2")
                    or k.endswith("_API_KEY3")
                    or k in ("CURSOR_API_KEY", "DEEPSEEK_API_URL", "GOOGLE_API_KEY")
                    or k.startswith("TRADINGAGENTS_MARKET_COMPARE_")
                }
            )
            run_market_compare(current)
        except Exception as exc:  # noqa: BLE001
            current["market_compare"] = {
                **(current.get("market_compare") or {}),
                "status": "failed",
                "error": str(exc)[:1500],
                "finished_at": iso_now(),
            }
        save_run(current)

    threading.Thread(target=worker, daemon=True, name=f"market-compare-{run_id}").start()
    return run


class Handler(BaseHTTPRequestHandler):
    server_version = "TradingAgentsSite/1.1"

    def log_message(self, fmt: str, *args) -> None:
        (LOG_DIR / "site.log").open("a", encoding="utf-8").write(
            f"{iso_now()} {self.address_string()} {fmt % args}\n"
        )

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, status: int = 302, extra_cookies: list[str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        for c in extra_cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()

    def _public_base(self) -> str:
        host = canonical_public_host(self.headers.get("Host"))
        return f"https://{host}"

    def _cookies(self) -> SimpleCookie:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:  # noqa: BLE001
            pass
        return cookie

    def _session_user(self) -> dict | None:
        if AUTH == "disabled":
            return {"email": "local-dev"}
        morsel = self._cookies().get(COOKIE)
        if not morsel:
            return None
        return sso_verify(urllib.parse.unquote(morsel.value))

    def _sticky_account(self) -> str | None:
        morsel = self._cookies().get(ACCOUNT_COOKIE)
        if not morsel:
            return None
        val = urllib.parse.unquote(morsel.value).strip()
        return val or None

    def _account_cookie(self, account: str) -> str:
        return (
            f"{ACCOUNT_COOKIE}={urllib.parse.quote(account)}; Path=/; "
            f"Max-Age={30 * 24 * 3600}; Secure; SameSite=Lax"
        )

    def _service_authorized(self) -> bool:
        expected = service_token()
        if not expected:
            return False
        provided = self.headers.get("X-TradingAgents-Service-Token") or self.headers.get(
            "X-TradingAgents-Bridge-Token"
        )
        if not provided:
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
        if not provided:
            return False
        try:
            return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
        except Exception:  # noqa: BLE001
            return False

    def _require_session(self, api: bool = False) -> dict | None:
        user = self._session_user()
        if user:
            return user
        if api and self._service_authorized():
            return {"email": "service-token", "service": True}
        base = self._public_base()
        path = self.path if self.path.startswith("/") else "/"
        login = f"{SSO_URL}?return_to={urllib.parse.quote(base + path, safe='')}"
        if api:
            self._json({"error": "login required", "login": login}, status=401)
        else:
            self._redirect(login)
        return None

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8") or "{}")

    def _render_run_page(self, run: dict) -> str:
        tpl = web_text("run.html")
        past = list_runs(ticker=str(run.get("ticker") or ""), limit=40)
        past = [r for r in past if r.get("id") != run.get("id")]
        payload = {
            "run": run,
            "past": past,
            "viewer": self._session_user() or {},
        }
        page = tpl.replace("__BOOTSTRAP__", json.dumps(payload))
        return page.replace("<!--__MARKET_COMPARE__-->", render_market_compare_section(run))

    def do_HEAD(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            return
        # Preserve deep links (e.g. /runs/<id>/report) the same as GET auth.
        base = self._public_base()
        ret = self.path if self.path.startswith("/") else "/"
        self.send_response(302)
        self.send_header(
            "Location",
            f"{SSO_URL}?return_to={urllib.parse.quote(base + ret, safe='')}",
        )
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/healthz":
            self._json({"ok": True, "service": "tradingagents-site", "at": iso_now()})
            return

        if path == "/sso/callback":
            token = (qs.get("token") or [""])[0]
            payload = sso_verify(token)
            if not payload:
                self._html("SSO token invalid or expired. Reload to sign in again.", status=403)
                return
            session = sso_sign(
                {
                    "email": payload.get("email", "user"),
                    "uid": payload.get("uid") or payload.get("user_id"),
                    "exp": int(time.time()) + SESSION_HOURS * 3600,
                }
            )
            self.send_response(302)
            self.send_header(
                "Set-Cookie",
                f"{COOKIE}={urllib.parse.quote(session)}; Path=/; Max-Age={SESSION_HOURS * 3600}; "
                "Secure; HttpOnly; SameSite=Lax",
            )
            ret = (qs.get("return_to") or ["/"])[0]
            if not ret.startswith("/") or ret.startswith("//"):
                ret = "/"
            self.send_header("Location", self._public_base() + ret)
            self.end_headers()
            return

        if path == "/logout":
            self.send_response(200)
            self.send_header(
                "Set-Cookie",
                f"{COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax",
            )
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed out.")
            return

        # Autolaunch: queue research immediately, land on the run page.
        if path == "/start":
            user = self._require_session(api=False)
            if not user:
                return
            ticker = (qs.get("ticker") or qs.get("symbol") or [""])[0].strip().upper()
            if not ticker:
                self._redirect("/")
                return
            account = (qs.get("account") or [""])[0].strip() or self._sticky_account()
            analysts = (qs.get("analysts") or ["market,news,fundamentals"])[0]
            run = queue_analyze(
                ticker=ticker,
                account=account,
                analysts=analysts,
                viewer=user.get("email"),
            )
            cookies = []
            if account:
                cookies.append(self._account_cookie(account))
            self._redirect(f"/runs/{run['id']}", extra_cookies=cookies)
            return

        if path == "/api/accounts":
            if not self._require_session(api=True):
                return
            try:
                accounts = bridge_cli("accounts")
                self._json(
                    {
                        "accounts": accounts,
                        "viewer": self._session_user(),
                        "sticky_account": self._sticky_account(),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, status=500)
            return

        if path == "/api/portfolio":
            if not self._require_session(api=True):
                return
            account = (qs.get("account") or [""])[0]
            watchlist_id = (qs.get("watchlist") or qs.get("watchlist_id") or [""])[0].strip()
            if watchlist_id:
                try:
                    book = bridge_cli("watchlist", "--id", watchlist_id)
                    self._json(
                        {
                            "watchlist_id": watchlist_id,
                            "watchlist": book.get("watchlist"),
                            "portfolio": book,
                            "source": "watchlist",
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, status=500)
                return
            if not account:
                self._json({"error": "account or watchlist required"}, status=422)
                return
            try:
                book = bridge_cli("portfolio", "--account", account, "--strict")
                self._json({"account": account, "portfolio": book})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, status=500)
            return

        if path == "/api/watchlists":
            if not self._require_session(api=True):
                return
            try:
                rows = bridge_cli("watchlists")
                self._json({"watchlists": rows})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, status=500)
            return

        if path == "/api/runs":
            if not self._require_session(api=True):
                return
            ticker = (qs.get("ticker") or [""])[0].strip().upper() or None
            self._json({"runs": list_runs(ticker=ticker)})
            return

        if path in ("/api/reports/latest", "/api/ticker/latest"):
            if not self._require_session(api=True):
                return
            ticker = (qs.get("ticker") or qs.get("symbol") or [""])[0].strip().upper()
            if not ticker:
                self._json({"error": "ticker required"}, status=422)
                return
            latest = latest_report_for_ticker(ticker)
            self._json(
                {
                    "ok": True,
                    "ticker": ticker,
                    "report": latest,
                    "public_base": PUBLIC_URL,
                }
            )
            return

        m = re.fullmatch(r"/api/runs/([^/]+)/market-compare", path)
        if m:
            if not self._require_session(api=True):
                return
            run = load_run(m.group(1))
            if not run:
                self._json({"error": "not found"}, status=404)
                return
            self._json({"ok": True, "market_compare": run.get("market_compare"), "run_id": run.get("id")})
            return

        if path.startswith("/api/runs/"):
            if not self._require_session(api=True):
                return
            run_id = path.rsplit("/", 1)[-1]
            run = load_run(run_id)
            if not run:
                self._json({"error": "not found"}, status=404)
                return
            # Kick compare for legacy completed runs that never got one.
            mc = run.get("market_compare") or {}
            if run.get("status") == "ok" and not mc.get("status"):
                queue_market_compare(run_id)
                run = load_run(run_id) or run
            self._json(run)
            return

        m = re.fullmatch(r"/runs/([^/]+)/report", path)
        if m:
            if not self._require_session(api=False):
                return
            run = load_run(m.group(1))
            if not run or not run.get("report_html"):
                self._json({"error": "report not found"}, status=404)
                return
            html_path = Path(run["report_html"])
            if not html_path.is_file():
                self._json({"error": "report missing on disk"}, status=404)
                return
            # Autostart compare for older reports so the injected section can fill in.
            mc = run.get("market_compare") or {}
            if run.get("status") == "ok" and not mc.get("status"):
                queue_market_compare(run["id"])
                run = load_run(run["id"]) or run
            body = html_path.read_text(encoding="utf-8", errors="replace")
            run_id = m.group(1)
            ticker = html.escape(str(run.get("ticker") or ""))
            rating = html.escape(str(run.get("rating") or run.get("status") or ""))
            banner = (
                '<div id="ta-report-nav" style="position:sticky;top:0;z-index:9999;'
                "font:600 13px/1.4 system-ui,sans-serif;padding:0.55rem 1rem;"
                "background:#0f1419;color:#e7eef7;border-bottom:1px solid #2a3542;"
                'display:flex;gap:1rem;flex-wrap:wrap;align-items:center">'
                f'<a href="/runs/{html.escape(run_id)}" style="color:#3d9cf0;text-decoration:none">'
                "← Run details</a>"
                f'<span style="color:#8b9bb0">{ticker} · {rating} · full research report</span>'
                '<a href="/" style="color:#8b9bb0;margin-left:auto;text-decoration:none">TradingAgents</a>'
                "</div>"
            )
            compare_block = render_report_compare_banner(run)
            inject = banner + compare_block
            if "<body" in body.lower():
                body = re.sub(
                    r"(<body[^>]*>)",
                    r"\1" + inject,
                    body,
                    count=1,
                    flags=re.IGNORECASE,
                )
            else:
                body = inject + body
            self._html(body)
            return

        m = re.fullmatch(r"/runs/([^/]+)/?", path)
        if m:
            if not self._require_session(api=False):
                return
            run = load_run(m.group(1))
            if not run:
                self._html("Run not found.", status=404)
                return
            mc = run.get("market_compare") or {}
            if run.get("status") == "ok" and not mc.get("status"):
                queue_market_compare(run["id"])
                run = load_run(run["id"]) or run
            self._html(self._render_run_page(run))
            return

        if path in ("/", "/index.html"):
            if not self._require_session(api=False):
                return
            # Legacy deep-link: ?ticker=&analyze=1 → /start (no waiting for form).
            ticker = (qs.get("ticker") or qs.get("symbol") or [""])[0].strip()
            analyze = (qs.get("analyze") or qs.get("run") or [""])[0] in ("1", "true", "yes")
            if ticker and analyze:
                q = urllib.parse.urlencode(
                    {
                        k: (qs.get(k) or [""])[0]
                        for k in ("ticker", "symbol", "account", "analysts")
                        if (qs.get(k) or [""])[0]
                    }
                )
                self._redirect(f"/start?{q}")
                return
            self._html(web_text("index.html"))
            return

        if path in ("/settings", "/settings/", "/settings/sources"):
            if not self._require_session(api=False):
                return
            self._html(web_text("settings.html"))
            return

        m = re.fullmatch(r"/settings/sources/([^/]+)/reports/?", path)
        if m:
            if not self._require_session(api=False):
                return
            # Dedicated full-page popup list of compares that used this source.
            self._html(web_text("settings_source_reports.html"))
            return

        if path == "/api/settings/sources":
            if not self._require_session(api=True):
                return
            rows = compare_sources.list_sources(include_disabled=True)
            enriched = []
            for row in rows:
                item = dict(row)
                item["frequency"] = compare_sources.frequency_summary(row.get("stats") or {})
                item["reports_url"] = f"/settings/sources/{row.get('id')}/reports"
                enriched.append(item)
            store = compare_sources.load_store()
            self._json(
                {
                    "ok": True,
                    "sources": enriched,
                    "backfilled_at": store.get("backfilled_at"),
                    "updated_at": store.get("updated_at"),
                    "count": len(enriched),
                }
            )
            return

        m = re.fullmatch(r"/api/settings/sources/([^/]+)/reports", path)
        if m:
            if not self._require_session(api=True):
                return
            try:
                payload = compare_sources.source_reports(m.group(1))
            except KeyError as exc:
                self._json({"error": str(exc)}, status=404)
                return
            self._json({"ok": True, **payload})
            return

        m = re.fullmatch(r"/api/settings/sources/([^/]+)/?", path)
        if m:
            if not self._require_session(api=True):
                return
            src = compare_sources.get_source(m.group(1))
            if not src:
                self._json({"error": "source not found"}, status=404)
                return
            src = dict(src)
            src["frequency"] = compare_sources.frequency_summary(src.get("stats") or {})
            src["reports_url"] = f"/settings/sources/{src.get('id')}/reports"
            self._json({"ok": True, "source": src})
            return

        self._json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        m = re.fullmatch(r"/api/runs/([^/]+)/market-compare", path)
        if m:
            user = self._require_session(api=True)
            if not user:
                return
            run_id = m.group(1)
            run = load_run(run_id)
            if not run:
                self._json({"error": "not found"}, status=404)
                return
            try:
                body = self._read_json()
            except json.JSONDecodeError:
                body = {}
            force = bool(body.get("force") if isinstance(body, dict) else False)
            queued = queue_market_compare(run_id, force=force or True)
            self._json(
                {
                    "ok": True,
                    "queued": True,
                    "run_id": run_id,
                    "market_compare": (queued or run).get("market_compare"),
                }
            )
            return

        if path == "/api/settings/sources/backfill":
            user = self._require_session(api=True)
            if not user:
                return
            try:
                body = self._read_json()
            except json.JSONDecodeError:
                body = {}
            limit = body.get("limit")
            try:
                limit_i = int(limit) if limit is not None else None
            except (TypeError, ValueError):
                limit_i = None
            reset = bool(body.get("reset_stats", True))
            result = compare_sources.backfill_from_runs(limit=limit_i, reset_stats=reset)
            self._json(result)
            return

        m = re.fullmatch(r"/api/settings/sources/([^/]+)/?", path)
        if m:
            user = self._require_session(api=True)
            if not user:
                return
            try:
                body = self._read_json()
            except json.JSONDecodeError as exc:
                self._json({"error": f"invalid JSON: {exc}"}, status=422)
                return
            try:
                updated = compare_sources.update_source(m.group(1), body if isinstance(body, dict) else {})
            except KeyError as exc:
                self._json({"error": str(exc)}, status=404)
                return
            updated = dict(updated)
            updated["frequency"] = compare_sources.frequency_summary(updated.get("stats") or {})
            self._json({"ok": True, "source": updated})
            return

        if path not in ("/api/analyze", "/api/reports/refresh", "/api/ticker/refresh"):
            self._json({"error": "not found"}, status=404)
            return
        user = self._require_session(api=True)
        if not user:
            return
        try:
            body = self._read_json()
        except json.JSONDecodeError as exc:
            self._json({"error": f"invalid JSON: {exc}"}, status=422)
            return

        qs = urllib.parse.parse_qs(parsed.query)
        ticker = str(body.get("ticker") or body.get("symbol") or (qs.get("ticker") or [""])[0]).strip().upper()
        watchlist_id = str(body.get("watchlist_id") or body.get("watchlist") or "").strip() or None
        account = str(body.get("account") or "").strip() or (
            None if watchlist_id else self._sticky_account()
        )
        analysts = str(body.get("analysts") or "market,news,fundamentals").strip()
        if not ticker:
            self._json({"error": "ticker required"}, status=422)
            return
        if not account and not watchlist_id:
            self._json({"error": "account or watchlist required"}, status=422)
            return
        date = str(body.get("date") or "").strip() or None
        # Background refresh from quote pages / cadence cron: do not require a human viewer.
        viewer = user.get("email") if not user.get("service") else str(body.get("viewer") or "cadence")
        # Service token may pass force=true to bypass the daily per-ticker cap.
        force = bool(body.get("force")) and bool(user.get("service"))
        run = queue_analyze(
            ticker=ticker,
            account=account,
            analysts=analysts,
            viewer=viewer,
            analysis_date=date,
            watchlist_id=watchlist_id,
            force=force,
        )
        coalesced = bool(run.get("coalesced"))
        payload = {
            "ok": True,
            "queued": (not coalesced) and str(run.get("status") or "").lower() == "queued",
            "coalesced": coalesced,
            "coalesce_reason": run.get("coalesce_reason"),
            "run": summarize_run(run),
            "redirect": f"/runs/{run['id']}",
            "public_url": f"{PUBLIC_URL}/runs/{run['id']}",
        }
        if account and path == "/api/analyze" and not user.get("service"):
            # Mirror sticky account for subsequent browser visits.
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", self._account_cookie(account))
            body_bytes = json.dumps(payload).encode("utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return
        self._json(payload)


def main() -> None:
    sso_secret()
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"TradingAgents site http://{BIND}:{PORT}/ → {PUBLIC_URL} auth={AUTH}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
