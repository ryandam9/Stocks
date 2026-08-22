"""Configuration loading and path resolution.

Single source of truth for config for both the Python entry points and the
shell wrappers.  ``scripts/*.sh`` query this module rather than parsing YAML
with grep/awk, so there is only one place that knows the config layout.

Paths in the YAML are relative to the project root unless they are absolute.
Set ``STOCKS_DATA_ROOT`` to relocate all generated data (CSVs and SQLite DBs)
outside the repository without editing any config file.
"""

import math
import os
import re
from dataclasses import dataclass, field

import yaml

# Maps exchange codes to the suffix Yahoo Finance expects on a ticker symbol.
# e.g. NSE ticker "RELIANCE" is requested as "RELIANCE.NS".
EXCHANGE_SUFFIXES = {
    "NSE": "NS",  # National Stock Exchange (India)
    "BSE": "BO",  # Bombay Stock Exchange
    # "US" is the whole US listed universe: Nasdaq, NYSE, NYSE American,
    # NYSE Arca, Cboe BZX and IEX. The exchange symbol directory this project
    # syncs from covers all of them, and links are built from each ticker's
    # own venue, so a single US universe is more honest than pretending the
    # list is Nasdaq-only. NYSE and NASDAQ remain available for venue-specific
    # universes; no config is shipped for them.
    "US": "",
    "NYSE": "",  # New York Stock Exchange
    "NASDAQ": "",  # Nasdaq
    "ASX": "AX",  # Australian Securities Exchange
}

INSTRUMENTS = ["stocks", "etf"]

# Declared schema of every per-window growth table. Kept here so the analysis
# stage and the SQLite loader agree. FLOAT rather than REAL because that is the
# spelling sqlite-utils emits, so an empty run and a populated run produce a
# byte-identical schema (both have REAL affinity either way): creating the table from CSV inference gave
# it FLOAT/INTEGER columns on a normal run and all-TEXT columns on an empty
# one, so the same logical table changed type between runs.
GROWTH_COLUMN_TYPES = [
    ("ticker", "TEXT"),
    ("name", "TEXT"),
    ("exchange", "TEXT"),
    ("asset_type", "TEXT"),
    ("first_date", "TEXT"),
    ("first_price", "FLOAT"),
    ("last_date", "TEXT"),
    ("latest_price", "FLOAT"),
    ("pct_change", "FLOAT"),
    ("observations", "INTEGER"),
    ("days_covered", "INTEGER"),
    ("coverage", "FLOAT"),
    ("observation_ratio", "FLOAT"),
    ("median_volume", "FLOAT"),
    ("price_basis", "TEXT"),
    ("data_as_of", "TEXT"),
    ("run_id", "TEXT"),
    ("google_finance", "TEXT"),
]

GROWTH_COLUMNS = [name for name, _ in GROWTH_COLUMN_TYPES]


def growth_schema_sql() -> str:
    """Column definitions for a growth table, for CREATE TABLE."""
    return ", ".join(f'"{name}" {sql_type}' for name, sql_type in GROWTH_COLUMN_TYPES)


# Instrument categories a config may ask to screen. "unknown" is accepted so a
# user can deliberately opt into instruments whose class could not be
# established, but it is never included by default.
VALID_ASSET_TYPES = {
    "common_stock",
    "etf",
    "warrant",
    "unit",
    "right",
    "preferred",
    "note",
    "unknown",
}

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Env var that relocates every generated artefact. data_dir/db_path are
# resolved against this root, which defaults to <project_root>/data.
DATA_ROOT_ENV = "STOCKS_DATA_ROOT"
DEFAULT_DATA_ROOT = os.path.join(PROJECT_ROOT, "data")

