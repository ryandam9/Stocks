"""Regression tests for the Round 2 review findings (R2-001 .. R2-012)."""

import json
import os
import re

import pandas as pd
import pytest
from conftest import make_series
from test_integration import build_project, write_prices, write_universe

import analysis
import config as cfg_mod
import fetch_prices
from analysis import _worst_price_basis, compute_window_growth
from config import AnalysisSettings, growth_schema_sql, load_config
from runmeta import atomic_write_csv, atomic_write_text
from symbol_directory import US_EXCHANGES, classify_security_name, fetch_symbol_directory
from universe import default_asset_type_for, filter_universe, load_universe, sync_universe

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ R2-001


def _stub_download(monkeypatch, ok_symbols):
    """Return price data only for ``ok_symbols``; everything else is empty."""

    def fake(tickers, start, end, **kwargs):
        wanted = [t for t in tickers if t in ok_symbols]
        if not wanted:
            return pd.DataFrame()
        index = pd.DatetimeIndex(pd.bdate_range("2026-05-01", "2026-06-01"), name="Date")
        columns = pd.MultiIndex.from_product(
            [wanted, ["Open", "High", "Low", "Close", "Adj Close", "Volume"]],
            names=["Ticker", "Price"],
        )
        return pd.DataFrame(1.0, index=index, columns=columns)

    monkeypatch.setattr(fetch_prices.yf, "download", fake)


def _fetch_project(tmp_path, monkeypatch, tickers):
    (tmp_path / "config").mkdir(exist_ok=True)
    import yaml

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
    (tmp_path / "config" / "universe.csv").write_text("".join(f"{t}~{t} Inc\n" for t in tickers))
    monkeypatch.setattr(cfg_mod, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(cfg_mod, "DEFAULT_DATA_ROOT", str(tmp_path / "data"))


def test_partial_fetch_is_rejected(tmp_path, monkeypatch):
    """A screen missing a third of its universe is not a valid dataset."""
    tickers = [f"T{i:03d}" for i in range(100)]
    _fetch_project(tmp_path, monkeypatch, tickers)
    _stub_download(monkeypatch, set(tickers[:70]))

    fetcher = fetch_prices.YahooFinanceDataFetcher("US", "stocks", period=30)
    fetcher.fetch_historical_data()

    assert fetcher.success_ratio == pytest.approx(0.70)
    with pytest.raises(fetch_prices.PartialFetchError, match="70/100"):
        fetcher.assert_fetch_is_complete(min_ratio=0.95)


def test_near_complete_fetch_is_accepted(tmp_path, monkeypatch):
    tickers = [f"T{i:03d}" for i in range(100)]
    _fetch_project(tmp_path, monkeypatch, tickers)
    _stub_download(monkeypatch, set(tickers[:99]))

    fetcher = fetch_prices.YahooFinanceDataFetcher("US", "stocks", period=30)
    fetcher.fetch_historical_data()
    fetcher.assert_fetch_is_complete(min_ratio=0.95)  # must not raise


def test_allow_partial_overrides_the_gate(tmp_path, monkeypatch):
    tickers = [f"T{i:03d}" for i in range(100)]
    _fetch_project(tmp_path, monkeypatch, tickers)
    _stub_download(monkeypatch, set(tickers[:10]))

    fetcher = fetch_prices.YahooFinanceDataFetcher("US", "stocks", period=30)
    fetcher.fetch_historical_data()
    fetcher.assert_fetch_is_complete(min_ratio=0.95, allow_partial=True)


def test_failed_gate_leaves_the_previous_price_file_intact(tmp_path, monkeypatch):
    """The completeness check must run before the old dataset is overwritten."""
    tickers = [f"T{i:03d}" for i in range(100)]
    _fetch_project(tmp_path, monkeypatch, tickers)
    _stub_download(monkeypatch, set(tickers[:20]))

    fetcher = fetch_prices.YahooFinanceDataFetcher("US", "stocks", period=30)
    os.makedirs(fetcher.config.data_dir, exist_ok=True)
    with open(fetcher.config.eod_csv, "w") as handle:
        handle.write("KNOWN GOOD\n")

    fetcher.fetch_historical_data()
    with pytest.raises(fetch_prices.PartialFetchError):
        fetcher.assert_fetch_is_complete(min_ratio=0.95)

    assert open(fetcher.config.eod_csv).read() == "KNOWN GOOD\n"


# ------------------------------------------------------------------ R2-002


def test_directory_separates_spac_share_classes():
    """The whole point: same company name, three different securities."""
    cases = [
        ("Artius II Acquisition Inc. - Class A Ordinary Shares", "common_stock"),
        ("Artius II Acquisition Inc. - Rights", "right"),
        ("Artius II Acquisition Inc. - Units", "unit"),
        ("Artius II Acquisition Inc. - Warrant", "warrant"),
    ]
    for name, expected in cases:
        assert classify_security_name(name) == expected, name


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Alphabet Inc. - Class C Capital Stock", "common_stock"),
        ("Apple Inc. - Common Stock", "common_stock"),
        ("Gladstone Commercial Corporation - Real Estate Investment Trust", "common_stock"),
        (
            "Alliance Resource Partners, L.P. - Common Units Representing LP Interests",
            "common_stock",
        ),
        ("America Movil - American Depositary Shares", "common_stock"),
        # A warrant over depositary shares is still a warrant.
        ("IQM - Warrants to purchase American Depositary Shares", "warrant"),
        ("Ares Capital Corporation - Closed End Fund", "etf"),
        ("Amplitech Group, Inc. - Series B Right", "right"),
        ("Wheeler REIT 7.00% Series D Cumulative Preferred", "preferred"),
    ],
)
def test_directory_classification(name, expected):
    assert classify_security_name(name) == expected


