"""Multi-provider LLM panel that compares TradingAgents vs street consensus."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable


def _env(*names: str) -> str | None:
    for name in names:
        val = (os.environ.get(name) or "").strip().strip('"').strip("'")
        if val:
            return val
    return None


def _parse_json(text: str) -> dict | None:
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


def _compare_prompt(*, ticker: str, ta_decision: str, market_blob: str) -> str:
    return f"""You are an independent market-research analyst. Compare TradingAgents' decision on {ticker}
against the prevailing street / media analyst picture gathered below.

## TradingAgents decision (source of truth for "our" call)
{ta_decision[:12000]}

## Prevailing market / media analyst research
{market_blob[:16000]}

Return STRICT JSON:
{{
  "panelist": "<your provider/model id>",
  "street_consensus": "Buy|Overweight|Hold|Underweight|Sell|Mixed|Unknown",
  "tradingagents_rating": "Buy|Overweight|Hold|Underweight|Sell|Unknown",
  "alignment": "Agree|Mostly agree|Mixed|Disagree|Unknown",
  "coincide": [
    {{"point": "...", "why": "..."}}
  ],
  "differ": [
    {{"point": "...", "why": "..."}}
  ],
  "per_source": [
    {{"source": "TipRanks|Seeking Alpha|Morningstar|Barchart|Robinhood Cortex|...", "their_stance": "...", "vs_tradingagents": "coincide|differ|unclear", "why": "..."}}
  ],
  "executive_take": "4-8 sentences a portfolio manager would read"
}}
JSON only. No markdown fences."""


def _openai_compatible_chat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: float = 120.0,
) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You compare equity research views. Reply with JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.load(resp)
    return ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def _gemini_chat(*, api_key: str, model: str, prompt: str, timeout: float = 120.0) -> str:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.load(resp)
    parts = (((body.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _anthropic_chat(*, api_key: str, model: str, prompt: str, timeout: float = 120.0) -> str:
    payload = {
        "model": model,
        "max_tokens": 2500,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": prompt}],
        "system": "You compare equity research views. Reply with JSON only.",
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.load(resp)
    blocks = body.get("content") or []
    return "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")


def _cursor_agent_chat(*, api_key: str, prompt: str, timeout: float = 180.0) -> str:
    """Use Cursor Agent CLI in ask/print mode when CURSOR_API_KEY is present."""
    bin_path = shutil.which("cursor-agent") or shutil.which("agent")
    if not bin_path:
        # shimazu installs under ~/.local/bin
        for cand in (
            os.path.expanduser("~/.local/bin/cursor-agent"),
            os.path.expanduser("~/.local/bin/agent"),
            "/home/marckoby/.local/bin/cursor-agent",
            "/home/marckoby/.local/bin/agent",
        ):
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                bin_path = cand
                break
    if not bin_path:
        raise RuntimeError("cursor-agent binary not found")

    env = os.environ.copy()
    env["CURSOR_API_KEY"] = api_key
    cmd = [
        bin_path,
        "--print",
        "--output-format",
        "text",
        "--mode",
        "ask",
        "--api-key",
        api_key,
        prompt,
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        raise RuntimeError((proc.stderr or proc.stdout or f"exit {proc.returncode}")[-1500:])
    return (proc.stdout or "").strip() or (proc.stderr or "").strip()


def _wrap(provider: str, model: str, fn: Callable[[], str]) -> dict[str, Any]:
    try:
        raw = fn()
        parsed = _parse_json(raw)
        return {
            "provider": provider,
            "model": model,
            "ok": True,
            "raw": raw[:20000],
            "parsed": parsed,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "provider": provider,
            "model": model,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:1500],
        }


def run_panel(*, ticker: str, ta_decision: str, market_gather: dict) -> list[dict[str, Any]]:
    """Fan out to available providers: Cursor, Gemini, Grok, OpenAI, Anthropic, …"""
    research = (market_gather.get("research") or {})
    parsed = research.get("parsed") or {}
    snippets = market_gather.get("page_snippets") or []
    snippet_txt = "\n".join(
        f"- {s.get('name')}: {(s.get('snippet') or '')[:400]}"
        for s in snippets
        if s.get("ok")
    )
    market_blob = json.dumps(
        {
            "perplexity": parsed or research.get("raw"),
            "citations": research.get("citations"),
            "page_snippets": snippet_txt,
            "source_urls": [s.get("url") for s in (market_gather.get("sources") or [])[:20]],
        },
        indent=2,
    )
    prompt = _compare_prompt(ticker=ticker, ta_decision=ta_decision, market_blob=market_blob)

    jobs: list[tuple[str, str, Callable[[], str]]] = []

    cursor_key = _env("CURSOR_API_KEY")
    if cursor_key:
        jobs.append(("cursor", "cursor-agent", lambda: _cursor_agent_chat(api_key=cursor_key, prompt=prompt)))

    gemini_key = _env("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY3")
    if gemini_key:
        gmodel = os.environ.get("TRADINGAGENTS_MARKET_COMPARE_GEMINI_MODEL", "gemini-2.0-flash")
        jobs.append(("gemini", gmodel, lambda k=gemini_key, m=gmodel: _gemini_chat(api_key=k, model=m, prompt=prompt)))

    xai_key = _env("XAI_API_KEY", "XAI_API_KEY2")
    if xai_key:
        xmodel = os.environ.get("TRADINGAGENTS_MARKET_COMPARE_GROK_MODEL", "grok-4-fast-non-reasoning")
        jobs.append(
            (
                "xai",
                xmodel,
                lambda k=xai_key, m=xmodel: _openai_compatible_chat(
                    base_url="https://api.x.ai/v1", api_key=k, model=m, prompt=prompt
                ),
            )
        )

    openai_key = _env("OPENAI_API_KEY", "OPENAI_API_KEY2", "OPENAI_API_KEY_AGENTROBIN")
    if openai_key:
        omodel = os.environ.get("TRADINGAGENTS_MARKET_COMPARE_OPENAI_MODEL", "gpt-4.1-mini")
        jobs.append(
            (
                "openai",
                omodel,
                lambda k=openai_key, m=omodel: _openai_compatible_chat(
                    base_url="https://api.openai.com/v1", api_key=k, model=m, prompt=prompt
                ),
            )
        )

    anth_key = _env("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY2", "ANTHROPIC_API_KEY3")
    if anth_key:
        amodel = os.environ.get("TRADINGAGENTS_MARKET_COMPARE_ANTHROPIC_MODEL", "claude-sonnet-4-5")
        jobs.append(
            ("anthropic", amodel, lambda k=anth_key, m=amodel: _anthropic_chat(api_key=k, model=m, prompt=prompt))
        )

    # Optional extras when present
    for provider, env_names, base, default_model in (
        ("mistral", ("MISTRAL_API_KEY",), "https://api.mistral.ai/v1", "mistral-small-latest"),
        ("groq", ("GROQ_API_KEY",), "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
        ("deepseek", ("DEEPSEEK_API_KEY",), "https://api.deepseek.com", "deepseek-chat"),
    ):
        key = _env(*env_names)
        if not key:
            continue
        model = os.environ.get(f"TRADINGAGENTS_MARKET_COMPARE_{provider.upper()}_MODEL", default_model)
        jobs.append(
            (
                provider,
                model,
                lambda k=key, m=model, b=base: _openai_compatible_chat(
                    base_url=b, api_key=k, model=m, prompt=prompt
                ),
            )
        )

    results: list[dict[str, Any]] = []
    if not jobs:
        return [{"provider": "none", "ok": False, "error": "no LLM API keys available"}]

    with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as pool:
        futs = {pool.submit(_wrap, p, m, fn): (p, m) for p, m, fn in jobs}
        for fut in as_completed(futs):
            results.append(fut.result())
    results.sort(key=lambda r: r.get("provider") or "")
    return results