# Window labels become filename fragments and SQLite identifiers.
_SAFE_LABEL = re.compile(r"[A-Za-z0-9_]+")

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
    # Number of trading days median-averaged at each endpoint. 1 restores
    # single-day behaviour; 3 removes most single-print noise.
    endpoint_window: int = 3
    # Fraction of the window's *expected trading sessions* a ticker must
    # actually have. Distinct from min_coverage, which only checks that the
    # first and last observations span the window: a ticker with two prints a
    # year apart has full span coverage but no usable history.
    min_observation_ratio: float = 0.5
    # Reject the whole dataset if its newest row is older than this many days.
    # Guards against screening a stale CSV as though it were current.
    max_data_age_days: int = 5
    # Instrument categories to screen. Warrants, units and preferred lines are
    # excluded by default; they are not ordinary equity exposure.
    asset_types: list[str] = field(default_factory=lambda: ["common_stock", "etf"])
    windows: list[dict] = field(default_factory=lambda: list(DEFAULT_WINDOWS))


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
    def growth_labels(self) -> list[str]:
        """Window labels in config order, e.g. ["1_year", "6_months", ...]."""
        return [str(w["label"]) for w in self.analysis.windows]

    @property
    def combined_growth_csv(self) -> str:
        """Price history for every ticker that grew in any window."""
        return self._growth_path("_growth")

    def growth_csv(self, label: str) -> str:
        """Per-window growth summary, e.g. ``us_stocks_eod_growth_1_year.csv``."""
        return self._growth_path(f"_growth_{label}")

    def _growth_path(self, suffix: str) -> str:
        stem, extension = os.path.splitext(self.eod_csv)
        return f"{stem}{suffix}{extension}"


def _require_range(
    section: dict,
    key: str,
    low: float,
    high: float,
    path: str,
    exclusive_min: bool = False,
) -> None:
    """Reject a numeric setting that falls outside its meaningful range."""
    if key not in section:
        return
    value = float(section[key])
    if not math.isfinite(value):
        raise ValueError(f"{path}: {key} must be a finite number, got {section[key]}")
    too_low = value <= low if exclusive_min else value < low
    if too_low or value > high:
        bound = "(" if exclusive_min else "["
        raise ValueError(f"{path}: {key} must be in {bound}{low}, {high}], got {value}")


