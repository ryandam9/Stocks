import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))


@pytest.fixture
def latest_date():
    return pd.Timestamp("2026-06-02")


def make_series(ticker, name, start, end, start_price, end_price, volume=1_000_000):
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
            "volume": volume,
        }
    )


@pytest.fixture
def build_frame():
    def _build(*series):
        df = pd.concat(series, ignore_index=True)
        return df.sort_values(["ticker", "stock_price_date"], kind="mergesort")

    return _build
