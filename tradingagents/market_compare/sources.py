"""Public analyst / research source URLs for a ticker (stock or ETF).

Catalog blends the original TradingAgents street list with the free/paid
research tools called out for ROBIN-1894 (Business Quant, StocksBrew 2026,
Ticker Nerd comparisons): Finviz, Koyfin, Stock Analysis, TIKR, TradingView,
Yahoo Finance, Business Quant, Quartr, Seeking Alpha, TipRanks, Motley Fool,
Investopedia, Stock Rover, Simply Wall St, Quiver Quantitative, Google Finance,
Ticker Nerd, plus RH Cortex / Benzinga / Morningstar / Barchart / MarketBeat /
CNBC / Bloomberg / TheStreet / MarketWatch / PortfoliosLab.
"""

from __future__ import annotations

# Common US-listed ETFs (extend as needed). Used only to pick URL shapes.
_ETF_TICKERS = {
    "SPY", "VOO", "IVV", "QQQ", "DIA", "IWM", "VTI", "VXUS", "VEA", "VWO",
    "AGG", "BND", "TLT", "IEF", "LQD", "HYG", "GLD", "SLV", "USO", "UNG",
    "XLE", "XLF", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE",
    "SMH", "SOXX", "ARKK", "TQQQ", "SQQQ", "UPRO", "SPXU", "UVXY", "VIXY",
    "SCHD", "VIG", "VYM", "JEPI", "JEPQ", "QYLD", "RYLD", "SPHD",
}


