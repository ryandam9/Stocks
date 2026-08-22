"""Configuration loading and path resolution.

Single source of truth for config for both the Python entry points and the
shell wrappers.  ``scripts/*.sh`` query this module rather than parsing YAML
with grep/awk, so there is only one place that knows the config layout.

Paths in the YAML are relative to the project root unless they are absolute.
Set ``STOCKS_DATA_ROOT`` to relocate all generated data (CSVs and SQLite DBs)
outside the repository without editing any config file.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List

import yaml

# Maps exchange codes to the suffix Yahoo Finance expects on a ticker symbol.
# e.g. NSE ticker "RELIANCE" is requested as "RELIANCE.NS".
EXCHANGE_SUFFIXES = {
    "NSE": "NS",  # National Stock Exchange (India)
    "BSE": "BO",  # Bombay Stock Exchange
    "NYSE": "",  # New York Stock Exchange
    "NASDAQ": "",  # NASDAQ
    "ASX": "AX",  # Australian Securities Exchange
}

INSTRUMENTS = ["stocks", "etf"]

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Env var that relocates every generated artefact. data_dir/db_path are
# resolved against this root, which defaults to <project_root>/data.
DATA_ROOT_ENV = "STOCKS_DATA_ROOT"
DEFAULT_DATA_ROOT = os.path.join(PROJECT_ROOT, "data")

# Used when a config file omits the analysis block entirely.
DEFAULT_WINDOWS = [
    {"months": 12, "label": "1_year", "threshold": 25.0},
    {"months": 6, "label": "6_months", "threshold": 25.0},
    {"months": 3, "label": "3_months", "threshold": 25.0},
    {"months": 1, "label": "1_month", "threshold": 10.0},
]


@dataclass
class AnalysisSettings:
    """Thresholds controlling which tickers qualify as "growth"."""

    # Minimum latest price. Guards against percentage moves in sub-dollar
    # names being treated as meaningful. Denominated in the exchange's own
    # currency, so it is per-config rather than a global constant.
    min_price: float = 10.0
    # Minimum median daily volume over the window; illiquid tickers produce
    # percentages that cannot actually be traded.
    min_median_volume: float = 50_000.0
    # Fraction of the requested window a ticker must actually span to be
    # reported for it. Without this a stock listed 2 weeks ago shows up in
    # the 1-year table with its 2-week return.
    min_coverage: float = 0.8
    # Number of trading days median-averaged at each endpoint. 1 restores the
    # old single-day behaviour; 3 removes most single-print noise.
    endpoint_window: int = 3
    windows: List[Dict] = field(default_factory=lambda: list(DEFAULT_WINDOWS))


@dataclass
class StockConfig:
    """Fully resolved configuration for one (exchange, instrument) pair."""

    exchange: str
    instrument_type: str
    ticker_file: str
    data_dir: str
    db_path: str
    analysis: AnalysisSettings

    @property
    def prefix(self) -> str:
        """Filename stem shared by every artefact for this pair."""
        return f"{self.exchange.lower()}_{self.instrument_type.lower()}"

    @property
    def eod_csv(self) -> str:
        return os.path.join(self.data_dir, f"{self.prefix}_eod.csv")

    @property
    def error_csv(self) -> str:
        return os.path.join(self.data_dir, f"{self.prefix}_error.csv")

    @property
    def growth_labels(self) -> List[str]:
        """Window labels in config order, e.g. ["1_year", "6_months", ...]."""
        return [str(w["label"]) for w in self.analysis.windows]

    @property
    def combined_growth_csv(self) -> str:
        """Price history for every ticker that grew in any window."""
        return self._growth_path("_growth")

    def growth_csv(self, label: str) -> str:
        """Per-window growth summary, e.g. ``nasdaq_stocks_eod_growth_1_year.csv``."""
        return self._growth_path(f"_growth_{label}")

    def _growth_path(self, suffix: str) -> str:
        stem, extension = os.path.splitext(self.eod_csv)
        return f"{stem}{suffix}{extension}"


def _resolve(path: str, base: str) -> str:
    """Expand ``~``/env vars and anchor a relative path to ``base``."""
    path = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    return os.path.abspath(path)


def config_path(exchange: str, instrument_type: str) -> str:
    filename = f"{exchange.lower()}_{instrument_type.lower()}_config.yaml"
    return os.path.join(PROJECT_ROOT, "config", filename)


def load_config(exchange: str, instrument_type: str) -> StockConfig:
    """Load and validate the config for an (exchange, instrument) pair.

    Raises:
        ValueError: unsupported exchange or instrument type.
        FileNotFoundError: no config file for the pair.
        KeyError: a required key is missing from the config file.
    """
    if exchange.upper() not in EXCHANGE_SUFFIXES:
        raise ValueError(
            f"Unsupported exchange: {exchange}. "
            f"Supported exchanges: {', '.join(EXCHANGE_SUFFIXES)}"
        )
    if instrument_type.lower() not in INSTRUMENTS:
        raise ValueError(
            f"Unsupported instrument type: {instrument_type}. "
            f"Supported types: {', '.join(INSTRUMENTS)}"
        )

    path = config_path(exchange, instrument_type)
    if not os.path.exists(path):
        raise FileNotFoundError(f"No config file for {exchange}/{instrument_type}: {path}")

    with open(path, "r") as handle:
        raw = yaml.safe_load(handle) or {}

    try:
        section = raw["config"]
    except KeyError:
        raise KeyError(f"{path} is missing the top-level 'config' key")

    try:
        ticker_file = section["ticker_file"]
    except KeyError:
        raise KeyError(f"{path} is missing required key 'config.ticker_file'")

    # Generated artefacts live under STOCKS_DATA_ROOT when it is set, so the
    # checked-in config stays portable across machines. data_dir/db_path in the
    # YAML are relative to this root, never to the project root.
    data_root = os.environ.get(DATA_ROOT_ENV)
    data_base = _resolve(data_root, PROJECT_ROOT) if data_root else DEFAULT_DATA_ROOT

    analysis_raw = section.get("analysis") or {}
    windows = analysis_raw.get("windows") or DEFAULT_WINDOWS
    for window in windows:
        missing = {"months", "label", "threshold"} - set(window)
        if missing:
            raise KeyError(
                f"{path}: analysis window {window} is missing key(s): "
                f"{', '.join(sorted(missing))}"
            )

    analysis = AnalysisSettings(
        min_price=float(analysis_raw.get("min_price", 10.0)),
        min_median_volume=float(analysis_raw.get("min_median_volume", 50_000.0)),
        min_coverage=float(analysis_raw.get("min_coverage", 0.8)),
        endpoint_window=int(analysis_raw.get("endpoint_window", 3)),
        windows=list(windows),
    )

    return StockConfig(
        exchange=exchange.upper(),
        instrument_type=instrument_type.lower(),
        # Ticker lists are inputs tracked in the repo, so they anchor to the
        # project root even when data has been relocated.
        ticker_file=_resolve(ticker_file, PROJECT_ROOT),
        data_dir=_resolve(section.get("data_dir", "."), data_base),
        db_path=_resolve(section.get("db_path", "stocks.db"), data_base),
        analysis=analysis,
    )


def _main() -> None:
    """Print one resolved config value.

    Lets the shell wrappers read config without reimplementing YAML parsing:
        python src/config.py NASDAQ stocks data_dir
    """
    import sys

    if len(sys.argv) != 4:
        print(
            "Usage: config.py <EXCHANGE> <INSTRUMENT_TYPE> <KEY>\n"
            "  KEY: ticker_file | data_dir | db_path | eod_csv |\n"
            "       combined_growth_csv | growth_labels | prefix",
            file=sys.stderr,
        )
        sys.exit(2)

    exchange, instrument_type, key = sys.argv[1:4]
    try:
        cfg = load_config(exchange, instrument_type)
    except Exception as exc:  # surfaced verbatim to the shell caller
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not hasattr(cfg, key) or key.startswith("_"):
        print(f"Error: unknown config key '{key}'", file=sys.stderr)
        sys.exit(1)

    value = getattr(cfg, key)
    # Lists print one per line so the shell can read them with a while loop.
    if isinstance(value, list):
        print("\n".join(str(item) for item in value))
    else:
        print(value)


if __name__ == "__main__":
    _main()
