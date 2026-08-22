import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))


@pytest.fixture(autouse=True)
def isolate_data_root(monkeypatch):
    """Stop tests from ever touching the real data directory.

    Tests monkeypatch config.DEFAULT_DATA_ROOT to a tmp path, but
    load_config prefers the STOCKS_DATA_ROOT environment variable when it is
    set -- and it is set, in any shell that exports it. That precedence let a
    plain `pytest` run resolve the *real* configs and overwrite live price
    data. Clearing it for every test removes the possibility; .env is applied
    only by command entry points, which tests do not invoke.
    """
    monkeypatch.delenv("STOCKS_DATA_ROOT", raising=False)


@pytest.fixture
def latest_date():
    return pd.Timestamp("2026-06-02")


def make_series(
    ticker, name, start, end, start_price, end_price, volume=1_000_000, price_basis="adjusted"
):
    """Build a linear daily price series on business days."""
    dates = pd.bdate_range(start, end)
    if len(dates) == 1:
        prices = [start_price]
    else:
        step = (end_price - start_price) / (len(dates) - 1)
        prices = [start_price + step * i for i in range(len(dates))]
    return pd.DataFrame(
        {
            "stock_price_date": dates,
            "ticker": ticker,
            "name": name,
            "price": prices,
            "close": prices,
            "adj_close": prices,
            "volume": volume,
            "price_basis": price_basis,
        }
    )


@pytest.fixture
def build_frame():
    def _build(*series):
        df = pd.concat(series, ignore_index=True)
        return df.sort_values(["ticker", "stock_price_date"], kind="mergesort")

    return _build