def asset_kind(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    return "etf" if t in _ETF_TICKERS else "stock"


def source_catalog(ticker: str) -> list[dict]:
    """Ordered list of {name, url, group, asset_kind} for research + citations.

    Overlaps are intentional (same desk, multiple surfaces). Gather skips
    paywalls / empty pages and reuses a recent on-disk cache when live fetch
    fails — see ``gather.fetch_page_snippets``.
    """
    t = (ticker or "").strip().upper()
    if not t:
        return []
    kind = asset_kind(t)
    tl = t.lower()
    rows: list[dict] = []

    def add(name: str, url: str, *, group: str) -> None:
        rows.append({"name": name, "url": url, "group": group, "asset_kind": kind})

    # —— Broker / Cortex / RH news ——
    add("Robinhood quote / Cortex", f"https://robinhood.com/stocks/{t}/", group="broker")
    add("Robinhood (alt path)", f"https://robinhood.com/us/en/stocks/{t}/", group="broker")
    # e.g. https://robinhood.com/news/NOW — ticker-scoped news feed
    add("Robinhood news", f"https://robinhood.com/news/{t}", group="media")

    # —— Analyst ratings / written research (priority for coincide/differ) ——
    add(
        "TipRanks",
        f"https://www.tipranks.com/{'etf' if kind == 'etf' else 'stocks'}/{tl}/"
        f"{'etf-analysis' if kind == 'etf' else 'stock-analysis'}",
        group="analyst",
    )
    add("TipRanks analysts", f"https://www.tipranks.com/stocks/{tl}/analysts", group="analyst")
    add("Seeking Alpha analysis", f"https://seekingalpha.com/symbol/{t}/analysis", group="analyst")
    add("Seeking Alpha ratings", f"https://seekingalpha.com/symbol/{t}/ratings", group="analyst")
    add("Motley Fool", f"https://www.fool.com/quote/{tl}/", group="analyst")
    add("Simply Wall St", f"https://simplywall.st/stocks/us/{tl}", group="analyst")
    add("Ticker Nerd", f"https://tickernerd.com/stocks/{tl}/", group="analyst")
    add("stocksbrew", f"https://www.stocksbrew.online/stocks/{tl}", group="analyst")

    if kind == "etf":
        add("Morningstar ETF", f"https://www.morningstar.com/etfs/arcx/{tl}/analysis", group="analyst")
        add("Morningstar (xmex fallback)", f"https://www.morningstar.com/etfs/xmex/{tl}/analysis", group="analyst")
        add("Barchart opinion", f"https://www.barchart.com/etfs-funds/quotes/{t}/opinion", group="technical")
        add("Barchart technicals", f"https://www.barchart.com/etfs-funds/quotes/{t}/technical-analysis", group="technical")
        add("Stock Analysis ETF", f"https://stockanalysis.com/etf/{tl}/", group="fundamentals")
        add("Stock Analysis ETF forecast", f"https://stockanalysis.com/etf/{tl}/forecast/", group="analyst")
        add("MarketBeat news", f"https://www.marketbeat.com/stocks/NYSEARCA/{t}/news/", group="media")
        add("MarketBeat ratings", f"https://www.marketbeat.com/stocks/NYSEARCA/{t}/price-target/", group="analyst")
        add("PortfoliosLab", f"https://portfolioslab.com/symbol/{t}", group="fundamentals")
        add("Finviz ETF", f"https://finviz.com/quote.ashx?t={t}&p=d", group="screener")
        add("Yahoo Finance", f"https://finance.yahoo.com/quote/{t}/", group="media")
        add("TradingView", f"https://www.tradingview.com/symbols/{t}/", group="technical")
    else:
        add("Morningstar stock", f"https://www.morningstar.com/stocks/xnas/{tl}/analysis", group="analyst")
        add("Barchart opinion", f"https://www.barchart.com/stocks/quotes/{t}/opinion", group="technical")
        add("Barchart technicals", f"https://www.barchart.com/stocks/quotes/{t}/technical-analysis", group="technical")
        add("Stock Analysis", f"https://stockanalysis.com/stocks/{tl}/", group="fundamentals")
        add("Stock Analysis forecast", f"https://stockanalysis.com/stocks/{tl}/forecast/", group="analyst")
        add("MarketBeat", f"https://www.marketbeat.com/stocks/NASDAQ/{t}/", group="media")
        add("MarketBeat ratings", f"https://www.marketbeat.com/stocks/NASDAQ/{t}/price-target/", group="analyst")
        add("PortfoliosLab", f"https://portfolioslab.com/symbol/{t}", group="fundamentals")
        add("Finviz", f"https://finviz.com/quote.ashx?t={t}", group="screener")
        add("Yahoo Finance", f"https://finance.yahoo.com/quote/{t}/", group="media")
        add("Yahoo Finance analysis", f"https://finance.yahoo.com/quote/{t}/analysis/", group="analyst")
        add("TradingView", f"https://www.tradingview.com/symbols/{t}/", group="technical")

    # —— Free research workspace / data platforms (Business Quant 7 + peers) ——
    add("Koyfin", f"https://app.koyfin.com/expected/{t}", group="fundamentals")
    add("TIKR", f"https://app.tikr.com/stock/{tl}", group="fundamentals")
    add("Business Quant", f"https://businessquant.com/company/{tl}", group="fundamentals")
    add("Stock Rover", f"https://www.stockrover.com/research/{t}", group="screener")
    add("Quiver Quantitative", f"https://www.quiverquant.com/stock/{t}/", group="alt_data")
    add("Quartr", f"https://quartr.com/companies/{tl}", group="earnings")
    add("Google Finance", f"https://www.google.com/finance/quote/{t}:NASDAQ", group="media")
    add("Google Finance (NYSE fallback)", f"https://www.google.com/finance/quote/{t}:NYSE", group="media")

    # —— News / desks ——
    add("Benzinga", f"https://www.benzinga.com/quote/{tl}", group="media")
    add("Investopedia search", f"https://www.investopedia.com/search?q={t}", group="media")
    add(
        "MarketWatch",
        f"https://www.marketwatch.com/investing/{'fund' if kind == 'etf' else 'stock'}/{tl}",
        group="media",
    )
    add("CNBC", f"https://www.cnbc.com/quotes/{t}", group="media")
    add("Bloomberg quote", f"https://www.bloomberg.com/quote/{t}:US", group="media")
    add("TheStreet", f"https://www.thestreet.com/quote/{t}", group="media")

    return rows


# Names Perplexity is told to prioritize for *ratings + explanations*
# (paywalled pages are fine — Sonar can still summarize public surfaces).
ANALYST_FOCUS_NAMES = (
    "TipRanks",
    "Seeking Alpha",
    "Morningstar",
    "Motley Fool",
    "Yahoo Finance",
    "Finviz",
    "Stock Analysis",
    "TradingView",
    "TIKR",
    "Koyfin",
    "Simply Wall St",
    "MarketBeat",
    "Benzinga",
    "CNBC",
    "Bloomberg",
    "Quartr",
    "Quiver Quantitative",
    "Ticker Nerd",
    "stocksbrew",
    "Business Quant",
    "Stock Rover",
    "Investopedia",
    "Robinhood Cortex",
    "Robinhood news",
    "Barchart",
)
