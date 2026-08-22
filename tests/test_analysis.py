import pandas as pd
import pytest
from conftest import make_series

from analysis import (
    MAX_STALENESS_DAYS,
    compute_window_growth,
    load_price_data,
)
from config import AnalysisSettings

LOOSE = AnalysisSettings(
    min_price=0.0,
    min_median_volume=0.0,
    min_coverage=0.8,
    endpoint_window=1,
    min_observation_ratio=0.0,
)


def growth(df, latest_date, months=12, threshold=25.0, settings=LOOSE):
    """Return just the result table; the funnel is asserted separately."""
    result, _funnel = compute_window_growth(
        df, {"months": months}, threshold, settings, latest_date, "US"
    )
    return result.reset_index(drop=True)


def funnel_of(df, latest_date, months=12, threshold=25.0, settings=LOOSE):
    _result, funnel = compute_window_growth(
        df, {"months": months}, threshold, settings, latest_date, "US"
    )
    return dict(funnel)


def test_basic_percentage_change(build_frame, latest_date):
    df = build_frame(make_series("AAA", "Alpha", "2025-06-02", "2026-06-02", 100, 200))
    result = growth(df, latest_date)
    assert result.loc[0, "ticker"] == "AAA"
    assert result.loc[0, "pct_change"] == pytest.approx(100.0, abs=0.5)


def test_short_history_excluded_from_long_window(build_frame, latest_date):
    """A ticker listed weeks ago must not appear in the 1-year table.

    This is the bug the original implementation had: it took whatever the
    first row in the window was, so a 12-day-old listing was reported as
    1-year growth.
    """
    full = make_series("FULL", "Full History", "2025-06-02", "2026-06-02", 100, 200)
    recent = make_series("NEW", "Recent Listing", "2026-05-21", "2026-06-02", 100, 200)
    result = growth(build_frame(full, recent), latest_date)

    assert "FULL" in set(result["ticker"])
    assert "NEW" not in set(result["ticker"])


def test_short_history_still_qualifies_for_a_short_window(build_frame, latest_date):
    """The same ticker is legitimate in a window it actually spans."""
    recent = make_series("NEW", "Recent Listing", "2026-05-04", "2026-06-02", 100, 200)
    result = growth(build_frame(recent), latest_date, months=1, threshold=10.0)
    assert "NEW" in set(result["ticker"])


def test_delisted_ticker_excluded(build_frame, latest_date):
    """A stock that stopped printing is not a tradeable growth pick."""
    stale_end = latest_date - pd.Timedelta(days=MAX_STALENESS_DAYS + 20)
    stale = make_series("GONE", "Suspended", "2025-06-02", stale_end, 100, 300)
    assert growth(build_frame(stale), latest_date).empty


def test_illiquid_ticker_excluded(build_frame, latest_date):
    thin = make_series("THIN", "Illiquid", "2025-06-02", "2026-06-02", 100, 400, volume=50)
    settings = AnalysisSettings(
        min_price=0.0,
        min_median_volume=50_000,
        min_coverage=0.8,
        endpoint_window=1,
        min_observation_ratio=0.0,
    )
    assert growth(build_frame(thin), latest_date, settings=settings).empty


def test_price_floor_applies_to_latest_price(build_frame, latest_date):
    penny = make_series("PENNY", "Penny", "2025-06-02", "2026-06-02", 0.10, 5.0)
    settings = AnalysisSettings(
        min_price=10.0,
        min_median_volume=0.0,
        min_coverage=0.8,
        endpoint_window=1,
        min_observation_ratio=0.0,
    )
    assert growth(build_frame(penny), latest_date, settings=settings).empty


def test_threshold_is_exclusive(build_frame, latest_date):
    flat = make_series("FLAT", "Flat", "2025-06-02", "2026-06-02", 100, 125)
    assert growth(build_frame(flat), latest_date, threshold=25.0).empty


def test_endpoint_median_absorbs_a_single_bad_print(build_frame, latest_date):
    """One anomalous close must not define the whole window's return."""
    series = make_series("SPIKE", "Spiky", "2025-06-02", "2026-06-02", 100, 200)
    # Corrupt the final close with a 10x print.
    series.loc[series.index[-1], ["price", "close"]] = 2000.0
    df = build_frame(series)

    single = AnalysisSettings(
        min_price=0.0,
        min_median_volume=0.0,
        min_coverage=0.8,
        endpoint_window=1,
        min_observation_ratio=0.0,
    )
    smoothed = AnalysisSettings(
        min_price=0.0,
        min_median_volume=0.0,
        min_coverage=0.8,
        endpoint_window=3,
        min_observation_ratio=0.0,
    )
    assert growth(df, latest_date, settings=single).loc[0, "pct_change"] > 1500
    assert growth(df, latest_date, settings=smoothed).loc[0, "pct_change"] < 150


def test_duplicate_rows_do_not_affect_endpoints(tmp_path, latest_date):
    series = make_series("DUP", "Dup", "2025-06-02", "2026-06-02", 100, 200)
    doubled = pd.concat([series, series], ignore_index=True)
    csv = tmp_path / "eod.csv"
    doubled.drop(columns=["price"]).to_csv(csv, index=False)

    loaded = load_price_data(str(csv))
    assert len(loaded) == len(series)


def test_zero_and_negative_prices_dropped(tmp_path):
    series = make_series("ZERO", "Zero", "2025-06-02", "2025-07-02", 100, 200)
    series.loc[series.index[0], ["price", "close", "adj_close"]] = 0.0
    csv = tmp_path / "eod.csv"
    series.drop(columns=["price"]).to_csv(csv, index=False)

    assert (load_price_data(str(csv))["price"] > 0).all()


def test_adj_close_preferred_over_close(tmp_path):
    series = make_series("ADJ", "Adjusted", "2025-06-02", "2025-07-02", 100, 200)
    series["adj_close"] = series["price"] / 2
    csv = tmp_path / "eod.csv"
    series.drop(columns=["price"]).to_csv(csv, index=False)

    loaded = load_price_data(str(csv))
    assert loaded["price"].iloc[0] == pytest.approx(50.0)


def test_missing_required_column_raises(tmp_path):
    csv = tmp_path / "bad.csv"
    pd.DataFrame({"ticker": ["A"], "close": [1.0]}).to_csv(csv, index=False)
    with pytest.raises(ValueError, match="stock_price_date"):
        load_price_data(str(csv))


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_price_data(str(tmp_path / "nope.csv"))
