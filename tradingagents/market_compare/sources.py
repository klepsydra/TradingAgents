"""Public analyst / research source URLs for a ticker (stock or ETF)."""

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
    """Ordered list of {name, url, kind} for research + citations."""
    t = (ticker or "").strip().upper()
    if not t:
        return []
    kind = asset_kind(t)
    tl = t.lower()
    rows: list[dict] = []

    def add(name: str, url: str, *, group: str) -> None:
        rows.append({"name": name, "url": url, "group": group, "asset_kind": kind})

    # Robinhood quote / Cortex surface
    add("Robinhood quote / Cortex", f"https://robinhood.com/stocks/{t}/", group="broker")
    add("Robinhood (alt path)", f"https://robinhood.com/us/en/stocks/{t}/", group="broker")

    # Core street / media desks from the shared analyst list
    add("Benzinga", f"https://www.benzinga.com/quote/{tl}", group="media")
    add("TipRanks", f"https://www.tipranks.com/{'etf' if kind == 'etf' else 'stocks'}/{tl}/{'etf-analysis' if kind == 'etf' else 'stock-analysis'}", group="analyst")
    add("Seeking Alpha analysis", f"https://seekingalpha.com/symbol/{t}/analysis", group="analyst")
    add("Seeking Alpha ratings", f"https://seekingalpha.com/symbol/{t}/ratings", group="analyst")
    if kind == "etf":
        add("Morningstar ETF", f"https://www.morningstar.com/etfs/arcx/{tl}/analysis", group="analyst")
        add("Morningstar (xmex fallback)", f"https://www.morningstar.com/etfs/xmex/{tl}/analysis", group="analyst")
        add("Barchart opinion", f"https://www.barchart.com/etfs-funds/quotes/{t}/opinion", group="technical")
        add("Barchart technicals", f"https://www.barchart.com/etfs-funds/quotes/{t}/technical-analysis", group="technical")
        add("StockAnalysis ETF", f"https://stockanalysis.com/etf/{tl}/", group="fundamentals")
        add("MarketBeat news", f"https://www.marketbeat.com/stocks/NYSEARCA/{t}/news/", group="media")
        add("PortfoliosLab", f"https://portfolioslab.com/symbol/{t}", group="fundamentals")
    else:
        add("Morningstar stock", f"https://www.morningstar.com/stocks/xnas/{tl}/analysis", group="analyst")
        add("Barchart opinion", f"https://www.barchart.com/stocks/quotes/{t}/opinion", group="technical")
        add("Barchart technicals", f"https://www.barchart.com/stocks/quotes/{t}/technical-analysis", group="technical")
        add("StockAnalysis", f"https://stockanalysis.com/stocks/{tl}/", group="fundamentals")
        add("MarketBeat", f"https://www.marketbeat.com/stocks/NASDAQ/{t}/", group="media")
        add("PortfoliosLab", f"https://portfolioslab.com/symbol/{t}", group="fundamentals")

    add("Motley Fool", f"https://www.fool.com/quote/{tl}/", group="media")
    add("Investopedia search", f"https://www.investopedia.com/search?q={t}", group="media")
    add("MarketWatch", f"https://www.marketwatch.com/investing/{'fund' if kind == 'etf' else 'stock'}/{tl}", group="media")
    add("CNBC", f"https://www.cnbc.com/quotes/{t}", group="media")
    add("Bloomberg quote", f"https://www.bloomberg.com/quote/{t}:US", group="media")
    add("TheStreet", f"https://www.thestreet.com/quote/{t}", group="media")

    return rows