def test_etf_flag_is_authoritative():
    assert classify_security_name("Some Fund Common Stock", is_etf=True) == "etf"


def test_unclassifiable_name_is_unknown_not_common_stock():
    """Fail closed: an unrecognised class must not default into the screen."""
    assert classify_security_name("Mystery Holdings Ltd") == "unknown"
    df = pd.DataFrame(
        {
            "ticker": ["X"],
            "name": ["Mystery"],
            "exchange": ["NYSE"],
            "asset_type": ["unknown"],
            "currency": [""],
            "source_date": [""],
        }
    )
    assert filter_universe(df, ["common_stock"]).empty


def test_directory_parsing(tmp_path):
    nasdaq = (
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
        "Round Lot Size|ETF|NextShares\n"
        "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\n"
        "AACIW|Armada Acquisition Corp. III - Warrant|S|N|N|100|N|N\n"
        "TESTX|Test Issue - Common Stock|Q|Y|N|100|N|N\n"
        "File Creation Time: 0821202621:31|||||||\n"
    )
    other = (
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|"
        "Test Issue|NASDAQ Symbol\n"
        "AA|Alcoa Corporation Common Stock|N|AA|N|100|N|AA\n"
        "SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY\n"
        "File Creation Time: 0821202621:31|||||||\n"
    )
    d = fetch_symbol_directory(nasdaq_text=nasdaq, other_text=other)
    by_ticker = dict(zip(d["ticker"], d["asset_type"], strict=True))

    assert by_ticker["AAPL"] == "common_stock"
    assert by_ticker["AACIW"] == "warrant"
    assert by_ticker["SPY"] == "etf"
    assert "TESTX" not in by_ticker, "test issues are not tradeable"
    assert dict(zip(d["ticker"], d["exchange"], strict=True))["AA"] == "NYSE"


# ------------------------------------------------------------------ R2-003


def test_temp_files_are_unique_per_call(tmp_path):
    """A fixed temp name lets concurrent runs clobber each other."""
    seen = set()
    real_replace = os.replace

    def capture(src, dst):
        seen.add(os.path.basename(src))
        real_replace(src, dst)

    import runmeta

    original = runmeta.os.replace
    runmeta.os.replace = capture
    try:
        for _ in range(5):
            atomic_write_text("x", str(tmp_path / "same.txt"))
            atomic_write_csv(pd.DataFrame({"a": [1]}), str(tmp_path / "same.csv"))
    finally:
        runmeta.os.replace = original

    assert len(seen) == 10, f"temp names collided: {seen}"


