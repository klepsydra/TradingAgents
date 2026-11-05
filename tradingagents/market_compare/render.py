"""HTML fragments for market-compare sections on run + report pages."""

from __future__ import annotations

import html
from typing import Any


def _esc(s: Any) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def render_market_compare_section(run: dict) -> str:
    mc = run.get("market_compare") or {}
    status = mc.get("status") or "absent"
    ticker = _esc(mc.get("ticker") or run.get("ticker") or "")

    if status in ("absent", None, ""):
        return (
            '<section id="market-compare" class="market-compare">'
            "<h2>Market analysts vs TradingAgents</h2>"
            '<p class="meta">Comparison not started yet. '
            "It usually runs automatically after research finishes, or use "
            '<button type="button" id="mcRefreshBtn">Run comparison</button>.</p>'
            '<div id="marketCompareBody"></div></section>'
        )

    if status == "running":
        return (
            '<section id="market-compare" class="market-compare">'
            "<h2>Market analysts vs TradingAgents</h2>"
            f'<p class="meta">Studying prevailing analysts for <strong>{ticker}</strong>… '
            "this uses Perplexity + a multi-LLM panel (Cursor / Gemini / Grok / …).</p>"
            '<div id="marketCompareBody"><pre>Running…</pre></div>'
            '<p class="meta"><button type="button" id="mcRefreshBtn">Refresh status</button></p>'
            "</section>"
        )

    if status == "failed" and not mc.get("summary"):
        err = _esc(mc.get("error") or "failed")
        return (
            '<section id="market-compare" class="market-compare">'
            "<h2>Market analysts vs TradingAgents</h2>"
            f'<p class="err">Comparison failed: {err}</p>'
            '<p class="meta"><button type="button" id="mcRefreshBtn">Retry comparison</button></p>'
            '<div id="marketCompareBody"></div></section>'
        )

    summary = mc.get("summary") or {}
    gather = mc.get("gather") or {}
    parsed = gather.get("parsed") or {}
    cortex = (parsed.get("robinhood_cortex") if isinstance(parsed, dict) else None) or {}

    coincide_rows = "".join(
        f"<li><strong>{_esc(i.get('point'))}</strong> — {_esc(i.get('why'))} "
        f"<span class='meta'>({_esc(i.get('via') or '')})</span></li>"
        for i in (summary.get("coincide") or [])[:8]
    ) or "<li class='meta'>None called out</li>"
    differ_rows = "".join(
        f"<li><strong>{_esc(i.get('point'))}</strong> — {_esc(i.get('why'))} "
        f"<span class='meta'>({_esc(i.get('via') or '')})</span></li>"
        for i in (summary.get("differ") or [])[:8]
    ) or "<li class='meta'>None called out</li>"

    per_source = "".join(
        "<tr>"
        f"<td>{_esc(i.get('source'))}</td>"
        f"<td>{_esc(i.get('their_stance'))}</td>"
        f"<td>{_esc(i.get('vs_tradingagents'))}</td>"
        f"<td class='meta'>{_esc(i.get('why'))}</td>"
        "</tr>"
        for i in (summary.get("per_source") or [])[:16]
    ) or "<tr><td colspan=4 class='meta'>No per-source rows yet</td></tr>"

    takes = "".join(
        f"<div class='prose' style='margin-bottom:0.6rem'>{_esc(t)}</div>"
        for t in (summary.get("panel_takes") or [])[:6]
    ) or "<p class='meta'>No panel takes</p>"

    urls = "".join(
        f"<li><a href='{_esc(s.get('url'))}' target='_blank' rel='noopener'>"
        f"{_esc(s.get('name'))}</a></li>"
        for s in (mc.get("source_urls") or [])[:16]
    )

    panel_meta = ", ".join(
        f"{p.get('provider')}{'✓' if p.get('ok') else '✗'}"
        for p in (mc.get("panel") or [])
    ) or "—"

    research_summary = _esc(summary.get("research_summary") or parsed.get("consensus_summary") or "")
    cortex_html = ""
    if isinstance(cortex, dict) and (cortex.get("found") or cortex.get("summary")):
        cites = ", ".join(_esc(c) for c in (cortex.get("cited_sources") or [])[:8])
        cortex_html = (
            "<h3 style='margin:1rem 0 0.4rem;font-size:0.85rem;color:var(--muted);"
            "text-transform:uppercase;letter-spacing:0.06em'>Robinhood Cortex</h3>"
            f"<p>{_esc(cortex.get('summary') or 'Mentioned in research')}</p>"
            f"<p class='meta'>Cited sources: {cites or '—'}</p>"
        )

    failed = summary.get("panelists_failed") or []
    fail_note = ""
    if failed:
        fail_note = (
            "<p class='meta'>Panelists skipped/failed: "
            + _esc(", ".join(f"{f.get('provider')}: {f.get('error')}" for f in failed)[:500])
            + "</p>"
        )

    return f"""
<section id="market-compare" class="market-compare">
  <h2>Market analysts vs TradingAgents</h2>
  <p class="meta">
    Street consensus: <strong>{_esc(summary.get('street_consensus'))}</strong>
    · TradingAgents: <strong>{_esc(summary.get('tradingagents_rating') or run.get('rating') or '—')}</strong>
    · Alignment: <strong>{_esc(summary.get('alignment'))}</strong>
    · Panel: {_esc(panel_meta)}
  </p>
  <p>{research_summary or '—'}</p>
  {cortex_html}
  <div class="grid" style="margin-top:0.8rem">
    <div>
      <h3 style="margin:0 0 0.4rem;font-size:0.85rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em">Where they coincide</h3>
      <ul>{coincide_rows}</ul>
    </div>
    <div>
      <h3 style="margin:0 0 0.4rem;font-size:0.85rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em">Where they differ</h3>
      <ul>{differ_rows}</ul>
    </div>
  </div>
  <h3 style="margin:1rem 0 0.4rem;font-size:0.85rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em">Per-source vs TradingAgents</h3>
  <table>
    <thead><tr><th>Source</th><th>Stance</th><th>vs TA</th><th>Why</th></tr></thead>
    <tbody>{per_source}</tbody>
  </table>
  <h3 style="margin:1rem 0 0.4rem;font-size:0.85rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em">Panel takes</h3>
  {takes}
  {fail_note}
  <details style="margin-top:0.8rem">
    <summary class="meta" style="cursor:pointer">Source URL checklist</summary>
    <ul>{urls}</ul>
  </details>
  <p class="meta" style="margin-top:0.75rem">
    <button type="button" id="mcRefreshBtn">Re-run comparison</button>
    <span> Finished {_esc(mc.get('finished_at') or '')}</span>
  </p>
  <div id="marketCompareBody" hidden></div>
</section>
"""


