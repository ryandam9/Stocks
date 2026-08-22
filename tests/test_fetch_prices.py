import numpy as np
import pandas as pd
import pytest

from config import AnalysisSettings
from fetch_prices import TRANSIENT_ERRORS, YahooFinanceDataFetcher, retry


class DummyFetcher(YahooFinanceDataFetcher):
    """Bypasses config loading so parsing/normalising can be tested directly."""

    def __init__(self, ticker_file, asset_types=("common_stock",), instrument_type="stocks"):
        analysis = AnalysisSettings(asset_types=list(asset_types))
        self.config = type(
            "C",
            (),
            {
                "ticker_file": str(ticker_file),
                "analysis": analysis,
                "instrument_type": instrument_type,
                # The real config seeds the universe; the stub is already there.
                "ensure_universe": lambda _self=None: str(ticker_file),
            },
        )()


def test_ticker_file_skips_comments_blanks_and_duplicates(tmp_path):
    path = tmp_path / "tickers.csv"
    path.write_text(
        "AAPL~Apple Inc.\n"
        "\n"
        "# a comment\n"
        "MSFT~Microsoft Corporation\n"
        "AAPL~Apple Inc.\n"
        "NONAME\n"
        "  SPACED  ~  Spaced Name  \n"
    )
    tickers = DummyFetcher(path)._read_ticker_file()

    assert tickers == [
        ("AAPL", "Apple Inc."),
        ("MSFT", "Microsoft Corporation"),
        ("NONAME", "NONAME"),
        ("SPACED", "Spaced Name"),
    ]


def test_ticker_file_excludes_warrants_and_units(tmp_path):
    """STK-005: derivative lines must not enter a 'stocks' screen."""
    path = tmp_path / "tickers.csv"
    path.write_text(
        "AAPL~Apple Inc.\n"
        "AACIW~Armada Acquisition Corp. III Warrant\n"
        "AACIU~Armada Acquisition Corp. III Units\n"
        "WHLRL~Wheeler REIT 7.00% Series D Cumulative Preferred\n"
    )
    assert DummyFetcher(path)._read_ticker_file() == [("AAPL", "Apple Inc.")]


def _frame():
    return pd.DataFrame(
        {
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Adj Close": [10.2],
            "Volume": [1000],
        },
        index=pd.DatetimeIndex(["2026-06-01"], name="Date"),
    )


def test_normalise_produces_expected_schema():
    out = YahooFinanceDataFetcher._normalise(
        _frame(), "AAA", "Alpha Inc", "2026-06-02 10:00:00", run_id="run-1"
    )
    assert list(out.columns) == [
        "stock_price_date",
        "ticker",
        "name",
        "fetch_time",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
        "price_basis",
        "fetch_run_id",
    ]
    assert out.loc[0, "price_basis"] == "adjusted"
    # R2-005: every price row identifies the fetch that produced it.
    assert out.loc[0, "fetch_run_id"] == "run-1"
    assert out.loc[0, "stock_price_date"] == "2026-06-01"
    assert out.loc[0, "close"] == 10.5
    assert out.loc[0, "adj_close"] == 10.2


def test_normalise_records_raw_fallback_when_adj_close_absent():
    """STK-004: a raw close must not masquerade as verified adjusted data."""
    frame = _frame().drop(columns=["Adj Close"])
    out = YahooFinanceDataFetcher._normalise(frame, "AAA", "Alpha", "2026-06-02 10:00:00")
    assert out.loc[0, "adj_close"] == out.loc[0, "close"]
    assert out.loc[0, "price_basis"] == "raw_fallback"


def test_extract_pulls_the_right_symbol_from_a_batch():
    columns = pd.MultiIndex.from_product(
        [["AAA", "BBB"], ["Open", "Close"]], names=["Ticker", "Price"]
    )
    raw = pd.DataFrame(
        [[1.0, 2.0, 3.0, 4.0]],
        columns=columns,
        index=pd.DatetimeIndex(["2026-06-01"], name="Date"),
    )
    assert YahooFinanceDataFetcher._extract(raw, "AAA")["Close"].iloc[0] == 2.0
    assert YahooFinanceDataFetcher._extract(raw, "BBB")["Close"].iloc[0] == 4.0


def test_extract_returns_none_for_unknown_symbol():
    columns = pd.MultiIndex.from_product([["AAA"], ["Close"]], names=["Ticker", "Price"])
    raw = pd.DataFrame([[1.0]], columns=columns, index=pd.DatetimeIndex(["2026-06-01"]))
    assert YahooFinanceDataFetcher._extract(raw, "MISSING") is None


def test_extract_drops_all_nan_symbol():
    """Unrecognised symbols still occupy columns, filled with NaN."""
    columns = pd.MultiIndex.from_product([["AAA", "BOGUS"], ["Close"]], names=["Ticker", "Price"])
    raw = pd.DataFrame([[1.0, np.nan]], columns=columns, index=pd.DatetimeIndex(["2026-06-01"]))
    assert YahooFinanceDataFetcher._extract(raw, "BOGUS").empty


