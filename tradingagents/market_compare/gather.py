"""Gather prevailing market-analyst snippets for a ticker.

Live fetches often hit paywalls / bot blocks. Policy (ROBIN-1894):

* Prefer a **recent on-disk cache** (default 7 days) when present.
* On live failure / paywall / empty body: **skip** that source for this run
  and leave the previous cache untouched so the **next** monthly/backfill
  pass can retry.
* Focus research on **analyst ratings + explanations** of recent company /
  industry / macro events (not just price quotes).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .sources import ANALYST_FOCUS_NAMES, source_catalog

_UA = (
    "Mozilla/5.0 (compatible; TradingAgentsMarketCompare/1.0; "
    "+https://tradingagents.quiltyapp.com)"
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_PAYWALL_HINTS = (
    "subscribe to continue",
    "create a free account",
    "sign in to read",
    "sign up to continue",
    "become a member",
    "premium article",
    "paywall",
    "cookies disabled",
    "enable javascript",
    "captcha",
    "cf-browser-verification",
    "access denied",
    "attention required",
)


def _cache_dir() -> Path:
    root = Path(
        os.environ.get(
            "TRADINGAGENTS_MARKET_COMPARE_CACHE",
            Path.home() / ".tradingagents" / "market_compare_cache",
        )
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cache_ttl_sec() -> float:
    return float(os.environ.get("TRADINGAGENTS_MARKET_COMPARE_CACHE_DAYS", "7")) * 86400.0


def _cache_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def _cache_path(url: str) -> Path:
    return _cache_dir() / f"{_cache_key(url)}.json"


def read_cached_snippet(url: str, *, max_age_sec: float | None = None) -> dict[str, Any] | None:
    path = _cache_path(url)
    if not path.is_file():
        return None
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(row, dict) or not row.get("ok"):
        return None
    age = time.time() - float(row.get("cached_at") or 0)
    ttl = _cache_ttl_sec() if max_age_sec is None else max_age_sec
    if age > ttl:
        return None
    row = dict(row)
    row["from_cache"] = True
    row["cache_age_hours"] = round(age / 3600.0, 2)
    return row


def write_cached_snippet(row: dict[str, Any]) -> None:
    if not row.get("ok") or not row.get("url"):
        return
    payload = {
        **row,
        "cached_at": time.time(),
        "from_cache": False,
    }
    try:
        _cache_path(str(row["url"])).write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def _http_get(url: str, *, timeout: float = 12.0) -> tuple[int, str]:
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept": "text/html,application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(250_000)
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read(80_000).decode("utf-8", errors="replace") if exc.fp else ""
        return int(exc.code), body
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def _strip_html(text: str, *, limit: int = 2500) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit]


def _looks_paywalled(status: int, body: str, snippet: str) -> bool:
    if status in (401, 402, 403, 407, 429, 451):
        return True
    if status >= 400:
        return True
    low = f"{body[:2000]} {snippet}".lower()
    if any(h in low for h in _PAYWALL_HINTS):
        # Short soft walls often still leave usable public teaser text;
        # treat as paywall only when the usable snippet is thin.
        if len(snippet) < 400:
            return True
    return False


def _env_key(*names: str) -> str | None:
    for name in names:
        val = (os.environ.get(name) or "").strip().strip('"').strip("'")
        if val:
            return val
    return None


def fetch_page_snippets(ticker: str, *, max_pages: int = 22) -> list[dict[str, Any]]:
    """Best-effort public page snippets with cache + skip-on-paywall."""
    catalog = source_catalog(ticker)
    # Prefer ratings / analyst narrative, then technicals, then desks.
    priority = (
        "analyst",
        "technical",
        "broker",
        "fundamentals",
        "earnings",
        "screener",
        "alt_data",
        "media",
    )
    ordered = sorted(
        catalog,
        key=lambda r: priority.index(r["group"]) if r["group"] in priority else 99,
    )
    out: list[dict[str, Any]] = []

    def one(row: dict) -> dict:
        url = row["url"]
        cached = read_cached_snippet(url)
        status, body = _http_get(url)
        snippet = _strip_html(body) if status and status < 500 else ""
        paywalled = _looks_paywalled(status, body or "", snippet)
        hits = []
        low = (snippet or body or "").lower()
        for kw in (
            "cortex",
            "buy",
            "sell",
            "hold",
            "overweight",
            "underweight",
            "outperform",
            "underperform",
            "strong buy",
            "analyst",
            "consensus",
            "price target",
            "rating",
        ):
            if kw in low:
                hits.append(kw)

        live_ok = bool(snippet) and status == 200 and not paywalled
        if live_ok:
            result = {
                "name": row["name"],
                "url": url,
                "group": row["group"],
                "http_status": status,
                "snippet": snippet,
                "keywords": hits[:12],
                "ok": True,
                "from_cache": False,
                "skipped": False,
            }
            write_cached_snippet(result)
            return result

        # Live miss / paywall → reuse recent cache if any; else skip (retry next run).
        if cached:
            cached = dict(cached)
            cached["name"] = row["name"]
            cached["group"] = row["group"]
            cached["skipped_live"] = True
            cached["live_http_status"] = status
            cached["live_paywalled"] = paywalled
            return cached

        return {
            "name": row["name"],
            "url": url,
            "group": row["group"],
            "http_status": status,
            "snippet": "",
            "keywords": [],
            "ok": False,
            "skipped": True,
            "skip_reason": "paywall" if paywalled else (f"http_{status}" if status else "fetch_error"),
            "from_cache": False,
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(one, row) for row in ordered[:max_pages]]
        for fut in as_completed(futs):
            try:
                out.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                out.append({"name": "error", "ok": False, "skipped": True, "error": str(exc)})
    out.sort(key=lambda r: (0 if r.get("ok") else 1, 0 if r.get("from_cache") else 1, r.get("name") or ""))
    return out


def perplexity_research(ticker: str, sources: list[dict]) -> dict[str, Any]:
    """Web-grounded digest via Perplexity Sonar (preferred gatherer)."""
    key = _env_key("PERPLEXITY_API_KEY", "PERPLEXITY_API_KEY2")
    if not key:
        return {"ok": False, "error": "PERPLEXITY_API_KEY missing"}

    # Put analyst-focused URLs first; still pass a wide set for Sonar grounding.
    focus = [s for s in sources if any(n.lower() in s["name"].lower() for n in ANALYST_FOCUS_NAMES)]
    rest = [s for s in sources if s not in focus]
    ordered = focus + rest
    url_lines = "\n".join(f"- {s['name']}: {s['url']}" for s in ordered[:28])
    focus_list = ", ".join(ANALYST_FOCUS_NAMES)

    prompt = f"""You are researching the CURRENT prevailing street / media analyst stance on ticker {ticker}.