def _require_whole_number(raw, key: str, path: str) -> None:
    """Reject a fractional value where an integer is required.

    ``int(1.9)`` silently truncates to 1, changing behaviour with no indication
    that the configured value was not the one used.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{path}: {key} must be a number, got {raw!r}") from None
    if not math.isfinite(value) or value != int(value):
        raise ValueError(
            f"{path}: {key} must be a whole number, got {raw!r} "
            f"(a fractional value would be silently truncated)"
        )


def _require_positive(section: dict, key: str, path: str, integer: bool = False) -> None:
    if key not in section:
        return
    raw = section[key]
    if integer:
        _require_whole_number(raw, key, path)
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{path}: {key} must be a finite number, got {raw}")
    if value < 1:
        raise ValueError(f"{path}: {key} must be >= 1, got {raw}")


def _require_non_negative(section: dict, key: str, path: str) -> None:
    if key not in section:
        return
    value = float(section[key])
    if not math.isfinite(value):
        raise ValueError(f"{path}: {key} must be a finite number, got {section[key]}")
    if value < 0:
        raise ValueError(f"{path}: {key} must be >= 0, got {section[key]}")


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
            f"Unsupported exchange: {exchange}. Supported exchanges: {', '.join(EXCHANGE_SUFFIXES)}"
        )
    if instrument_type.lower() not in INSTRUMENTS:
        raise ValueError(
            f"Unsupported instrument type: {instrument_type}. "
            f"Supported types: {', '.join(INSTRUMENTS)}"
        )

    path = config_path(exchange, instrument_type)
    if not os.path.exists(path):
        raise FileNotFoundError(f"No config file for {exchange}/{instrument_type}: {path}")

    with open(path) as handle:
        raw = yaml.safe_load(handle) or {}

    try:
        section = raw["config"]
    except KeyError as exc:
        raise KeyError(f"{path} is missing the top-level 'config' key") from exc

    try:
        ticker_file = section["ticker_file"]
    except KeyError as exc:
        raise KeyError(f"{path} is missing required key 'config.ticker_file'") from exc

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
                f"{path}: analysis window {window} is missing key(s): {', '.join(sorted(missing))}"
            )

    _require_range(analysis_raw, "min_coverage", 0.0, 1.0, path, exclusive_min=True)
    _require_range(analysis_raw, "min_observation_ratio", 0.0, 1.0, path)
    _require_positive(analysis_raw, "endpoint_window", path, integer=True)
    _require_positive(analysis_raw, "max_data_age_days", path, integer=True)
    _require_non_negative(analysis_raw, "min_price", path)
    _require_non_negative(analysis_raw, "min_median_volume", path)

    if "asset_types" in analysis_raw:
        raw_types = analysis_raw["asset_types"]
        # A bare string is iterable, so list() would silently turn
        # "common_stock" into twelve single-character types and screen nothing.
        if isinstance(raw_types, str) or not isinstance(raw_types, (list, tuple)):
            raise ValueError(
                f"{path}: asset_types must be a list, got {raw_types!r}. "
                f"Write it as [{raw_types!r}] if you mean a single type."
            )
        unknown = {str(t) for t in raw_types} - VALID_ASSET_TYPES
        if unknown:
            raise ValueError(
                f"{path}: unknown asset_type(s): {', '.join(sorted(unknown))}. "
                f"Valid types: {', '.join(sorted(VALID_ASSET_TYPES))}"
            )
        if not raw_types:
            raise ValueError(f"{path}: asset_types must not be empty")

    for window in windows:
        _require_whole_number(window["months"], f"window '{window['label']}' months", path)

    labels = [str(w["label"]) for w in windows]
    duplicates = {label for label in labels if labels.count(label) > 1}
    if duplicates:
        raise ValueError(
            f"{path}: duplicate window label(s): {', '.join(sorted(duplicates))}. "
            "Labels name output files and SQLite tables, so they must be unique."
        )
    for label in labels:
        if not _SAFE_LABEL.fullmatch(label):
            raise ValueError(
                f"{path}: window label '{label}' must match [A-Za-z0-9_]+ so it is "
                "safe in filenames and SQLite identifiers"
            )
    for window in windows:
        if int(window["months"]) < 1:
            raise ValueError(f"{path}: window '{window['label']}' needs months >= 1")

    analysis = AnalysisSettings(
        min_price=float(analysis_raw.get("min_price", 10.0)),
        min_median_volume=float(analysis_raw.get("min_median_volume", 50_000.0)),
        min_coverage=float(analysis_raw.get("min_coverage", 0.8)),
        endpoint_window=int(analysis_raw.get("endpoint_window", 3)),
        min_observation_ratio=float(analysis_raw.get("min_observation_ratio", 0.5)),
        max_data_age_days=int(analysis_raw.get("max_data_age_days", 5)),
        asset_types=list(analysis_raw.get("asset_types", ["common_stock", "etf"])),
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
        python src/config.py US stocks data_dir
    """
    import sys

    if len(sys.argv) != 4:
        print(
            "Usage: config.py <EXCHANGE> <INSTRUMENT_TYPE> <KEY>\n"
            "  KEY: ticker_file | data_dir | db_path | eod_csv |\n"
            "       combined_growth_csv | growth_labels | prefix |\n"
            "       growth_schema_sql",
            file=sys.stderr,
        )
        sys.exit(2)

    exchange, instrument_type, key = sys.argv[1:4]
    try:
        cfg = load_config(exchange, instrument_type)
    except Exception as exc:  # surfaced verbatim to the shell caller
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if key == "growth_schema_sql":
        print(growth_schema_sql())
        return

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