def test_windows_publish_only_after_all_compute(tmp_path, monkeypatch):
    """A failure part-way must not leave a mixed-generation output set."""
    windows = [
        {"months": 1, "label": "first", "threshold": 1.0},
        {"months": 3, "label": "second", "threshold": 1.0},
    ]
    cfg = build_project(tmp_path, monkeypatch, windows=windows)
    write_universe(cfg, [("AAA", "Alpha Inc")])
    write_prices(cfg, make_series("AAA", "Alpha Inc", "2026-03-01", "2026-06-02", 100, 200))

    calls = {"n": 0}
    real = analysis.compute_window_growth

    def explode(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom during second window")
        return real(*args, **kwargs)

    monkeypatch.setattr(analysis, "compute_window_growth", explode)
    with pytest.raises(RuntimeError, match="boom"):
        analysis.analyze_stocks(cfg, allow_stale=True)

    # Neither window may have been published.
    assert not os.path.exists(cfg.growth_csv("first"))
    assert not os.path.exists(cfg.growth_csv("second"))


# ------------------------------------------------------------------ R2-004


@pytest.mark.parametrize("exc_type", [ValueError, KeyError])
def test_value_and_key_errors_abort_the_run(exc_type):
    """Generic built-ins signal defects, not provider faults.

    Treating them as recoverable turned a response-shape change or a
    normalisation bug into "these tickers failed" and published a partial
    dataset that hid the defect.
    """
    assert exc_type not in fetch_prices.RECOVERABLE_ERRORS

    class Fetcher(fetch_prices.YahooFinanceDataFetcher):
        def __init__(self):
            self.batch_size = 2
            self.run_id = "t"

        def _fetch_batch(self, *a, **k):
            raise exc_type("schema defect")

    with pytest.raises(exc_type):
        Fetcher()._fetch_all(["A", "B"], {}, "2026-01-01", "2026-02-01", "now")


# ------------------------------------------------------------------ R2-005


def test_price_rows_and_manifest_carry_fetch_lineage(tmp_path, monkeypatch):
    tickers = ["AAA", "BBB"]
    _fetch_project(tmp_path, monkeypatch, tickers)
    _stub_download(monkeypatch, set(tickers))

    fetcher = fetch_prices.YahooFinanceDataFetcher("US", "stocks", period=30)
    data = fetcher.fetch_historical_data()
    assert (data["fetch_run_id"] == fetcher.run_id).all()

    path = fetcher.write_manifest(status="success", eod_path=fetcher.config.eod_csv)
    manifest = json.load(open(path))
    assert manifest["stage"] == "fetch"
    assert manifest["counts"]["requested"] == 2
    assert manifest["counts"]["success_ratio"] == 1.0


def test_analysis_links_back_to_the_fetch_run(tmp_path, monkeypatch):
    cfg = build_project(tmp_path, monkeypatch)
    write_universe(cfg, [("AAA", "Alpha Inc")])
    write_prices(cfg, make_series("AAA", "Alpha Inc", "2026-05-01", "2026-06-02", 100, 150))
    atomic_write_text(
        json.dumps({"run_id": "fetch-123", "status": "success"}),
        os.path.join(cfg.data_dir, f"{cfg.prefix}_fetch_manifest.json"),
    )

    manifest = analysis.analyze_stocks(cfg, allow_stale=True)
    assert manifest.source_run_id == "fetch-123"
    assert manifest.source_status == "success"

    # Assert the *persisted* manifest, not just the in-memory object: an
    # undeclared dataclass attribute can be set and still be dropped by
    # asdict() on write, which is exactly what happened.
    written = json.load(open(os.path.join(cfg.data_dir, f"{cfg.prefix}_analysis_manifest.json")))
    assert written["source_run_id"] == "fetch-123"
    assert written["source_status"] == "success"


# ------------------------------------------------------------------ R2-006


def test_mixed_price_basis_degrades_to_the_worst(build_frame, latest_date):
    """Lexical min returned 'adjusted' for a window containing raw rows."""
    assert _worst_price_basis(["adjusted", "raw_fallback"]) == "raw_fallback"
    assert _worst_price_basis(["adjusted", "unknown"]) == "unknown"
    assert _worst_price_basis(["adjusted"]) == "adjusted"

    series = make_series("MIX", "Mixed", "2025-06-02", "2026-06-02", 100, 200)
    middle = len(series) // 2
    series.loc[series.index[middle], "price_basis"] = "raw_fallback"

    settings = AnalysisSettings(
        min_price=0.0,
        min_median_volume=0.0,
        min_coverage=0.8,
        endpoint_window=1,
        min_observation_ratio=0.0,
    )
    result, funnel = compute_window_growth(
        build_frame(series), 12, 25.0, settings, latest_date, "US"
    )
    assert result.empty
    assert dict(funnel)["Adjusted prices"] == 0


# ------------------------------------------------------------------ R2-007


def test_sync_reports_membership_changes(tmp_path):
    path = tmp_path / "u.csv"
    path.write_text("OLD~Old Corp\nKEEP~Keep Corp\n")
    directory = pd.DataFrame(
        {
            "ticker": ["KEEP", "NEW"],
            "name": ["Keep Corp - Common Stock", "New Corp - Common Stock"],
            "exchange": ["NYSE", "NASDAQ"],
            "asset_type": ["common_stock", "common_stock"],
        }
    )
    summary = sync_universe(str(path), directory)

    assert summary["added"] == ["NEW"]
    assert summary["removed"] == ["OLD"]
    assert summary["retained"] == 1
    assert set(load_universe(str(path))["ticker"]) == {"KEEP", "NEW"}


def test_sync_refuses_to_wipe_the_universe(tmp_path):
    path = tmp_path / "u.csv"
    path.write_text("KEEP~Keep Corp\n")
    with pytest.raises(ValueError, match="empty"):
        sync_universe(str(path), pd.DataFrame(columns=["ticker", "name", "exchange", "asset_type"]))
    assert load_universe(str(path))["ticker"].tolist() == ["KEEP"]


def test_us_exchange_groups_are_defined():
    assert "NASDAQ" in US_EXCHANGES["US"]
    assert "NYSE" in US_EXCHANGES["US"]


# ------------------------------------------------------------------ R2-008


def test_growth_schema_is_declared_and_typed():
    sql = growth_schema_sql()
    assert '"pct_change" FLOAT' in sql
    assert '"observations" INTEGER' in sql
    assert '"ticker" TEXT' in sql
    # Every declared column must exist in the analysis output contract.
    declared = set(re.findall(r'"([a-z_]+)"', sql))
    assert declared == set(analysis.GROWTH_COLUMNS)


# ------------------------------------------------------------------ R2-009


def test_fetch_boundary_uses_exchange_local_date(tmp_path, monkeypatch):
    """A host a day ahead of the exchange must not request a future session."""
    _fetch_project(tmp_path, monkeypatch, ["AAA"])
    captured = {}

    def fake(tickers, start, end, **kwargs):
        captured["end"] = end
        return pd.DataFrame()

    monkeypatch.setattr(fetch_prices.yf, "download", fake)
    fetcher = fetch_prices.YahooFinanceDataFetcher("US", "stocks", period=30)
    fetcher.fetch_historical_data()

    ny_today = pd.Timestamp.now(tz="America/New_York").normalize().tz_localize(None)
    assert pd.Timestamp(captured["end"]) == ny_today + pd.Timedelta(days=1)


def test_every_supported_exchange_has_a_timezone():
    from config import EXCHANGE_SUFFIXES

    missing = set(EXCHANGE_SUFFIXES) - set(fetch_prices.EXCHANGE_TIMEZONES)
    assert not missing, f"no session timezone for {missing}"


# ------------------------------------------------------------------ R2-012


def test_legacy_etf_universe_is_not_emptied(tmp_path, monkeypatch):
    """A legacy ETF file loaded with the stocks default screened to zero."""
    path = tmp_path / "etf.csv"
    path.write_text("VAS~Vanguard Australian Shares\nIOZ~iShares Core ASX 200\n")

    wrong = load_universe(str(path))
    assert filter_universe(wrong, ["etf"]).empty, "reproduces the defect"

    right = load_universe(str(path), default_asset_type=default_asset_type_for("etf"))
    assert len(filter_universe(right, ["etf"])) == 2


def test_default_asset_type_for():
    assert default_asset_type_for("etf") == "etf"
    assert default_asset_type_for("stocks") == "common_stock"


def test_shipped_etf_config_screens_a_universe():
    """End-to-end guard: the ASX config must not screen to zero instruments."""
    cfg = load_config("ASX", "etf")
    universe = load_universe(
        cfg.ticker_file, default_asset_type=default_asset_type_for(cfg.instrument_type)
    )
    assert len(filter_universe(universe, cfg.analysis.asset_types)) > 100


# ------------------------------------------------------------------ universe naming


def test_us_universe_is_the_shipped_one():
    """The shipped US config must load and screen a real universe."""
    cfg = load_config("US", "stocks")
    assert cfg.ticker_file.endswith("us_stocks.csv")
    assert cfg.prefix == "us_stocks"

    universe = load_universe(
        cfg.ticker_file, default_asset_type=default_asset_type_for(cfg.instrument_type)
    )
    screened = filter_universe(universe, cfg.analysis.asset_types)
    assert len(screened) > 1000
    # A US universe spans venues; that is the reason it is not named NASDAQ.
    assert len(set(screened["exchange"])) > 1


def test_every_classifier_output_is_a_valid_asset_type():
    """Guards the documented type list against drift in the classifier."""
    from config import VALID_ASSET_TYPES

    names = [
        "Apple Inc. - Common Stock",
        "Armada Acquisition Corp. III - Warrant",
        "Armada Acquisition Corp. III - Units",
        "Apogee Acquisition Corp - Rights",
        "Wheeler REIT 7.00% Series D Cumulative Preferred",
        "Acme Corp - 5.5% Notes due 2030",
        "Ares Capital Corporation - Closed End Fund",
        "Mystery Holdings Ltd",
    ]
    produced = {classify_security_name(n) for n in names}
    assert produced <= VALID_ASSET_TYPES, produced - VALID_ASSET_TYPES
    # Every non-equity class the README documents must be reachable.
    assert {"warrant", "unit", "right", "preferred", "note", "unknown"} <= produced


def test_documented_asset_types_match_the_config_whitelist():
    """The README table and VALID_ASSET_TYPES must not diverge."""
    import re as _re

    from config import VALID_ASSET_TYPES

    readme = open(os.path.join(PROJECT_ROOT, "README.md")).read()
    section = readme.split("### Instrument types", 1)[1].split("###", 1)[0]
    documented = set(_re.findall(r"^\| `([a-z_]+)` \|", section, _re.M))
    documented.discard("asset_type")  # the table's header cell
    assert documented == VALID_ASSET_TYPES, (
        f"README documents {sorted(documented)}, code allows {sorted(VALID_ASSET_TYPES)}"
    )


# ------------------------------------------------------------------ documentation


def test_readme_recipes_use_valid_exchange_codes():
    """Every --exchange / CLI code shown in the README must be real."""
    import re as _re

    from config import EXCHANGE_SUFFIXES, INSTRUMENTS

    readme = open(os.path.join(PROJECT_ROOT, "README.md")).read()
    invocations = _re.findall(
        r"(?:fetch_prices\.sh|run_analysis\.sh|universe\.py (?:sync|enrich)) "
        r"([A-Z]+) ([a-z]+)",
        readme,
    )
    assert invocations, "no example invocations found in the README"
    for exchange, instrument in invocations:
        assert exchange in EXCHANGE_SUFFIXES, f"README uses unknown exchange {exchange}"
        assert instrument in INSTRUMENTS, f"README uses unknown instrument {instrument}"


def test_readme_asset_type_examples_are_valid():
    """asset_types values shown in the README must be accepted by config."""
    import re as _re

    from config import VALID_ASSET_TYPES

    readme = open(os.path.join(PROJECT_ROOT, "README.md")).read()
    for block in _re.findall(r"asset_types: \[([^\]]+)\]", readme):
        for value in (v.strip() for v in block.split(",")):
            assert value in VALID_ASSET_TYPES, f"README shows invalid asset_type {value!r}"


def test_asx_stocks_is_absent_and_fails_clearly():
    """ASX common stock is deliberately out of scope.

    The failure must stay clean and explicit, so nobody gets a silently
    wrong result from asking for a universe that does not exist.
    """
    with pytest.raises(FileNotFoundError, match="asx_stocks_config.yaml"):
        load_config("ASX", "stocks")


# ------------------------------------------------------------------ universe pruning


def _stub_provider(monkeypatch, resolvable, rate_limited=()):
    """Stub yfinance metadata lookups for refresh_universe."""
    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError

    class Handle:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def history_metadata(self):
            if self.symbol in rate_limited:
                # YFRateLimitError takes no constructor arguments.
                raise YFRateLimitError()
            if self.symbol in resolvable:
                return {"fullExchangeName": "ASX", "instrumentType": "ETF"}
            raise Exception("no data")

    monkeypatch.setattr(yf, "Ticker", Handle)
    # The retry path sleeps between attempts; tests must not wait for it.
    monkeypatch.setattr("universe.time.sleep", lambda _seconds: None)


def test_prune_removes_delisted_instruments(tmp_path, monkeypatch):
    """Dead tickers drag every later fetch below the publication threshold.

    Proportions mirror the real ASX case: a small minority of the universe is
    delisted, well under the mass-deletion safety limit.
    """
    from universe import refresh_universe

    alive = [f"A{i}" for i in range(18)]
    dead = ["DEAD1", "DEAD2"]
    path = tmp_path / "u.csv"
    path.write_text("".join(f"{t}~{t} Fund\n" for t in alive + dead))
    _stub_provider(monkeypatch, resolvable=set(alive))

    df = refresh_universe(str(path), "", default_asset_type="etf", prune=True)
    assert set(df["ticker"]) == set(alive)
    assert set(load_universe(str(path))["ticker"]) == set(alive)


def test_prune_is_skipped_when_lookups_were_throttled(tmp_path, monkeypatch):
    """A throttled lookup is indistinguishable from a delisting."""
    from universe import refresh_universe

    path = tmp_path / "u.csv"
    alive = [f"A{i}" for i in range(18)]
    path.write_text("".join(f"{t}~{t} Fund\n" for t in [*alive, "MAYBE"]))
    _stub_provider(monkeypatch, resolvable=set(alive), rate_limited={"MAYBE"})

    df = refresh_universe(str(path), "", default_asset_type="etf", prune=True)
    assert "MAYBE" in set(df["ticker"]), "must not prune while throttled"


def test_prune_refuses_a_mass_deletion(tmp_path, monkeypatch):
    """A provider outage must not empty the universe."""
    from universe import refresh_universe

    path = tmp_path / "u.csv"
    path.write_text("".join(f"T{i}~Fund {i}\n" for i in range(10)))
    _stub_provider(monkeypatch, resolvable={"T0"})  # 9 of 10 look dead

    df = refresh_universe(str(path), "", default_asset_type="etf", prune=True)
    assert len(df) == 10, "above the safety limit, nothing may be pruned"


def test_enrich_without_prune_keeps_everything(tmp_path, monkeypatch):
    from universe import refresh_universe

    path = tmp_path / "u.csv"
    path.write_text("ALIVE~Alive Fund\nDEAD~Dead Fund\n")
    _stub_provider(monkeypatch, resolvable={"ALIVE"})

    df = refresh_universe(str(path), "", default_asset_type="etf")
    assert set(df["ticker"]) == {"ALIVE", "DEAD"}
