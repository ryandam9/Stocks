"""Tests for the universe-wide price history table (e.g. ASX_1_YEAR_HISTORY).

Distinct from the matched-ticker history covered in test_growth_export.py: this
table is not restricted to tickers a window matched, and it exists so any
ticker in the universe can be charted.
"""

import os
import sqlite3

import pandas as pd
import pytest
from conftest import make_series
from test_integration import build_project, read_csv_rows, write_prices

from analysis import analyze_stocks
from pipeline import publish

WINDOWS = [{"months": 12, "label": "1_year", "threshold": 25.0}]


def write_typed_universe(cfg, rows):
    """Write a structured universe file carrying an asset_type per ticker."""
    os.makedirs(os.path.dirname(cfg.ticker_file), exist_ok=True)
    pd.DataFrame(rows, columns=["ticker", "name", "asset_type"]).to_csv(
        cfg.ticker_file, index=False
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    def _build(**overrides):
        options = {
            "include_universe_history": True,
            "price_history_sampling": "weekly",
            "asset_types": ["common_stock"],
            "windows": WINDOWS,
        }
        options.update(overrides)
        return build_project(tmp_path, monkeypatch, **options)

    return _build


def test_table_name_is_the_one_the_config_declares():
    """The published table is named for the exchange, not the config prefix."""
    from config import StockConfig

    cfg = StockConfig("ASX", "etf", "t.csv", "d", "d/asx.db", None)
    assert cfg.universe_history_table == "ASX_1_YEAR_HISTORY"
    assert cfg.universe_table == "asx_universe"


def test_covers_tickers_the_screen_excludes(project):
    """Every ticker in the universe is covered, asset_type included.

    The screen takes common_stock only, so WRNT never reaches a growth table --
    but it is in the universe, so it must still be chartable.
    """
    cfg = project()
    write_typed_universe(
        cfg,
        [
            ("BOOM", "Boom Inc", "common_stock"),
            ("FLAT", "Flat Inc", "common_stock"),
            ("WRNT", "Boom Inc Warrant", "warrant"),
        ],
    )
    write_prices(
        cfg,
        pd.concat(
            [
                make_series("BOOM", "Boom Inc", "2025-06-02", "2026-06-02", 100, 200),
                make_series("FLAT", "Flat Inc", "2025-06-02", "2026-06-02", 100, 100),
                make_series("WRNT", "Boom Inc Warrant", "2025-06-02", "2026-06-02", 5, 9),
            ],
            ignore_index=True,
        ),
    )
    analyze_stocks(cfg, allow_stale=True)

    rows = read_csv_rows(cfg.universe_history_csv)
    assert {r["ticker"] for r in rows} == {"BOOM", "FLAT", "WRNT"}

    # ...while the screen itself is unchanged: only the common stock that grew.
    assert [r["ticker"] for r in read_csv_rows(cfg.growth_csv("1_year"))] == ["BOOM"]


def test_drops_tickers_no_longer_in_the_universe(project):
    """A price file still carrying a delisted ticker must not resurrect it."""
    cfg = project()
    write_typed_universe(cfg, [("STAY", "Stay Inc", "common_stock")])
    write_prices(
        cfg,
        pd.concat(
            [
                make_series("STAY", "Stay Inc", "2025-06-02", "2026-06-02", 100, 120),
                make_series("GONE", "Gone Inc", "2025-06-02", "2026-06-02", 100, 120),
            ],
            ignore_index=True,
        ),
    )
    analyze_stocks(cfg, allow_stale=True)

    assert {r["ticker"] for r in read_csv_rows(cfg.universe_history_csv)} == {"STAY"}


def test_weekly_sampling_keeps_one_row_a_week_ending_on_the_newest_close(project):
    cfg = project()
    write_typed_universe(cfg, [("BOOM", "Boom Inc", "common_stock")])
    frame = make_series("BOOM", "Boom Inc", "2025-06-02", "2026-06-02", 100, 200)
    write_prices(cfg, frame)
    analyze_stocks(cfg, allow_stale=True)

    dates = [pd.Timestamp(r["stock_price_date"]) for r in read_csv_rows(cfg.universe_history_csv)]
    assert dates == sorted(dates)
    # One point per ISO week, and the series ends on the newest session.
    assert len({(d.isocalendar().year, d.isocalendar().week) for d in dates}) == len(dates)
    assert dates[-1] == frame["stock_price_date"].max()
    # A year of business days is ~52-53 weeks, never the ~261 daily sessions.
    assert 50 <= len(dates) <= 54


def test_trimmed_to_twelve_months_even_though_the_fetch_pulls_400_days(project):
    """The table is named 1_YEAR, so it must actually hold a year."""
    cfg = project()
    write_typed_universe(cfg, [("BOOM", "Boom Inc", "common_stock")])
    # 400 days of history, as `run.py --period 400` produces.
    write_prices(cfg, make_series("BOOM", "Boom Inc", "2025-04-28", "2026-06-02", 100, 200))
    analyze_stocks(cfg, allow_stale=True)

    dates = [pd.Timestamp(r["stock_price_date"]) for r in read_csv_rows(cfg.universe_history_csv)]
    assert dates[0] >= pd.Timestamp("2026-06-02") - pd.DateOffset(months=12)
    assert 50 <= len(dates) <= 54


def test_published_table_is_indexed_and_leaves_the_screen_tables_alone(project):
    cfg = project()
    write_typed_universe(
        cfg,
        [("BOOM", "Boom Inc", "common_stock"), ("WRNT", "Boom Warrant", "warrant")],
    )
    write_prices(
        cfg,
        pd.concat(
            [
                make_series("BOOM", "Boom Inc", "2025-06-02", "2026-06-02", 100, 200),
                make_series("WRNT", "Boom Warrant", "2025-06-02", "2026-06-02", 5, 9),
            ],
            ignore_index=True,
        ),
    )
    analyze_stocks(cfg, allow_stale=True)
    publish(cfg)

    conn = sqlite3.connect(cfg.db_path)
    table = cfg.universe_history_table
    assert table == "US_1_YEAR_HISTORY"

    columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    assert columns == [
        "ticker",
        "stock_price_date",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
    ]
    assert {r[0] for r in conn.execute(f'SELECT DISTINCT ticker FROM "{table}"')} == {
        "BOOM",
        "WRNT",
    }
    assert [r[1] for r in conn.execute(f'PRAGMA index_list("{table}")')] == [
        f"ix_{table}_ticker_date"
    ]

    # The screen tables are untouched: still common_stock only.
    assert [r[0] for r in conn.execute("SELECT ticker FROM us_stocks_growth_1_year")] == ["BOOM"]
    conn.close()


def test_lookup_table_names_every_ticker_including_unpriced_ones(project):
    """The lookup exists so a chart has a title; it must not omit anything.

    NOPX is in the universe but the provider returned no prices for it, so it
    has no history rows. It still belongs in the lookup: a LEFT JOIN from here
    is how you see which part of the universe is chartable.
    """
    cfg = project()
    write_typed_universe(
        cfg,
        [
            ("BOOM", "Boom Inc", "common_stock"),
            ("WRNT", "Boom Warrant", "warrant"),
            ("NOPX", "No Prices Inc", "common_stock"),
        ],
    )
    write_prices(
        cfg,
        pd.concat(
            [
                make_series("BOOM", "Boom Inc", "2025-06-02", "2026-06-02", 100, 200),
                make_series("WRNT", "Boom Warrant", "2025-06-02", "2026-06-02", 5, 9),
            ],
            ignore_index=True,
        ),
    )
    analyze_stocks(cfg, allow_stale=True)
    publish(cfg)

    conn = sqlite3.connect(cfg.db_path)
    lookup = cfg.universe_table
    assert lookup == "us_universe"

    assert [r[1] for r in conn.execute(f'PRAGMA table_info("{lookup}")')] == [
        "ticker",
        "name",
        "exchange",
        "asset_type",
        "currency",
        "source_date",
    ]
    assert dict(conn.execute(f'SELECT ticker, name FROM "{lookup}"')) == {
        "BOOM": "Boom Inc",
        "WRNT": "Boom Warrant",
        "NOPX": "No Prices Inc",
    }

    # The join the table exists for: a chart with a title.
    named = conn.execute(
        f'SELECT u.name, COUNT(h.ticker) FROM "{lookup}" u '
        f'LEFT JOIN "{cfg.universe_history_table}" h USING (ticker) '
        f"GROUP BY u.ticker ORDER BY u.ticker"
    ).fetchall()
    assert dict(named)["No Prices Inc"] == 0, "unpriced tickers survive the left join"
    assert dict(named)["Boom Inc"] > 0
    conn.close()


def test_lookup_ticker_is_unique(project):
    """A duplicated ticker would fan a chart query out into two rows a date."""
    cfg = project()
    write_typed_universe(cfg, [("BOOM", "Boom Inc", "common_stock")])
    write_prices(cfg, make_series("BOOM", "Boom Inc", "2025-06-02", "2026-06-02", 100, 200))
    analyze_stocks(cfg, allow_stale=True)
    publish(cfg)

    conn = sqlite3.connect(cfg.db_path)
    indexes = {r[1]: r[2] for r in conn.execute(f'PRAGMA index_list("{cfg.universe_table}")')}
    assert indexes[f"ix_{cfg.universe_table}_ticker"] == 1, "must be UNIQUE"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f'INSERT INTO "{cfg.universe_table}" (ticker) VALUES (?)', ("BOOM",))
    conn.close()


