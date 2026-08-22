import os

import pytest
import yaml

import config as cfg_mod
from config import load_config


def write_config(tmp_path, monkeypatch, body, name="nasdaq_stocks_config.yaml"):
    """Point the loader at a throwaway config directory."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / name).write_text(yaml.safe_dump(body))
    monkeypatch.setattr(cfg_mod, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(cfg_mod, "DEFAULT_DATA_ROOT", str(tmp_path / "data"))
    return config_dir


BASE = {
    "config": {
        "ticker_file": "config/nasdaq_stocks.csv",
        "data_dir": "nasdaq/stocks",
        "db_path": "nasdaq.db",
    }
}


def test_relative_paths_resolve_under_project_root(tmp_path, monkeypatch):
    monkeypatch.delenv("STOCKS_DATA_ROOT", raising=False)
    write_config(tmp_path, monkeypatch, BASE)
    cfg = load_config("NASDAQ", "stocks")

    assert cfg.ticker_file == str(tmp_path / "config" / "nasdaq_stocks.csv")
    assert cfg.data_dir == str(tmp_path / "data" / "nasdaq" / "stocks")
    assert cfg.db_path == str(tmp_path / "data" / "nasdaq.db")


def test_data_root_env_relocates_outputs_but_not_inputs(tmp_path, monkeypatch):
    """The regression that produced data/data/nasdaq.db paths."""
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("STOCKS_DATA_ROOT", str(elsewhere))
    write_config(tmp_path, monkeypatch, BASE)
    cfg = load_config("NASDAQ", "stocks")

    assert cfg.data_dir == str(elsewhere / "nasdaq" / "stocks")
    assert cfg.db_path == str(elsewhere / "nasdaq.db")
    # Ticker lists are repo inputs and must not follow the data root.
    assert cfg.ticker_file == str(tmp_path / "config" / "nasdaq_stocks.csv")


def test_absolute_paths_are_left_alone(tmp_path, monkeypatch):
    body = {"config": dict(BASE["config"], db_path="/var/tmp/explicit.db")}
    write_config(tmp_path, monkeypatch, body)
    assert load_config("NASDAQ", "stocks").db_path == "/var/tmp/explicit.db"


def test_unsupported_exchange_rejected():
    with pytest.raises(ValueError, match="Unsupported exchange"):
        load_config("LSE", "stocks")


def test_unsupported_instrument_rejected():
    with pytest.raises(ValueError, match="Unsupported instrument"):
        load_config("NASDAQ", "bonds")


def test_missing_ticker_file_key_names_the_key(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, {"config": {"data_dir": "x"}})
    with pytest.raises(KeyError, match="ticker_file"):
        load_config("NASDAQ", "stocks")


def test_malformed_window_is_rejected(tmp_path, monkeypatch):
    body = {
        "config": dict(BASE["config"], analysis={"windows": [{"months": 12, "label": "1_year"}]})
    }
    write_config(tmp_path, monkeypatch, body)
    with pytest.raises(KeyError, match="threshold"):
        load_config("NASDAQ", "stocks")


def test_shipped_configs_all_load():
    """The checked-in configs must stay valid."""
    for exchange, instrument in [("NASDAQ", "stocks"), ("ASX", "etf")]:
        cfg = load_config(exchange, instrument)
        assert os.path.isabs(cfg.data_dir)
        assert cfg.growth_labels
        assert cfg.analysis.min_coverage > 0