def render_report_compare_banner(run: dict) -> str:
    """Compact sticky-friendly block injected into full HTML reports."""
    mc = run.get("market_compare") or {}
    if not mc or mc.get("status") not in ("ok", "failed", "running"):
        return ""
    summary = mc.get("summary") or {}
    ticker = html.escape(str(run.get("ticker") or ""))
    run_id = html.escape(str(run.get("id") or ""))
    align = html.escape(str(summary.get("alignment") or mc.get("status") or ""))
    street = html.escape(str(summary.get("street_consensus") or "—"))
    ta = html.escape(str(summary.get("tradingagents_rating") or run.get("rating") or "—"))
    research = html.escape(str(summary.get("research_summary") or "")[:400])
    coincide = summary.get("coincide") or []
    differ = summary.get("differ") or []
    c_html = "".join(
        f"<li><strong>{html.escape(str(i.get('point') or ''))}</strong> — "
        f"{html.escape(str(i.get('why') or ''))}</li>"
        for i in coincide[:5]
    ) or "<li>n/a</li>"
    d_html = "".join(
        f"<li><strong>{html.escape(str(i.get('point') or ''))}</strong> — "
        f"{html.escape(str(i.get('why') or ''))}</li>"
        for i in differ[:5]
    ) or "<li>n/a</li>"
    return f"""
<section id="ta-market-compare" style="margin:1rem;padding:1rem 1.1rem;border:1px solid #2a3542;
border-radius:12px;background:#121820;color:#e7eef7;font:14px/1.45 system-ui,sans-serif">
  <div style="font:600 12px/1.2 system-ui;letter-spacing:0.08em;text-transform:uppercase;color:#8b9bb0;margin-bottom:0.5rem">
    Market analysts vs TradingAgents · {ticker}
  </div>
  <p style="margin:0 0 0.6rem">Alignment <strong>{align}</strong>
    · Street <strong>{street}</strong>
    · TradingAgents <strong>{ta}</strong>
    · <a href="/runs/{run_id}" style="color:#3d9cf0">Run details</a>
  </p>
  <p style="margin:0 0 0.75rem;color:#c5d0dc">{research}</p>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem">
    <div><div style="color:#8b9bb0;font-size:12px;text-transform:uppercase">Coincide</div><ul>{c_html}</ul></div>
    <div><div style="color:#8b9bb0;font-size:12px;text-transform:uppercase">Differ</div><ul>{d_html}</ul></div>
  </div>
</section>
"""