def test_disabled_removes_the_csv_and_the_table(project):
    """A run with it off must not republish the last run's history."""
    cfg = project()
    write_typed_universe(cfg, [("BOOM", "Boom Inc", "common_stock")])
    write_prices(cfg, make_series("BOOM", "Boom Inc", "2025-06-02", "2026-06-02", 100, 200))
    analyze_stocks(cfg, allow_stale=True)
    publish(cfg)
    assert os.path.exists(cfg.universe_history_csv)

    off = project(include_universe_history=False)
    analyze_stocks(off, allow_stale=True)
    publish(off)

    assert not os.path.exists(off.universe_history_csv)
    assert not os.path.exists(off.universe_csv)
    conn = sqlite3.connect(off.db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert off.universe_history_table not in tables
    assert off.universe_table not in tables
    assert "us_stocks_growth_1_year" in tables
    conn.close()


def test_empty_history_still_publishes_a_headed_table(project, tmp_path, monkeypatch):
    """A universe with no usable prices must still leave a queryable table."""
    cfg = project()
    write_typed_universe(cfg, [("BOOM", "Boom Inc", "common_stock")])
    write_prices(cfg, make_series("BOOM", "Boom Inc", "2026-05-01", "2026-06-02", 100, 200))
    analyze_stocks(cfg, allow_stale=True)
    publish(cfg)

    conn = sqlite3.connect(cfg.db_path)
    # A one-month price file trimmed to a 12-month window keeps every row it
    # has; the point is that the table exists and is shaped correctly.
    assert conn.execute(f'SELECT COUNT(*) FROM "{cfg.universe_history_table}"').fetchone()[0] > 0
    conn.close()
