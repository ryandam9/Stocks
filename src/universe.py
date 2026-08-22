"""Instrument universe loading, classification and refresh.

A universe file lists the instruments to screen. Two formats are accepted:

* **Structured** (preferred) - a CSV with a header:
  ``ticker,name,exchange,asset_type,currency,source_date``
* **Legacy** - ``TICKER~Name`` per line, no header.

Legacy files carry no exchange or instrument type, so both are inferred:
``asset_type`` from the security's name, and ``exchange`` left unknown. An
unknown exchange is preserved as unknown rather than being replaced by the
config's exchange code, because that is what produced links labelling NYSE
securities as NASDAQ.

Run ``python src/universe.py refresh <EXCHANGE> <INSTRUMENT>`` to upgrade a
legacy file in place using the data provider's own metadata.
"""

import os
import re

import pandas as pd

UNIVERSE_COLUMNS = ["ticker", "name", "exchange", "asset_type", "currency", "source_date"]

# Instrument categories. Only common_stock and etf represent ordinary equity
# exposure; the rest are derivatives or hybrid securities that should not sit
# in a screen labelled "stocks".
COMMON_STOCK = "common_stock"
ETF = "etf"
WARRANT = "warrant"
UNIT = "unit"
PREFERRED = "preferred"
RIGHT = "right"
NOTE = "note"
UNKNOWN = "unknown"

EXCHANGE_UNKNOWN = "UNKNOWN"

# Name patterns for the derivative and hybrid classes only. These suffixes are
# unambiguous in Yahoo/NASDAQ security names, so they classify reliably with no
# network access.
#
# Deliberately absent: any fund-vs-operating-company rule. Words like "Trust"
# and "Fund" appear in the names of REITs and ordinary corporations (Arbor
# Realty Trust, American Assets Trust), so matching them misclassifies real
# equities as funds. The equity/fund distinction comes from the universe's
# declared type and the provider instead.
_NAME_PATTERNS = [
    (re.compile(r"\bwarrants?\b", re.I), WARRANT),
    (re.compile(r"\brights?\b", re.I), RIGHT),
    (re.compile(r"\bunits?\b", re.I), UNIT),
    (re.compile(r"\b(preferred|depositary\s+shares?|depositary\s+receipts?)\b", re.I), PREFERRED),
    (re.compile(r"\b(notes?|debentures?)\s+due\b", re.I), NOTE),
    (re.compile(r"%\s*(senior|subordinated|cumulative|convertible|notes)", re.I), NOTE),
]

# Provider instrumentType -> our asset_type.
_PROVIDER_TYPES = {
    "EQUITY": COMMON_STOCK,
    "ETF": ETF,
    "MUTUALFUND": ETF,
    "INDEX": ETF,
}

# Provider exchange names -> the code Google Finance expects.
_EXCHANGE_ALIASES = {
    "NasdaqGS": "NASDAQ",
    "NasdaqGM": "NASDAQ",
    "NasdaqCM": "NASDAQ",
    "NMS": "NASDAQ",
    "NCM": "NASDAQ",
    "NGM": "NASDAQ",
    "NasdaqAll": "NASDAQ",
    "NYSE": "NYSE",
    "NYQ": "NYSE",
    "New York Stock Exchange": "NYSE",
    "NYSEArca": "NYSEARCA",
    "PCX": "NYSEARCA",
    "ARCA": "NYSEARCA",
    "NYSEAmerican": "NYSEAMERICAN",
    "ASE": "NYSEAMERICAN",
    "AMEX": "NYSEAMERICAN",
    "ASX": "ASX",
    "ASX All": "ASX",
    "NSI": "NSE",
    "NSE": "NSE",
    "BSE": "BOM",
    "BOM": "BOM",
}


def normalise_exchange(raw: str | None) -> str:
    """Map a provider exchange label to a Google-Finance-compatible code."""
    if not raw or (isinstance(raw, float) and pd.isna(raw)):
        return EXCHANGE_UNKNOWN
    raw = str(raw).strip()
    if raw in _EXCHANGE_ALIASES:
        return _EXCHANGE_ALIASES[raw]
    upper = raw.upper()
    return _EXCHANGE_ALIASES.get(upper, upper or EXCHANGE_UNKNOWN)


def infer_asset_type(name: str, default: str = COMMON_STOCK) -> str:
    """Classify a security from its name.

    Only recognises derivative/hybrid classes (warrants, rights, units,
    preferred lines, notes); anything else returns ``default``. Used for legacy
    universe files and as a cross-check on provider metadata, which reports
    warrants and units as plain EQUITY.
    """
    if not name:
        return default
    for pattern, asset_type in _NAME_PATTERNS:
        if pattern.search(name):
            return asset_type
    return default


def _is_structured(path: str) -> bool:
    """Structured files have a header row starting with 'ticker'."""
    with open(path) as handle:
        first = handle.readline().strip().lower()
    return first.startswith("ticker,")