Priority: find **analyst ratings and written explanations** that respond to recent company events
and the surrounding industry / sector / macro / business environment — not just a price quote.

Use the web. Prefer these URLs when available (Robinhood Cortex Digests + any sources they cite
on the quote page count too). Paywalled pages: use whatever public teaser / consensus surface
is visible, or skip that outlet rather than inventing content.
{url_lines}

Also cover these research tools when they publish ratings or narrative for {ticker}:
{focus_list}.

Return STRICT JSON with keys:
{{
  "ticker": "{ticker}",
  "consensus_rating": "Buy|Overweight|Hold|Underweight|Sell|Mixed|Unknown",
  "consensus_summary": "2-4 sentences emphasizing WHY analysts moved (events / sector / macro)",
  "robinhood_cortex": {{"found": true/false, "summary": "...", "cited_sources": ["..."]}},
  "sources": [
    {{"name": "...", "stance": "Bullish|Neutral|Bearish|Unknown", "rating_or_signal": "...", "why": "recent-event explanation", "url": "..."}}
  ],
  "divergences_inside_street": "short note",
  "skipped_or_paywalled": ["outlet names skipped this pass"]
}}
No markdown fences. JSON only."""

    payload = {
        "model": os.environ.get("TRADINGAGENTS_MARKET_COMPARE_PERPLEXITY_MODEL", "sonar"),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You research equity/ETF analyst consensus and explanations. "
                    "Skip paywalled sources rather than guessing. Reply with JSON only."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.perplexity.ai/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = json.load(resp)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    content = (
        ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    ).strip()
    citations = body.get("citations") or []
    parsed = _parse_json_object(content)
    return {
        "ok": True,
        "raw": content[:20000],
        "parsed": parsed,
        "citations": citations[:30],
        "model": payload["model"],
    }


def _parse_json_object(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None


def gather_market_views(ticker: str) -> dict[str, Any]:
    ticker = (ticker or "").strip().upper()
    sources = source_catalog(ticker)
    pages = fetch_page_snippets(ticker)
    research = perplexity_research(ticker, sources)
    skipped = [p for p in pages if p.get("skipped")]
    cached = [p for p in pages if p.get("from_cache")]
    return {
        "ticker": ticker,
        "sources": sources,
        "page_snippets": pages,
        "research": research,
        "gather_stats": {
            "sources_catalog": len(sources),
            "pages_ok": sum(1 for p in pages if p.get("ok")),
            "pages_from_cache": len(cached),
            "pages_skipped": len(skipped),
            "skip_reasons": sorted(
                {str(p.get("skip_reason") or "unknown") for p in skipped}
            ),
        },
    }
