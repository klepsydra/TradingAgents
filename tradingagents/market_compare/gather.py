"""Gather prevailing market-analyst snippets for a ticker."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .sources import source_catalog

_UA = (
    "Mozilla/5.0 (compatible; TradingAgentsMarketCompare/1.0; "
    "+https://tradingagents.quiltyapp.com)"
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _http_get(url: str, *, timeout: float = 12.0) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html,application/json"})
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


def _env_key(*names: str) -> str | None:
    for name in names:
        val = (os.environ.get(name) or "").strip().strip('"').strip("'")
        if val:
            return val
    return None


def fetch_page_snippets(ticker: str, *, max_pages: int = 8) -> list[dict[str, Any]]:
    """Best-effort public page snippets (many sites block bots — failures are OK)."""
    catalog = source_catalog(ticker)
    # Prefer opinion/ratings/technical/RH first
    priority = ("technical", "analyst", "broker", "fundamentals", "media")
    ordered = sorted(
        catalog,
        key=lambda r: priority.index(r["group"]) if r["group"] in priority else 99,
    )
    out: list[dict[str, Any]] = []

    def one(row: dict) -> dict:
        status, body = _http_get(row["url"])
        snippet = _strip_html(body) if status and status < 400 else ""
        # Light keyword extract for Cortex / ratings language
        hits = []
        low = (snippet or body or "").lower()
        for kw in (
            "cortex", "buy", "sell", "hold", "overweight", "underweight",
            "outperform", "underperform", "strong buy", "analyst", "consensus",
        ):
            if kw in low:
                hits.append(kw)
        return {
            "name": row["name"],
            "url": row["url"],
            "group": row["group"],
            "http_status": status,
            "snippet": snippet,
            "keywords": hits[:12],
            "ok": bool(snippet) and status == 200,
        }

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(one, row) for row in ordered[:max_pages]]
        for fut in as_completed(futs):
            try:
                out.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                out.append({"name": "error", "ok": False, "error": str(exc)})
    out.sort(key=lambda r: (0 if r.get("ok") else 1, r.get("name") or ""))
    return out


def perplexity_research(ticker: str, sources: list[dict]) -> dict[str, Any]:
    """Web-grounded digest via Perplexity Sonar (preferred gatherer)."""
    key = _env_key("PERPLEXITY_API_KEY", "PERPLEXITY_API_KEY2")
    if not key:
        return {"ok": False, "error": "PERPLEXITY_API_KEY missing"}

    url_lines = "\n".join(f"- {s['name']}: {s['url']}" for s in sources[:18])
    prompt = f"""You are researching the CURRENT prevailing street / media analyst stance on ticker {ticker}.

Use the web. Prefer these sources when available (and Robinhood Cortex Digests + any sources they cite on the quote page):
{url_lines}

Also cover: Benzinga, TipRanks, Seeking Alpha, Morningstar, Motley Fool, Investopedia, MarketWatch, CNBC, Bloomberg, TheStreet, MarketBeat, Barchart opinion/technicals, PortfoliosLab, StockAnalysis.

Return STRICT JSON with keys:
{{
  "ticker": "{ticker}",
  "consensus_rating": "Buy|Overweight|Hold|Underweight|Sell|Mixed|Unknown",
  "consensus_summary": "2-4 sentences",
  "robinhood_cortex": {{"found": true/false, "summary": "...", "cited_sources": ["..."]}},
  "sources": [
    {{"name": "...", "stance": "Bullish|Neutral|Bearish|Unknown", "rating_or_signal": "...", "why": "...", "url": "..."}}
  ],
  "divergences_inside_street": "short note"
}}
No markdown fences. JSON only."""

    payload = {
        "model": os.environ.get("TRADINGAGENTS_MARKET_COMPARE_PERPLEXITY_MODEL", "sonar"),
        "messages": [
            {"role": "system", "content": "You research equity/ETF analyst consensus. Reply with JSON only."},
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
    return {
        "ticker": ticker,
        "sources": sources,
        "page_snippets": pages,
        "research": research,
    }
