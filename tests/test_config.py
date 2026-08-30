import os

import pytest
import yaml

import config as cfg_mod
from config import load_config


def write_config(tmp_path, monkeypatch, body, name="us_stocks_config.yaml"):
    """Point the loader at a throwaway config directory."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / name).write_text(yaml.safe_dump(body))
    monkeypatch.setattr(cfg_mod, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(cfg_mod, "DEFAULT_DATA_ROOT", str(tmp_path / "data"))
    return config_dir


BASE = {
    "config": {
        "ticker_file": "config/us_stocks.csv",
        "data_dir": "us/stocks",
        "db_path": "us.db",
    }
}


def test_relative_paths_resolve_under_project_root(tmp_path, monkeypatch):
    monkeypatch.delenv("STOCKS_DATA_ROOT", raising=False)
    write_config(tmp_path, monkeypatch, BASE)
    cfg = load_config("US", "stocks")

    assert cfg.bundled_ticker_file == str(tmp_path / "config" / "us_stocks.csv")
    # The live universe sits on the data volume, not in the repo.
    assert cfg.ticker_file == str(tmp_path / "data" / "universe" / "us_stocks.csv")
    assert cfg.data_dir == str(tmp_path / "data" / "us" / "stocks")
    assert cfg.db_path == str(tmp_path / "data" / "us.db")


def test_data_root_env_relocates_outputs_but_not_inputs(tmp_path, monkeypatch):
    """The regression that produced data/data/us.db paths."""
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("STOCKS_DATA_ROOT", str(elsewhere))
    write_config(tmp_path, monkeypatch, BASE)
    cfg = load_config("US", "stocks")

    assert cfg.data_dir == str(elsewhere / "us" / "stocks")
    assert cfg.db_path == str(elsewhere / "us.db")
    # The committed seed stays in the repo; the live copy follows the data root.
    assert cfg.bundled_ticker_file == str(tmp_path / "config" / "us_stocks.csv")
    assert cfg.ticker_file == str(elsewhere / "universe" / "us_stocks.csv")


def test_absolute_paths_are_left_alone(tmp_path, monkeypatch):
    body = {"config": dict(BASE["config"], db_path="/var/tmp/explicit.db")}
    write_config(tmp_path, monkeypatch, body)
    assert load_config("US", "stocks").db_path == "/var/tmp/explicit.db"


def test_unsupported_exchange_rejected():
    with pytest.raises(ValueError, match="Unsupported exchange"):
        load_config("LSE", "stocks")


def test_unsupported_instrument_rejected():
    with pytest.raises(ValueError, match="Unsupported instrument"):
        load_config("NASDAQ", "bonds")


def test_missing_ticker_file_key_names_the_key(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, {"config": {"data_dir": "x"}})
    with pytest.raises(KeyError, match="ticker_file"):
        load_config("US", "stocks")


def test_malformed_window_is_rejected(tmp_path, monkeypatch):
    body = {
        "config": dict(BASE["config"], analysis={"windows": [{"months": 12, "label": "1_year"}]})
    }
    write_config(tmp_path, monkeypatch, body)
    with pytest.raises(KeyError, match="threshold"):
        load_config("US", "stocks")


def test_shipped_configs_all_load():
    """The checked-in configs must stay valid."""
    for exchange, instrument in [("US", "stocks"), ("ASX", "etf"), ("NSE", "stocks")]:
        cfg = load_config(exchange, instrument)
        assert os.path.isabs(cfg.data_dir)
        assert cfg.growth_labels
        assert cfg.analysis.min_coverage > 0
        assert os.path.exists(cfg.bundled_ticker_file), cfg.bundled_ticker_file


def test_every_shipped_universe_loads_and_screens_something():
    """A config whose universe filters to nothing would fail only at run time."""
    from universe import default_asset_type_for, filter_categories, filter_universe, load_universe

    for exchange, instrument in [("US", "stocks"), ("ASX", "etf"), ("NSE", "stocks")]:
        cfg = load_config(exchange, instrument)
        universe = load_universe(
            cfg.bundled_ticker_file,
            default_asset_type=default_asset_type_for(cfg.instrument_type),
        )
        screened = filter_categories(
            filter_universe(universe, cfg.analysis.asset_types),
            cfg.analysis.exclude_categories,
        )
        assert not screened.empty, f"{exchange} {instrument} screens no instruments"


def test_price_history_sampling_defaults_to_weekly(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, BASE)
    assert load_config("US", "stocks").analysis.price_history_sampling == "weekly"


def test_unknown_price_history_sampling_rejected(tmp_path, monkeypatch):
    """A typo must fail at load, not silently publish 5x the rows."""
    body = {"config": dict(BASE["config"], analysis={"price_history_sampling": "fortnightly"})}
    write_config(tmp_path, monkeypatch, body)
    with pytest.raises(ValueError, match="price_history_sampling must be one of"):
        load_config("US", "stocks")


def test_leveraged_is_excluded_from_screens_by_default(tmp_path, monkeypatch):
    """Opt-out, not opt-in.

    A geared fund returns a multiple of its underlying, so it leads a growth
    ranking by construction; leaving the exclusion off by default would mean
    every shipped config had to remember to ask for it.
    """
    write_config(tmp_path, monkeypatch, BASE)
    assert load_config("US", "stocks").analysis.exclude_categories == ["leveraged"]


def test_exclude_categories_can_be_emptied(tmp_path, monkeypatch):
    body = {"config": {**BASE["config"], "analysis": {"exclude_categories": []}}}
    write_config(tmp_path, monkeypatch, body)
    assert load_config("US", "stocks").analysis.exclude_categories == []


def test_a_bare_string_exclude_category_is_rejected(tmp_path, monkeypatch):
    """ "leveraged" is iterable, so list() would make nine useless categories."""
    body = {"config": {**BASE["config"], "analysis": {"exclude_categories": "leveraged"}}}
    write_config(tmp_path, monkeypatch, body)
    with pytest.raises(ValueError, match="exclude_categories must be a list"):
        load_config("US", "stocks")


def test_code_revision_prefers_the_baked_in_value(monkeypatch):
    """Containers exclude .git, so git cannot answer; the image bakes it in."""
    import runmeta

    monkeypatch.setenv(runmeta.CODE_REVISION_ENV, "cafe123")
    assert runmeta.code_revision("/nonexistent") == "cafe123"


def test_code_revision_falls_back_to_git(monkeypatch):
    import runmeta

    monkeypatch.delenv(runmeta.CODE_REVISION_ENV, raising=False)
    # No repository at this path, and no baked value: must not raise.
    assert runmeta.code_revision("/nonexistent") == "unknown"


def test_ensure_universe_overwrites_a_stale_working_copy(tmp_path, monkeypatch):
    """The committed CSV is the only source of truth for what is screened.

    A working copy on a persistent volume is a leftover, not a record. If it
    outranked the file in the image, a ticker removed from the repository
    would keep being screened forever -- and nothing at runtime is allowed to
    decide membership.
    """
    monkeypatch.delenv("STOCKS_DATA_ROOT", raising=False)
    write_config(tmp_path, monkeypatch, BASE)
    cfg = load_config("US", "stocks")

    os.makedirs(os.path.dirname(cfg.bundled_ticker_file), exist_ok=True)
    with open(cfg.bundled_ticker_file, "w") as handle:
        handle.write("ticker,name\nKEEP,Keep Inc\n")

    os.makedirs(cfg.universe_dir, exist_ok=True)
    with open(cfg.ticker_file, "w") as handle:
        handle.write("ticker,name\nSTALE,Left Over Inc\n")

    cfg.ensure_universe()

    with open(cfg.ticker_file) as handle:
        refreshed = handle.read()
    assert "KEEP" in refreshed
    assert "STALE" not in refreshed
