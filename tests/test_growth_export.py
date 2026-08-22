"""Regression tests for the combined growth CSV.

The original pipeline appended growth_periods to each row with awk as a bare
comma-separated string. sqlite-utils then parsed those commas as column
separators, so every ticker that grew in more than one window silently lost
all but its first period. These tests pin the round-trip.
"""

import csv

import pandas as pd
from conftest import make_series

from analysis import build_combined_growth

ALL_WINDOWS = {"1_year": "1Y", "6_months": "6M", "3_months": "3M", "1_month": "1M"}


def _growth_result(tickers):
    return pd.DataFrame({"ticker": tickers})


def test_growth_periods_survive_csv_round_trip(tmp_path):
    df = pd.concat(
        [
            make_series("AA", "Alcoa Corporation", "2025-06-02", "2025-06-10", 10, 20),
            make_series("BB", "Beta Inc", "2025-06-02", "2025-06-10", 10, 20),
        ],
        ignore_index=True,
    )
    results = {
        "1_year": _growth_result(["AA", "BB"]),
        "6_months": _growth_result(["AA"]),
        "3_months": _growth_result(["AA"]),
        "1_month": _growth_result(["AA"]),
    }

    path = build_combined_growth(df, results, str(tmp_path / "eod.csv"), ALL_WINDOWS)

    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))

    by_ticker = {row["ticker"]: row for row in rows}
    assert by_ticker["AA"]["growth_count"] == "4"
    assert by_ticker["AA"]["growth_periods"] == "1Y,6M,3M,1M"
    assert by_ticker["BB"]["growth_count"] == "1"
    assert by_ticker["BB"]["growth_periods"] == "1Y"

    # Every row must have the same field count; ragged rows are what corrupted
    # the SQLite load.
    assert all(len(row) == len(rows[0]) for row in rows)


def test_comma_containing_ticker_still_round_trips(tmp_path):
    """A name with a comma must not shift columns in the combined history.

    The name column itself is no longer stored here -- it is constant per
    ticker and joinable from the per-window tables -- but the row must still
    parse cleanly, and growth_periods still contains commas of its own.
    """
    name = "Grupo Aeromexico, S.A.B. de C.V"
    df = make_series("AERO", name, "2025-06-02", "2025-06-10", 10, 20)

    path = build_combined_growth(
        df, {"1_year": _growth_result(["AERO"])}, str(tmp_path / "eod.csv"), {"1_year": "1Y"}
    )

    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["ticker"] == "AERO"
    assert rows[0]["growth_periods"] == "1Y"
    assert all(len(r) == len(rows[0]) for r in rows), "no column shift"


def test_combined_history_omits_run_constant_columns(tmp_path):
    """Columns constant for a run were 72% of the published database."""
    from analysis import REDUNDANT_HISTORY_COLUMNS

    df = make_series("AA", "Alpha Inc", "2025-06-02", "2025-06-10", 10, 20)
    path = build_combined_growth(
        df, {"1_year": _growth_result(["AA"])}, str(tmp_path / "eod.csv"), {"1_year": "1Y"}
    )

    with open(path, newline="") as handle:
        header = set(next(csv.reader(handle)))
    assert not (header & set(REDUNDANT_HISTORY_COLUMNS)), header
    # The per-row facts must remain.
    assert {"stock_price_date", "ticker", "close", "volume", "growth_periods"} <= header


def test_periods_follow_configured_window_order(tmp_path):
    df = make_series("AA", "Alpha", "2025-06-02", "2025-06-10", 10, 20)
    results = {
        "1_month": _growth_result(["AA"]),
        "1_year": _growth_result(["AA"]),
        "3_months": _growth_result(["AA"]),
    }
    path = build_combined_growth(df, results, str(tmp_path / "eod.csv"), ALL_WINDOWS)
    with open(path, newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["growth_periods"] == "1Y,3M,1M"


def test_no_growth_still_writes_an_empty_file(tmp_path):
    """STK-001: skipping the write would leave a previous run's file in place."""
    df = make_series("AA", "Alpha", "2025-06-02", "2025-06-10", 10, 20)
    path = build_combined_growth(
        df, {"1_year": pd.DataFrame()}, str(tmp_path / "eod.csv"), ALL_WINDOWS
    )
    assert path is not None

    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == []
    # The header must survive so the file is still schema-valid.
    with open(path) as handle:
        assert "ticker" in handle.readline()