def test_retry_stops_on_permanent_errors():
    """A delisted symbol fails identically every time; retrying just waits."""
    calls = []

    @retry(max_attempts=3, delay=0)
    def always_value_error():
        calls.append(1)
        raise ValueError("permanently bad symbol")

    with pytest.raises(ValueError):
        always_value_error()
    assert len(calls) == 1


def test_retry_reattempts_transient_errors_then_succeeds():
    calls = []

    @retry(max_attempts=3, delay=0)
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("transient")
        return "ok"

    assert flaky() == "ok"
    assert len(calls) == 3


def test_retry_reraises_after_exhausting_attempts():
    calls = []

    @retry(max_attempts=2, delay=0)
    def always_down():
        calls.append(1)
        raise TimeoutError("down")

    with pytest.raises(TimeoutError):
        always_down()
    assert len(calls) == 2


def test_rate_limit_error_is_treated_as_transient():
    from yfinance.exceptions import YFRateLimitError

    assert YFRateLimitError in TRANSIENT_ERRORS


# ---------------------------------------------------------------- T-06


def test_unexpected_exception_aborts_the_run(monkeypatch):
    """STK-006: a defect must fail loudly, not be logged as ticker failures.

    Catching bare Exception around a batch turned response-shape changes and
    normalisation bugs into "these tickers failed", publishing a partial
    dataset while hiding the real cause.
    """
    from fetch_prices import YahooFinanceDataFetcher

    class Fetcher(YahooFinanceDataFetcher):
        def __init__(self):
            self.batch_size = 2
            self.run_id = "test"

        def _fetch_batch(self, *args, **kwargs):
            raise AttributeError("response shape changed")

    with pytest.raises(AttributeError, match="response shape changed"):
        Fetcher()._fetch_all(["AAA", "BBB"], {}, "2026-01-01", "2026-02-01", "now")


def test_provider_errors_are_recovered_per_batch():
    """A recognised provider fault degrades to failed tickers, not a crash."""
    from yfinance.exceptions import YFPricesMissingError

    from fetch_prices import YahooFinanceDataFetcher

    class Fetcher(YahooFinanceDataFetcher):
        def __init__(self):
            self.batch_size = 2
            self.run_id = "test"

        def _fetch_batch(self, symbols, *args, **kwargs):
            raise YFPricesMissingError("AAA", "no prices")

    frames, failed = Fetcher()._fetch_all(["AAA", "BBB"], {}, "2026-01-01", "2026-02-01", "now")
    assert frames == []
    assert [symbol for symbol, _, _ in failed] == ["AAA", "BBB"]
    assert all(error_type == "YFPricesMissingError" for _, error_type, _ in failed)


# ---------------------------------------------------------------- T-08


def test_error_csv_is_rewritten_after_a_clean_run(tmp_path, monkeypatch):
    """STK-009: a stale failure list must not look like this run's failures."""
    import csv

    import yaml

    import config as cfg_mod
    import fetch_prices

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "us_stocks_config.yaml").write_text(
        yaml.safe_dump(
            {
                "config": {
                    "ticker_file": "config/universe.csv",
                    "data_dir": "us/stocks",
                    "db_path": "us.db",
                }
            }
        )
    )
    (tmp_path / "config" / "universe.csv").write_text("AAA~Alpha Inc\n")
    monkeypatch.setattr(cfg_mod, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(cfg_mod, "DEFAULT_DATA_ROOT", str(tmp_path / "data"))

    # Run 1: the provider returns nothing, so AAA is recorded as failed.
    monkeypatch.setattr(fetch_prices.yf, "download", lambda *a, **k: pd.DataFrame())
    fetcher = fetch_prices.YahooFinanceDataFetcher("US", "stocks", period=30)
    fetcher.fetch_historical_data()

    with open(fetcher.config.error_csv, newline="") as handle:
        assert [r["ticker"] for r in csv.DictReader(handle)] == ["AAA"]

    # Run 2: the provider succeeds, so the failure list must be emptied.
    def good_download(tickers, start, end, **kwargs):
        index = pd.DatetimeIndex(pd.bdate_range("2026-05-01", "2026-06-01"), name="Date")
        columns = pd.MultiIndex.from_product(
            [["AAA"], ["Open", "High", "Low", "Close", "Adj Close", "Volume"]],
            names=["Ticker", "Price"],
        )
        return pd.DataFrame(1.0, index=index, columns=columns)

    monkeypatch.setattr(fetch_prices.yf, "download", good_download)
    fetcher = fetch_prices.YahooFinanceDataFetcher("US", "stocks", period=30)
    data = fetcher.fetch_historical_data()
    assert not data.empty

    with open(fetcher.config.error_csv, newline="") as handle:
        assert list(csv.DictReader(handle)) == [], "run 1's failures must be cleared"
