from tradingagents.market_compare.sources import asset_kind, source_catalog


def test_voo_is_etf_and_has_core_sources():
    assert asset_kind("VOO") == "etf"
    urls = {row["url"] for row in source_catalog("VOO")}
    assert any("barchart.com/etfs-funds/quotes/VOO/opinion" in u for u in urls)
    assert any("tipranks.com/etf/voo" in u for u in urls)
    assert any("robinhood.com/stocks/VOO" in u for u in urls)
    assert any("seekingalpha.com/symbol/VOO/analysis" in u for u in urls)
    assert any("marketbeat.com/stocks/NYSEARCA/VOO" in u for u in urls)


def test_aapl_is_stock_shape():
    assert asset_kind("AAPL") == "stock"
    names = {row["name"] for row in source_catalog("AAPL")}
    assert "TipRanks" in names
    urls = " ".join(row["url"] for row in source_catalog("AAPL"))
    assert "etfs-funds" not in urls
    assert "barchart.com/stocks/quotes/AAPL/opinion" in urls
