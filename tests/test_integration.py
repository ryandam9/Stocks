"""End-to-end tests over the analysis stage and run lifecycle.

The unit tests cover individual rules; these cover the boundaries where the
serious defects lived - sequential runs, output publication, and the
stale-data guard.
"""

import csv
import os
from datetime import UTC, datetime

import pandas as pd
import pytest
import yaml
from conftest import make_series

import config as cfg_mod
from analysis import StaleDataError, analyze_stocks, compute_window_growth
from config import RETURN_BASIS_ROBUST, AnalysisSettings, load_config

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_project(tmp_path, monkeypatch, windows=None, **analysis_overrides):
    """Create a self-contained project tree and return its resolved config."""
    (tmp_path / "config").mkdir(exist_ok=True)
    data_root = tmp_path / "data"
    (data_root / "us" / "stocks").mkdir(parents=True, exist_ok=True)

    analysis = {
        "min_price": 0.0,
        "min_median_volume": 0.0,
        "min_coverage": 0.8,
        "min_observation_ratio": 0.0,
        "endpoint_window": 1,
        "max_data_age_days": 3650,
        "asset_types": ["common_stock"],
        "windows": windows or [{"months": 1, "label": "1_month", "threshold": 10.0}],
    }
    analysis.update(analysis_overrides)

    (tmp_path / "config" / "us_stocks_config.yaml").write_text(
        yaml.safe_dump(
            {
                "config": {
                    "ticker_file": "config/universe.csv",
                    "data_dir": "us/stocks",
                    "db_path": "us.db",
                    "analysis": analysis,
                }
            }
        )
    )
    monkeypatch.setattr(cfg_mod, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(cfg_mod, "DEFAULT_DATA_ROOT", str(data_root))
    return load_config("US", "stocks")


def write_universe(cfg, rows):
    os.makedirs(os.path.dirname(cfg.ticker_file), exist_ok=True)
    with open(cfg.ticker_file, "w") as handle:
        for ticker, name in rows:
            handle.write(f"{ticker}~{name}\n")


def write_prices(cfg, frame):
    frame = frame.drop(columns=["price"], errors="ignore")
    frame.to_csv(cfg.eod_csv, index=False)


def read_csv_rows(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


# ---------------------------------------------------------------- T-01


def test_second_run_with_no_results_clears_the_first(tmp_path, monkeypatch):
    """STK-001: a run that matches nothing must not republish the last one.

    Previously the write was skipped when a window matched nothing, so the
    earlier run's CSV stayed on disk and the SQLite load picked it up as the
    current result.
    """
    cfg = build_project(tmp_path, monkeypatch)
    write_universe(cfg, [("BOOM", "Boom Inc")])

    # Run 1: BOOM rises 50% and qualifies.
    write_prices(cfg, make_series("BOOM", "Boom Inc", "2026-05-01", "2026-06-02", 100, 150))
    analyze_stocks(cfg, allow_stale=True)
    output = cfg.growth_csv("1_month")
    assert [r["ticker"] for r in read_csv_rows(output)] == ["BOOM"]

    # Run 2: BOOM is flat and qualifies for nothing.
    write_prices(cfg, make_series("BOOM", "Boom Inc", "2026-05-01", "2026-06-02", 100, 100))
    analyze_stocks(cfg, allow_stale=True)

    assert os.path.exists(output), "the file must still exist, with a header"
    assert read_csv_rows(output) == [], "run 1's result must not survive run 2"


def test_combined_growth_is_also_cleared(tmp_path, monkeypatch):
    """Same STK-001 guarantee for the optional price-history output."""
    cfg = build_project(tmp_path, monkeypatch, include_price_history=True)
    write_universe(cfg, [("BOOM", "Boom Inc")])

    write_prices(cfg, make_series("BOOM", "Boom Inc", "2026-05-01", "2026-06-02", 100, 150))
    analyze_stocks(cfg, allow_stale=True)
    assert read_csv_rows(cfg.combined_growth_csv)

    write_prices(cfg, make_series("BOOM", "Boom Inc", "2026-05-01", "2026-06-02", 100, 100))
    analyze_stocks(cfg, allow_stale=True)
    assert read_csv_rows(cfg.combined_growth_csv) == []


# ---------------------------------------------------------------- T-02


def test_stale_dataset_is_rejected(tmp_path, monkeypatch):
    """STK-002: an old CSV must not be screened as though it were current."""
    cfg = build_project(tmp_path, monkeypatch, max_data_age_days=5)
    write_universe(cfg, [("OLD", "Old Data Inc")])
    # Newest row is roughly a year in the past.
    end = (pd.Timestamp.now() - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
    start = (pd.Timestamp.now() - pd.Timedelta(days=395)).strftime("%Y-%m-%d")
    write_prices(cfg, make_series("OLD", "Old Data Inc", start, end, 100, 200))

    with pytest.raises(StaleDataError, match="days old"):
        analyze_stocks(cfg)


def test_allow_stale_overrides_the_freshness_guard(tmp_path, monkeypatch):
    cfg = build_project(tmp_path, monkeypatch, max_data_age_days=5)
    write_universe(cfg, [("OLD", "Old Data Inc")])
    end = (pd.Timestamp.now() - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
    start = (pd.Timestamp.now() - pd.Timedelta(days=395)).strftime("%Y-%m-%d")
    write_prices(cfg, make_series("OLD", "Old Data Inc", start, end, 100, 200))

    manifest = analyze_stocks(cfg, allow_stale=True)
    assert manifest.status == "success"


def test_fresh_dataset_passes(tmp_path, monkeypatch):
    cfg = build_project(tmp_path, monkeypatch, max_data_age_days=5)
    write_universe(cfg, [("NEW", "Fresh Inc")])
    end = pd.Timestamp.now().strftime("%Y-%m-%d")
    start = (pd.Timestamp.now() - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    write_prices(cfg, make_series("NEW", "Fresh Inc", start, end, 100, 200))

    assert analyze_stocks(cfg).status == "success"


# ---------------------------------------------------------------- T-03


def test_sparse_history_fails_observation_coverage(build_frame, latest_date):
    """STK-003: full span with almost no observations is not real coverage."""
    dense = make_series("DENSE", "Dense", "2025-06-02", "2026-06-02", 100, 200)
    # Two prints a year apart: ~100% span coverage, no usable history.
    sparse = dense[dense.index.isin([dense.index[0], dense.index[-1]])]
    df = build_frame(dense, sparse.assign(ticker="SPARSE", name="Sparse"))

    settings = AnalysisSettings(
        min_price=0.0,
        min_median_volume=0.0,
        min_coverage=0.8,
        endpoint_window=1,
        min_observation_ratio=0.5,
    )
    result, _ = compute_window_growth(df, {"months": 12}, 25.0, settings, latest_date, "US")
    tickers = set(result["ticker"])
    assert "DENSE" in tickers
    assert "SPARSE" not in tickers


def test_endpoint_windows_may_not_overlap(build_frame, latest_date):
    """Fewer than 2*endpoint_window prints makes the endpoints share days."""
    series = make_series("TINY", "Tiny", "2025-06-02", "2026-06-02", 100, 300)
    tiny = series.iloc[[0, 1, 2, -1]]
    settings = AnalysisSettings(
        min_price=0.0,
        min_median_volume=0.0,
        min_coverage=0.8,
        endpoint_window=3,
        min_observation_ratio=0.0,
        return_basis=RETURN_BASIS_ROBUST,
    )
    result, _ = compute_window_growth(
        build_frame(tiny), {"months": 12}, 25.0, settings, latest_date, "US"
    )
    assert result.empty


# ---------------------------------------------------------------- T-04


def test_raw_fallback_prices_are_excluded(build_frame, latest_date):
    """STK-004: unverified adjusted data must not be screened."""
    good = make_series("ADJ", "Adjusted", "2025-06-02", "2026-06-02", 100, 200)
    raw = make_series(
        "RAW",
        "Raw Fallback",
        "2025-06-02",
        "2026-06-02",
        100,
        200,
        price_basis="raw_fallback",
    )
    settings = AnalysisSettings(
        min_price=0.0,
        min_median_volume=0.0,
        min_coverage=0.8,
        endpoint_window=1,
        min_observation_ratio=0.0,
        # The exclusion guards the adjusted series; google_finance screens the
        # raw close deliberately and does not apply it.
        return_basis=RETURN_BASIS_ROBUST,
    )
    result, funnel = compute_window_growth(
        build_frame(good, raw), {"months": 12}, 25.0, settings, latest_date, "US"
    )
    assert set(result["ticker"]) == {"ADJ"}
    assert dict(funnel)["Adjusted prices"] == 1


def test_unknown_price_basis_is_screened_with_a_warning(build_frame, latest_date):
    """Legacy files predate provenance; excluding them all would report
    'nothing grew' for an entire dataset."""
    legacy = make_series(
        "OLD", "Legacy", "2025-06-02", "2026-06-02", 100, 200, price_basis="unknown"
    )
    settings = AnalysisSettings(
        min_price=0.0,
        min_median_volume=0.0,
        min_coverage=0.8,
        endpoint_window=1,
        min_observation_ratio=0.0,
    )
    result, _ = compute_window_growth(
        build_frame(legacy), {"months": 12}, 25.0, settings, latest_date, "US"
    )
    assert set(result["ticker"]) == {"OLD"}


def test_missing_price_basis_column_becomes_unknown(tmp_path):
    """close == adj_close must not be read as 'unadjusted'."""
    from analysis import BASIS_UNKNOWN, load_price_data

    series = make_series("AAA", "Alpha", "2025-06-02", "2025-07-02", 100, 200)
    series = series.drop(columns=["price", "price_basis"])
    csv_path = tmp_path / "legacy.csv"
    series.to_csv(csv_path, index=False)

    loaded = load_price_data(str(csv_path))
    assert (loaded["price_basis"] == BASIS_UNKNOWN).all()


# ---------------------------------------------------------------- T-05


def test_links_use_each_ticker_s_real_exchange(tmp_path, monkeypatch):
    """STK-005: a NYSE security must not be linked as NASDAQ."""
    cfg = build_project(tmp_path, monkeypatch)
    os.makedirs(os.path.dirname(cfg.ticker_file), exist_ok=True)
    with open(cfg.ticker_file, "w") as handle:
        handle.write("ticker,name,exchange,asset_type,currency,source_date\n")
        handle.write("AA,Alcoa Corporation,NYSE,common_stock,USD,2026-08-22\n")
        handle.write("AAPL,Apple Inc.,NASDAQ,common_stock,USD,2026-08-22\n")
        handle.write("MYST,Mystery Inc,UNKNOWN,common_stock,USD,2026-08-22\n")

    frames = [
        make_series(t, n, "2026-05-01", "2026-06-02", 100, 150)
        for t, n in [("AA", "Alcoa Corporation"), ("AAPL", "Apple Inc."), ("MYST", "Mystery Inc")]
    ]
    write_prices(cfg, pd.concat(frames, ignore_index=True))
    analyze_stocks(cfg, allow_stale=True)

    links = {r["ticker"]: r["google_finance"] for r in read_csv_rows(cfg.growth_csv("1_month"))}
    assert links["AA"].startswith("https://www.google.com/finance/quote/AA:NYSE")
    assert links["AAPL"].startswith("https://www.google.com/finance/quote/AAPL:NASDAQ")
    # An unknown exchange must produce no link rather than a guessed one.
    assert links["MYST"] == ""


def test_derivative_lines_are_excluded_from_a_stock_screen(tmp_path, monkeypatch):
    cfg = build_project(tmp_path, monkeypatch)
    write_universe(
        cfg,
        [
            ("GOOD", "Good Company Inc."),
            ("GOODW", "Good Company Inc. Warrant"),
            ("GOODU", "Good Company Inc. Units"),
        ],
    )
    frames = [
        make_series(t, n, "2026-05-01", "2026-06-02", 100, 150)
        for t, n in [
            ("GOOD", "Good Company Inc."),
            ("GOODW", "Good Company Inc. Warrant"),
            ("GOODU", "Good Company Inc. Units"),
        ]
    ]
    write_prices(cfg, pd.concat(frames, ignore_index=True))
    manifest = analyze_stocks(cfg, allow_stale=True)

    assert manifest.universe_total == 3
    assert manifest.universe_screened == 1
    assert {r["ticker"] for r in read_csv_rows(cfg.growth_csv("1_month"))} == {"GOOD"}


def test_geared_funds_are_held_out_of_the_screen_but_still_charted(tmp_path, monkeypatch):
    """A -2x fund's move is its underlying's, doubled and flipped.

    BBUS led a live one-year ASX screen at +591% on a single 2.84 -> 27.96
    step -- a 1-for-10 consolidation the provider applied forward but not
    backward. Nothing about the eligibility rules catches that, because the
    series is smooth on either side of it. Excluding the category does, and
    the history stays published so the fund is still chartable.
    """
    cfg = build_project(tmp_path, monkeypatch, asset_types=["etf"], include_universe_history=True)
    os.makedirs(os.path.dirname(cfg.ticker_file), exist_ok=True)
    with open(cfg.ticker_file, "w") as handle:
        handle.write("ticker,name,exchange,asset_type,issuer,category,currency,source_date\n")
        handle.write("GEAR,Geared Australian Equities ETF,ASX,etf,Betashares,,AUD,2026-08-29\n")
        handle.write("VAS,Australian Shares Index ETF,ASX,etf,Vanguard,,AUD,2026-08-29\n")

    frames = [
        make_series(t, n, "2026-05-01", "2026-06-02", 100, 150)
        for t, n in [
            ("GEAR", "Geared Australian Equities ETF"),
            ("VAS", "Australian Shares Index ETF"),
        ]
    ]
    write_prices(cfg, pd.concat(frames, ignore_index=True))
    manifest = analyze_stocks(cfg, allow_stale=True)

    # Category is left blank in the file above, so this also proves
    # load_universe fills it before the exclusion is applied.
    assert manifest.universe_total == 2
    assert manifest.universe_screened == 1
    assert {r["ticker"] for r in read_csv_rows(cfg.growth_csv("1_month"))} == {"VAS"}

    # The universe-wide history is taken before either filter, so an excluded
    # fund is still chartable -- it is kept out of the ranking, not the data.
    history_path = manifest.outputs["universe_history"]["path"]
    assert "GEAR" in {r["ticker"] for r in read_csv_rows(history_path)}


def test_screening_geared_funds_is_one_config_line_away(tmp_path, monkeypatch):
    cfg = build_project(tmp_path, monkeypatch, asset_types=["etf"], exclude_categories=[])
    os.makedirs(os.path.dirname(cfg.ticker_file), exist_ok=True)
    with open(cfg.ticker_file, "w") as handle:
        handle.write("ticker,name,exchange,asset_type,issuer,category,currency,source_date\n")
        handle.write("GEAR,Geared Australian Equities ETF,ASX,etf,Betashares,,AUD,2026-08-29\n")

    write_prices(
        cfg,
        make_series("GEAR", "Geared Australian Equities ETF", "2026-05-01", "2026-06-02", 100, 150),
    )
    analyze_stocks(cfg, allow_stale=True)

    rows = read_csv_rows(cfg.growth_csv("1_month"))
    assert [r["ticker"] for r in rows] == ["GEAR"]
    # Carried onto the row, so the app can label or group it without a join.
    assert rows[0]["issuer"] == "Betashares"
    assert rows[0]["category"] == "leveraged"


# ---------------------------------------------------------------- STK-011 / STK-015


def test_funnel_reports_every_rule(build_frame, latest_date):
    """The funnel must account for each documented rule, in order."""
    df = build_frame(make_series("AAA", "Alpha", "2025-06-02", "2026-06-02", 100, 200))
    settings = AnalysisSettings(
        min_price=0.0,
        min_median_volume=0.0,
        min_coverage=0.8,
        endpoint_window=1,
        min_observation_ratio=0.0,
    )
    _, funnel = compute_window_growth(df, {"months": 12}, 25.0, settings, latest_date, "US")
    stages = [label for label, _ in funnel]
    assert stages == [
        "Universe in window",
        "Enough span",
        "Enough observations",
        "Still trading",
        "Adjusted prices",
        "Liquid enough",
        "Above price floor",
        "Valid baseline",
        "Return above 25.0%",
    ]
    counts = [count for _, count in funnel]
    assert counts == sorted(counts, reverse=True), "a funnel may never grow"


def test_manifest_records_provenance(tmp_path, monkeypatch):
    """STK-015: a surprising result weeks later must be explainable."""
    cfg = build_project(tmp_path, monkeypatch)
    write_universe(cfg, [("AAA", "Alpha Inc")])
    write_prices(cfg, make_series("AAA", "Alpha Inc", "2026-05-01", "2026-06-02", 100, 150))

    manifest = analyze_stocks(cfg, allow_stale=True)
    assert manifest.run_id
    assert manifest.data_as_of == "2026-06-02"
    assert manifest.status == "success"
    assert manifest.thresholds["min_coverage"] == 0.8
    assert manifest.counts["1_month"]["Universe in window"] == 1

    path = os.path.join(cfg.data_dir, f"{cfg.prefix}_analysis_manifest.json")
    assert os.path.exists(path)


def test_every_growth_row_carries_run_id_and_data_as_of(tmp_path, monkeypatch):
    cfg = build_project(tmp_path, monkeypatch)
    write_universe(cfg, [("AAA", "Alpha Inc")])
    write_prices(cfg, make_series("AAA", "Alpha Inc", "2026-05-01", "2026-06-02", 100, 150))
    manifest = analyze_stocks(cfg, allow_stale=True)

    for row in read_csv_rows(cfg.growth_csv("1_month")):
        assert row["run_id"] == manifest.run_id
        assert row["data_as_of"] == "2026-06-02"


# ---------------------------------------------------------------- T-09


def test_invalid_config_values_fail_early(tmp_path, monkeypatch):
    """STK-010: bad thresholds must fail at load, not produce empty screens."""
    for overrides, expected in [
        ({"min_coverage": 1.5}, "min_coverage"),
        ({"min_coverage": 0.0}, "min_coverage"),
        ({"endpoint_window": 0}, "endpoint_window"),
        ({"min_price": -1.0}, "min_price"),
    ]:
        with pytest.raises(ValueError, match=expected):
            build_project(tmp_path, monkeypatch, **overrides)


def test_duplicate_window_labels_rejected(tmp_path, monkeypatch):
    windows = [
        {"months": 12, "label": "same", "threshold": 25.0},
        {"months": 6, "label": "same", "threshold": 25.0},
    ]
    with pytest.raises(ValueError, match="duplicate window label"):
        build_project(tmp_path, monkeypatch, windows=windows)


def test_unsafe_window_label_rejected(tmp_path, monkeypatch):
    windows = [{"months": 12, "label": "1 year; DROP TABLE x", "threshold": 25.0}]
    with pytest.raises(ValueError, match="must match"):
        build_project(tmp_path, monkeypatch, windows=windows)


# ---------------------------------------------------------------- T-10


def test_fetch_requests_an_exclusive_end_past_today(tmp_path, monkeypatch):
    """STK-008: yfinance treats `end` as exclusive.

    Passing today's date drops today's completed session, so the fetcher must
    request tomorrow and let the provider return whatever has actually closed.

    "Tomorrow" is the *exchange's*, not the host's. The clock is frozen at
    03:03 UTC, when a UTC machine has rolled over to the 26th and New York is
    still on the evening of the 25th, because that is the only time the two
    definitions differ -- and asserting against the host's date made this test
    fail for those hours of every single day.
    """
    import fetch_prices

    cfg = build_project(tmp_path, monkeypatch)
    write_universe(cfg, [("AAA", "Alpha Inc")])

    frozen = datetime(2026, 8, 26, 3, 3, tzinfo=UTC)  # 23:03 on the 25th in New York

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz else frozen.replace(tzinfo=None)

    monkeypatch.setattr(fetch_prices, "datetime", FrozenDatetime)

    captured = {}

    def fake_download(tickers, start, end, **kwargs):
        captured["start"], captured["end"] = start, end
        return pd.DataFrame()

    monkeypatch.setattr(fetch_prices.yf, "download", fake_download)
    fetcher = fetch_prices.YahooFinanceDataFetcher("US", "stocks", period=30)
    fetcher.fetch_historical_data()

    # The day after the exchange's date, which is the 26th -- not the 27th,
    # which is what the host's own calendar would have asked for.
    assert captured["end"] == "2026-08-26"
    assert captured["start"] == "2026-07-26"
