from dataclasses import replace

import pandas as pd
import pytest
from conftest import make_series

from analysis import (
    MAX_STALENESS_DAYS,
    _sample_price_history,
    compute_window_growth,
    endpoint_window_for,
    load_price_data,
)
from config import RETURN_BASIS_GOOGLE_FINANCE, RETURN_BASIS_ROBUST, AnalysisSettings

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

    # endpoint_window only bites under the robust basis; google_finance is
    # defined on single closes and would smooth nothing.
    single = AnalysisSettings(
        min_price=0.0,
        min_median_volume=0.0,
        min_coverage=0.8,
        endpoint_window=1,
        min_observation_ratio=0.0,
        return_basis=RETURN_BASIS_ROBUST,
    )
    smoothed = AnalysisSettings(
        min_price=0.0,
        min_median_volume=0.0,
        min_coverage=0.8,
        endpoint_window=3,
        min_observation_ratio=0.0,
        return_basis=RETURN_BASIS_ROBUST,
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

    loaded = load_price_data(str(csv), RETURN_BASIS_ROBUST)
    assert loaded["price"].iloc[0] == pytest.approx(50.0)


def test_missing_required_column_raises(tmp_path):
    csv = tmp_path / "bad.csv"
    pd.DataFrame({"ticker": ["A"], "close": [1.0]}).to_csv(csv, index=False)
    with pytest.raises(ValueError, match="stock_price_date"):
        load_price_data(str(csv))


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_price_data(str(tmp_path / "nope.csv"))


# ------------------------------------------------------------ price history sampling


def history_frame(dates, tickers=("AAA",)):
    return pd.DataFrame(
        [
            {"ticker": t, "stock_price_date": pd.Timestamp(d), "adj_close": 10.0 + i}
            for t in tickers
            for i, d in enumerate(dates)
        ]
    )


def kept_dates(frame, mode):
    out = _sample_price_history(frame, mode)
    return [d.strftime("%Y-%m-%d") for d in out["stock_price_date"]]


def test_weekly_sampling_keeps_the_last_session_of_each_week():
    """The last, not the first: a chart has to end on the newest close."""
    dates = [
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",
        "2026-01-12",
        "2026-01-13",
        "2026-01-14",
        "2026-01-15",
        "2026-01-16",
        "2026-01-19",
        "2026-01-20",
    ]
    assert kept_dates(history_frame(dates), "weekly") == [
        "2026-01-09",
        "2026-01-16",
        "2026-01-20",
    ]


def test_sampling_is_per_ticker():
    """One ticker's calendar must not decide which rows another keeps."""
    frame = pd.concat(
        [
            history_frame(["2026-01-05", "2026-01-09"], tickers=("AAA",)),
            history_frame(["2026-01-06"], tickers=("BBB",)),
        ]
    )
    out = _sample_price_history(frame, "weekly")
    assert {(r.ticker, r.stock_price_date.strftime("%Y-%m-%d")) for r in out.itertuples()} == {
        ("AAA", "2026-01-09"),
        ("BBB", "2026-01-06"),
    }


def test_month_boundary_sessions_are_not_stored_twice():
    """The flaw that ruled out first/fifteenth/last-of-month sampling.

    A month's last trading day and the next month's first are consecutive
    sessions -- true for 12 of 12 month boundaries in the last year of US
    data. Weekly buckets them by week, so it never stores two points a single
    day apart and calls it two samples.
    """
    kept = kept_dates(history_frame(["2026-01-29", "2026-01-30", "2026-02-02"]), "weekly")
    assert kept == ["2026-01-30", "2026-02-02"]  # Friday, then the following Monday


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("weekly", ["2026-01-09", "2026-01-16", "2026-01-20", "2026-02-02"]),
        ("semi_monthly", ["2026-01-09", "2026-01-20", "2026-02-02"]),
        ("month_end", ["2026-01-20", "2026-02-02"]),
    ],
)
def test_each_mode_keeps_its_period_ends(mode, expected):
    dates = ["2026-01-05", "2026-01-09", "2026-01-16", "2026-01-20", "2026-02-02"]
    assert kept_dates(history_frame(dates), mode) == expected


def test_daily_is_a_passthrough():
    dates = ["2026-01-05", "2026-01-09", "2026-01-16"]
    frame = history_frame(dates)
    assert kept_dates(frame, "daily") == dates
    assert _sample_price_history(frame, "daily") is frame


def test_every_mode_ends_on_the_newest_close():
    """Whatever the mode, the last row is the most recent session."""
    dates = ["2026-01-05", "2026-01-09", "2026-01-16", "2026-01-20", "2026-02-02"]
    for mode in ("daily", "weekly", "semi_monthly", "month_end"):
        assert kept_dates(history_frame(dates), mode)[-1] == "2026-02-02", mode


