from tradingagents.market_compare.sources import (
    ANALYST_FOCUS_NAMES,
    asset_kind,
    source_catalog,
)


def test_voo_is_etf_and_has_core_sources():
    assert asset_kind("VOO") == "etf"
    rows = source_catalog("VOO")
    urls = {row["url"] for row in rows}
    names = {row["name"] for row in rows}
    assert any("barchart.com/etfs-funds/quotes/VOO/opinion" in u for u in urls)
    assert any("tipranks.com/etf/voo" in u for u in urls)
    assert any("robinhood.com/stocks/VOO" in u for u in urls)
    assert "https://robinhood.com/news/VOO" in urls
    assert "Robinhood news" in names
    assert any("seekingalpha.com/symbol/VOO/analysis" in u for u in urls)
    assert any("marketbeat.com/stocks/NYSEARCA/VOO" in u for u in urls)
    # ROBIN-1894 research-tool list
    assert "Finviz ETF" in names
    assert "Yahoo Finance" in names
    assert "TradingView" in names
    assert "Koyfin" in names
    assert "TIKR" in names
    assert "Business Quant" in names
    assert "Stock Analysis ETF" in names
    assert "Quartr" in names
    assert "stocksbrew" in names
    assert "Simply Wall St" in names
    assert "Quiver Quantitative" in names
    assert "Google Finance" in names
    assert "Ticker Nerd" in names
    assert "Stock Rover" in names


def test_aapl_is_stock_shape():
    assert asset_kind("AAPL") == "stock"
    names = {row["name"] for row in source_catalog("AAPL")}
    assert "TipRanks" in names
    assert "Finviz" in names
    assert "Yahoo Finance analysis" in names
    assert "TradingView" in names
    assert "Koyfin" in names
    assert "TIKR" in names
    assert "Business Quant" in names
    assert "Quartr" in names
    assert "stocksbrew" in names
    urls = " ".join(row["url"] for row in source_catalog("AAPL"))
    assert "etfs-funds" not in urls
    assert "barchart.com/stocks/quotes/AAPL/opinion" in urls
    assert "finviz.com/quote.ashx?t=AAPL" in urls
    assert "finance.yahoo.com/quote/AAPL/analysis" in urls
    assert "tradingview.com/symbols/AAPL" in urls
    assert "app.koyfin.com/expected/AAPL" in urls
    assert "app.tikr.com/stock/aapl" in urls
    assert "businessquant.com/company/aapl" in urls
    assert "quartr.com/companies/aapl" in urls
    assert "stocksbrew.online/stocks/aapl" in urls
    assert "https://robinhood.com/news/AAPL" in urls
    assert "Robinhood news" in names


def test_robinhood_news_url_shape():
    # https://robinhood.com/news/NOW — NOW is only an example ticker
    urls = {row["url"] for row in source_catalog("NOW")}
    assert "https://robinhood.com/news/NOW" in urls
    assert "robinhood news" in " ".join(ANALYST_FOCUS_NAMES).lower()


def test_analyst_focus_covers_robin1894_lists():
    focus = " ".join(ANALYST_FOCUS_NAMES).lower()
    for name in (
        "finviz",
        "koyfin",
        "stock analysis",
        "tikr",
        "tradingview",
        "yahoo finance",
        "business quant",
        "quartr",
        "seeking alpha",
        "stocksbrew",
        "ticker nerd",
        "stock rover",
        "simply wall st",
        "quiver",
        "tipranks",
        "motley fool",
        "investopedia",
        "robinhood news",
    ):
        assert name in focus, name


def test_empty_ticker():
    assert source_catalog("") == []
    assert source_catalog("  ") == []
