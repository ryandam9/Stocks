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

Run ``python src/universe.py sync <EXCHANGE> <INSTRUMENT>`` to upgrade a
legacy file in place using the data provider's own metadata.
"""

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

UNIVERSE_COLUMNS = ["ticker", "name", "exchange", "asset_type", "currency", "source_date"]

# Yahoo throttles metadata lookups; back off rather than recording the failure
# as though the security had no exchange.
RATE_LIMIT_ATTEMPTS = 4

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


def _canonical(label: str) -> str:
    """Strip case, spaces and punctuation for alias matching."""
    return re.sub(r"[^A-Z0-9]", "", label.upper())


def normalise_exchange(raw: str | None) -> str:
    """Map a provider exchange label to a Google-Finance-compatible code.

    Matching ignores case, spaces and punctuation: the provider returns the
    same venue as both "NYSEAmerican" and "NYSE AMERICAN", and Google Finance
    accepts only the unspaced form.
    """
    if not raw or (isinstance(raw, float) and pd.isna(raw)):
        return EXCHANGE_UNKNOWN
    raw = str(raw).strip()
    if not raw:
        return EXCHANGE_UNKNOWN
    if raw in _EXCHANGE_ALIASES:
        return _EXCHANGE_ALIASES[raw]

    key = _canonical(raw)
    for alias, code in _EXCHANGE_ALIASES.items():
        if _canonical(alias) == key:
            return code
    return key or EXCHANGE_UNKNOWN


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
    """Keep only the requested asset types.

    ``unknown`` is never implicitly included: an instrument whose class could
    not be established is excluded unless the caller asks for it by name, so a
    derivative can never enter a screen by defaulting into common stock.
    """
    wanted = {str(t).lower() for t in asset_types}
    return df[df["asset_type"].str.lower().isin(wanted)].reset_index(drop=True)


def default_asset_type_for(instrument_type: str) -> str:
    """The asset type a universe of ``instrument_type`` holds by default.

    Callers must pass this to :func:`load_universe`; without it a legacy
    ``TICKER~Name`` ETF file loads as common stock and is then filtered away
    entirely by an ETF config.
    """
    return ETF if str(instrument_type).lower() == "etf" else COMMON_STOCK


def sync_universe(
    path: str,
    directory: pd.DataFrame,
    instrument_type: str = "stocks",
    exchanges: list | None = None,
) -> dict:
    """Replace universe membership and metadata from an authoritative source.

    Unlike metadata enrichment, this adds newly listed symbols and drops ones
    the source no longer lists, so ``source_date`` genuinely describes the
    membership rather than the last time metadata was touched.

    Args:
        path: Universe file to rewrite.
        directory: Authoritative rows (ticker, name, exchange, asset_type).
        instrument_type: Used for the fallback asset type.
        exchanges: Restrict membership to these exchange codes.

    Returns:
        Summary with added/removed/retained ticker counts.
    """
    import datetime

    if directory.empty:
        raise ValueError("Symbol directory is empty; refusing to wipe the universe")

    incoming = directory.copy()
    if exchanges:
        wanted = {e.upper() for e in exchanges}
        incoming = incoming[incoming["exchange"].str.upper().isin(wanted)]
        if incoming.empty:
            raise ValueError(f"No symbols in the directory match exchanges {sorted(wanted)}")

    try:
        existing = load_universe(path, default_asset_type=default_asset_type_for(instrument_type))
        previous = set(existing["ticker"])
    except (FileNotFoundError, ValueError):
        previous = set()

    current = set(incoming["ticker"])
    today = datetime.date.today().isoformat()

    incoming = incoming.assign(currency="USD", source_date=today)
    write_universe(incoming[UNIVERSE_COLUMNS], path)

    return {
        "added": sorted(current - previous),
        "removed": sorted(previous - current),
        "retained": len(current & previous),
        "total": len(current),
    }


def refresh_universe(
    path: str,
    exchange_suffix: str,
    default_asset_type: str = COMMON_STOCK,
    batch_size: int = 50,
    max_workers: int = 4,
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
    from yfinance.exceptions import YFRateLimitError

    df = load_universe(path, default_asset_type=default_asset_type)
    today = datetime.date.today().isoformat()

    # Only resolve instruments that could actually be screened. Warrants,
    # units and rights are excluded by name whatever the provider says, and
    # they are also most of the delisted symbols whose lookups block for
    # seconds each, so skipping them is the bulk of the speed-up.
    needs_lookup = [
        ticker
        for ticker, name in zip(df["ticker"], df["name"], strict=True)
        if infer_asset_type(name, default=UNKNOWN) == UNKNOWN
    ]

    resolved_exchange, resolved_type = {}, {}
    throttled: list = []
    lock = threading.Lock()
    done = 0

    def resolve(ticker: str) -> bool:
        """Resolve one ticker. Returns False if the lookup itself failed.

        A failed lookup is not the same as a security with no exchange: if
        rate limiting were recorded as "unknown", a throttled run would
        quietly erase metadata for half the universe.
        """
        nonlocal done
        symbol = f"{ticker}.{exchange_suffix}" if exchange_suffix else ticker
        delay = 2.0
        ok = True
        metadata = {}
        for attempt in range(RATE_LIMIT_ATTEMPTS):
            try:
                metadata = yf.Ticker(symbol).history_metadata or {}
                ok = True
                break
            except YFRateLimitError:
                ok = False
                if attempt < RATE_LIMIT_ATTEMPTS - 1:
                    time.sleep(delay)
                    delay *= 2
            except Exception:
                # The provider genuinely has nothing for this symbol.
                metadata, ok = {}, True
                break

        exchange = metadata.get("fullExchangeName") or metadata.get("exchangeName")
        instrument = metadata.get("instrumentType")
        with lock:
            if exchange:
                resolved_exchange[ticker] = normalise_exchange(exchange)
            if instrument:
                resolved_type[ticker] = _PROVIDER_TYPES.get(str(instrument).upper(), UNKNOWN)
            if not ok:
                throttled.append(ticker)
            done += 1
            if progress and done % batch_size == 0:
                progress(done, len(needs_lookup))
        return ok

    # Keep concurrency low: Yahoo throttles these lookups aggressively, and a
    # throttled run yields no metadata at all.
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(resolve, needs_lookup))
    if progress:
        progress(len(needs_lookup), len(needs_lookup))

    if throttled:
        print(
            f"  WARNING: {len(throttled)} lookups were rate limited and could not "
            f"be resolved. Existing values for those tickers are preserved; "
            f"re-run later to fill them in.",
            flush=True,
        )

    def _exchange(row):
        # Fall back to whatever the file already held, so a throttled or
        # partial run never erases metadata resolved by an earlier one.
        existing = (row["exchange"] or "").strip() or EXCHANGE_UNKNOWN
        return resolved_exchange.get(row["ticker"], existing)

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

    usage = (
        "Usage: universe.py <sync|enrich> <EXCHANGE> <INSTRUMENT_TYPE>\n"
        "  sync    replace membership and metadata from the authoritative\n"
        "          exchange symbol directory (US listings only)\n"
        "  enrich  fill metadata for the tickers already in the file, using\n"
        "          provider lookups (works for any market)"
    )
    if len(sys.argv) != 4 or sys.argv[1] not in {"sync", "enrich"}:
        print(usage, file=sys.stderr)
        sys.exit(2)

    command, exchange, instrument_type = sys.argv[1:4]
    cfg = load_config(exchange, instrument_type)
    default_type = default_asset_type_for(cfg.instrument_type)

    if command == "sync":
        from symbol_directory import US_EXCHANGES, fetch_symbol_directory

        if cfg.exchange not in US_EXCHANGES:
            print(
                f"Error: 'sync' uses the US exchange symbol directory and does not "
                f"cover {cfg.exchange}. Use 'enrich' instead.",
                file=sys.stderr,
            )
            sys.exit(1)

        print(f"Downloading symbol directory for {cfg.exchange}...")
        directory = fetch_symbol_directory()
        summary = sync_universe(
            cfg.ticker_file,
            directory,
            instrument_type=cfg.instrument_type,
            exchanges=US_EXCHANGES[cfg.exchange],
        )
        print(
            f"  {summary['total']} symbols "
            f"(+{len(summary['added'])} added, -{len(summary['removed'])} removed, "
            f"{summary['retained']} retained)"
        )
        if summary["added"]:
            print(
                f"  added:   {', '.join(summary['added'][:10])}"
                + (" ..." if len(summary["added"]) > 10 else "")
            )
        if summary["removed"]:
            print(
                f"  removed: {', '.join(summary['removed'][:10])}"
                + (" ..." if len(summary["removed"]) > 10 else "")
            )
        df = load_universe(cfg.ticker_file, default_asset_type=default_type)
    else:

        def show(done, total):
            print(f"  {done}/{total} resolved", flush=True)

        print(f"Enriching {cfg.ticker_file}")
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