def test_sampling_an_empty_frame_is_safe():
    """No qualifying ticker is an ordinary outcome, not an error."""
    empty = history_frame(["2026-01-05"]).iloc[0:0]
    assert _sample_price_history(empty, "weekly").empty


# ------------------------------------------------------------------ threshold column


def test_threshold_column_records_the_bar_the_row_cleared(build_frame, latest_date):
    """A reader should see why a ticker qualified without opening the config."""
    df = build_frame(make_series("AAA", "Alpha", "2025-06-02", latest_date, 100, 200))
    result = growth(df, latest_date, months=12, threshold=25.0)

    assert result["threshold"].tolist() == [25.0]
    assert (result["pct_change"] > result["threshold"]).all()


def test_threshold_differs_per_window(build_frame, latest_date):
    """It is a per-window setting, so two tables in one database disagree."""
    df = build_frame(make_series("AAA", "Alpha", "2025-06-02", latest_date, 100, 200))

    # A linear 100 -> 200 year grows only ~4% in its final month, so the short
    # window needs a bar it can actually clear for this to test anything.
    year = growth(df, latest_date, months=12, threshold=25.0)
    month = growth(df, latest_date, months=1, threshold=1.0)

    assert year["threshold"].tolist() == [25.0]
    assert month["threshold"].tolist() == [1.0]


# ------------------------------------------------------------------ return_basis


GF = AnalysisSettings(
    min_price=0.0,
    min_median_volume=0.0,
    min_coverage=0.5,
    min_observation_ratio=0.0,
    return_basis=RETURN_BASIS_GOOGLE_FINANCE,
)


def test_google_finance_basis_ignores_endpoint_window(build_frame, latest_date):
    """Its endpoints are single closes, whatever endpoint_window is set to."""
    series = make_series("SPIKE", "Spiky", "2025-06-02", "2026-06-02", 100, 200)
    series.loc[series.index[-1], ["price", "close", "adj_close"]] = 2000.0
    df = build_frame(series)

    smoothed = replace(GF, endpoint_window=3)
    # A 3-day median would absorb the 10x print; Google Finance quotes the
    # close itself, so the spike must come straight through.
    assert growth(df, latest_date, settings=smoothed).loc[0, "pct_change"] > 1500
    assert endpoint_window_for(smoothed) == 1


def test_google_finance_window_opens_before_a_non_trading_anchor(build_frame):
    """A weekend anchor must take the previous session, not the next one."""
    # 2026-06-01 is a Monday, so the 3-month anchor is Saturday 2026-03-01.
    latest = pd.Timestamp("2026-06-01")
    series = make_series("AAA", "Alpha", "2025-06-02", "2026-06-01", 100, 200)
    df = build_frame(series)
    window = {"months": 3}

    gf, _ = compute_window_growth(df, window, -100.0, GF, latest, "US")
    robust, _ = compute_window_growth(
        df, window, -100.0, replace(GF, return_basis=RETURN_BASIS_ROBUST), latest, "US"
    )
    # Friday the 27th, not Monday the 2nd.
    assert gf.loc[0, "first_date"] == "2026-02-27"
    assert robust.loc[0, "first_date"] == "2026-03-02"


def test_google_finance_basis_uses_the_unadjusted_close(tmp_path):
    """Dividends must not inflate the return the way adj_close does."""
    series = make_series("DIV", "Dividend Payer", "2025-06-02", "2026-06-02", 100, 200)
    # A dividend-adjusted series sits below the raw close it was derived from.
    series["adj_close"] = series["price"] * 0.9
    csv = tmp_path / "eod.csv"
    series.drop(columns=["price"]).to_csv(csv, index=False)

    assert load_price_data(str(csv), RETURN_BASIS_GOOGLE_FINANCE)["price"].iloc[0] == 100.0
    assert load_price_data(str(csv), RETURN_BASIS_ROBUST)["price"].iloc[0] == 90.0


def test_google_finance_basis_keeps_raw_fallback_tickers(build_frame, latest_date):
    """The fallback marker describes adj_close, which this basis does not use."""
    raw = make_series(
        "RAW", "Raw Fallback", "2025-06-02", "2026-06-02", 100, 200, price_basis="raw_fallback"
    )
    result, funnel = compute_window_growth(
        build_frame(raw), {"months": 12}, 25.0, GF, latest_date, "US"
    )
    assert set(result["ticker"]) == {"RAW"}
    assert dict(funnel)["Adjusted prices"] == 1


def test_short_window_anchor_also_resolves_backwards(build_frame):
    """Day-denominated windows take the same previous-session anchor."""
    latest = pd.Timestamp("2026-06-01")  # Monday; minus 7 days is Monday the 25th
    df = build_frame(make_series("AAA", "Alpha", "2026-04-01", "2026-06-01", 100, 200))
    gf, _ = compute_window_growth(
        df, {"days": 7}, -100.0, replace(GF, min_coverage=0.1), latest, "US"
    )
    assert gf.loc[0, "first_date"] == "2026-05-25"