def load_universe(path: str, default_asset_type: str = COMMON_STOCK) -> pd.DataFrame:
    """Load a universe file into a frame with the full set of columns.

    Raises:
        FileNotFoundError: the universe file is missing.
        ValueError: the file contains no usable rows.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Universe file not found: {path}")

    if _is_structured(path):
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        missing = {"ticker", "name"} - set(df.columns)
        if missing:
            raise ValueError(f"{path} is missing column(s): {', '.join(sorted(missing))}")
        for column in UNIVERSE_COLUMNS:
            if column not in df.columns:
                df[column] = ""
    else:
        rows = []
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("~")
                ticker = parts[0].strip()
                if not ticker:
                    continue
                rows.append(
                    {"ticker": ticker, "name": parts[1].strip() if len(parts) > 1 else ticker}
                )
        df = pd.DataFrame(rows, columns=["ticker", "name"])
        for column in UNIVERSE_COLUMNS:
            if column not in df.columns:
                df[column] = ""

    if df.empty:
        raise ValueError(f"No usable rows in universe file: {path}")

    df = df.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)

    # Fill gaps: infer the type from the name, and leave exchange explicitly
    # unknown rather than guessing.
    blank_type = df["asset_type"].astype(str).str.strip() == ""
    df.loc[blank_type, "asset_type"] = df.loc[blank_type, "name"].apply(
        lambda n: infer_asset_type(n, default=default_asset_type)
    )
    blank_exchange = df["exchange"].astype(str).str.strip() == ""
    df.loc[blank_exchange, "exchange"] = EXCHANGE_UNKNOWN

    return df[UNIVERSE_COLUMNS]


def filter_universe(df: pd.DataFrame, asset_types) -> pd.DataFrame:
    """Keep only the requested asset types."""
    wanted = {str(t).lower() for t in asset_types}
    return df[df["asset_type"].str.lower().isin(wanted)].reset_index(drop=True)


def refresh_universe(
    path: str,
    exchange_suffix: str,
    default_asset_type: str = COMMON_STOCK,
    batch_size: int = 50,
    progress=None,
) -> pd.DataFrame:
    """Enrich a universe file with provider exchange and instrument metadata.

    Queries the provider for each ticker's listing exchange and instrument
    type, then rewrites ``path`` in structured form. Tickers the provider does
    not recognise keep their inferred values.

    The provider reports warrants and units as plain EQUITY, so the name-based
    classification wins whenever it identifies a non-common security.
    """
    import datetime

    import yfinance as yf

    df = load_universe(path, default_asset_type=default_asset_type)
    today = datetime.date.today().isoformat()
    tickers = df["ticker"].tolist()

    resolved_exchange, resolved_type = {}, {}
    for start in range(0, len(tickers), batch_size):
        batch = tickers[start : start + batch_size]
        symbols = [f"{t}.{exchange_suffix}" if exchange_suffix else t for t in batch]
        try:
            handles = yf.Tickers(" ".join(symbols)).tickers
        except Exception:
            handles = {}

        for ticker, symbol in zip(batch, symbols, strict=True):
            handle = handles.get(symbol) or handles.get(symbol.upper())
            if handle is None:
                continue
            try:
                metadata = handle.history_metadata or {}
            except Exception:
                continue
            exchange = metadata.get("fullExchangeName") or metadata.get("exchangeName")
            if exchange:
                resolved_exchange[ticker] = normalise_exchange(exchange)
            instrument = metadata.get("instrumentType")
            if instrument:
                resolved_type[ticker] = _PROVIDER_TYPES.get(str(instrument).upper(), UNKNOWN)

        if progress:
            progress(min(start + batch_size, len(tickers)), len(tickers))

    def _exchange(row):
        return resolved_exchange.get(row["ticker"], row["exchange"] or EXCHANGE_UNKNOWN)

    def _asset_type(row):
        # Name-based classification wins: the provider labels warrants and
        # units as plain EQUITY, which is exactly what must be excluded.
        from_name = infer_asset_type(row["name"], default=UNKNOWN)
        if from_name != UNKNOWN:
            return from_name
        # A provider ETF label is a trustworthy positive signal. An EQUITY
        # label is not trustworthy enough to override the universe's declared
        # type: it marks many genuine ETFs as equities.
        if resolved_type.get(row["ticker"]) == ETF:
            return ETF
        return default_asset_type

    df["exchange"] = df.apply(_exchange, axis=1)
    df["asset_type"] = df.apply(_asset_type, axis=1)
    df["source_date"] = today
    df["currency"] = df["currency"].replace("", pd.NA).fillna("")

    write_universe(df, path)
    return df


def write_universe(df: pd.DataFrame, path: str) -> None:
    """Write a universe frame atomically in structured form."""
    temp_path = f"{path}.tmp"
    df[UNIVERSE_COLUMNS].to_csv(temp_path, index=False)
    os.replace(temp_path, path)


def _main() -> None:
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config import EXCHANGE_SUFFIXES, load_config

    if len(sys.argv) != 4 or sys.argv[1] != "refresh":
        print(
            "Usage: universe.py refresh <EXCHANGE> <INSTRUMENT_TYPE>\n"
            "  Enriches the configured universe file with provider metadata.",
            file=sys.stderr,
        )
        sys.exit(2)

    _, _, exchange, instrument_type = sys.argv
    cfg = load_config(exchange, instrument_type)
    default_type = ETF if cfg.instrument_type == "etf" else COMMON_STOCK

    def show(done, total):
        print(f"  {done}/{total} resolved", flush=True)

    print(f"Refreshing {cfg.ticker_file}")
    df = refresh_universe(
        cfg.ticker_file,
        EXCHANGE_SUFFIXES[cfg.exchange],
        default_asset_type=default_type,
        progress=show,
    )
    print("\nAsset types:")
    print(df["asset_type"].value_counts().to_string())
    print("\nExchanges:")
    print(df["exchange"].value_counts().to_string())


if __name__ == "__main__":
    _main()
