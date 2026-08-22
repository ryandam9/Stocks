import numpy as np
import pandas as pd
import pytest

from fetch_prices import TRANSIENT_ERRORS, YahooFinanceDataFetcher, retry


class DummyFetcher(YahooFinanceDataFetcher):
    """Bypasses config loading so parsing/normalising can be tested directly."""

    def __init__(self, ticker_file):
        self.config = type("C", (), {"ticker_file": str(ticker_file)})()


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


def _frame():
    return pd.DataFrame(
        {
            "Open": [10.0], "High": [11.0], "Low": [9.0],
            "Close": [10.5], "Adj Close": [10.2], "Volume": [1000],
        },
        index=pd.DatetimeIndex(["2026-06-01"], name="Date"),
    )


def test_normalise_produces_expected_schema():
    out = YahooFinanceDataFetcher._normalise(_frame(), "AAA", "Alpha Inc", "2026-06-02 10:00:00")
    assert list(out.columns) == [
        "stock_price_date", "ticker", "name", "fetch_time",
        "open", "high", "low", "close", "adj_close", "volume",
    ]
    assert out.loc[0, "stock_price_date"] == "2026-06-01"
    assert out.loc[0, "close"] == 10.5
    assert out.loc[0, "adj_close"] == 10.2


def test_normalise_falls_back_when_adj_close_absent():
    frame = _frame().drop(columns=["Adj Close"])
    out = YahooFinanceDataFetcher._normalise(frame, "AAA", "Alpha", "2026-06-02 10:00:00")
    assert out.loc[0, "adj_close"] == out.loc[0, "close"]


def test_extract_pulls_the_right_symbol_from_a_batch():
    columns = pd.MultiIndex.from_product(
        [["AAA", "BBB"], ["Open", "Close"]], names=["Ticker", "Price"]
    )
    raw = pd.DataFrame(
        [[1.0, 2.0, 3.0, 4.0]], columns=columns,
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
    columns = pd.MultiIndex.from_product(
        [["AAA", "BOGUS"], ["Close"]], names=["Ticker", "Price"]
    )
    raw = pd.DataFrame(
        [[1.0, np.nan]], columns=columns, index=pd.DatetimeIndex(["2026-06-01"])
    )
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
