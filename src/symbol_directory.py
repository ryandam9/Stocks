"""Authoritative US symbol directory from Nasdaq Trader.

Per-ticker provider metadata cannot reliably tell a SPAC's common shares from
its rights or warrants: all three often carry the same company name, and the
provider reports every one of them as EQUITY. The exchange's own symbol
directory does distinguish them, because the security name carries the class:

    AACB   Artius II Acquisition Inc. - Class A Ordinary Shares
    AACBR  Artius II Acquisition Inc. - Rights
    AACBU  Artius II Acquisition Inc. - Units

Two files cover the US listed universe:

* ``nasdaqlisted.txt``  - Nasdaq-listed securities
* ``otherlisted.txt``   - NYSE, NYSE American, NYSE Arca, Cboe BZX and IEX

Both are pipe-delimited with a header row and a trailing "File Creation Time"
line, and both carry an authoritative ETF flag.
"""

import io
import re
import urllib.request

import pandas as pd

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"

# Exchange codes used in otherlisted.txt.
_EXCHANGE_CODES = {
    "A": "NYSEAMERICAN",
    "N": "NYSE",
    "P": "NYSEARCA",
    "Z": "CBOEBZX",
    "V": "IEX",
}

# Security-name patterns, most specific first. Order matters twice over:
# derivative classes are tested before the common-stock patterns because a
# name can contain both ("Units, each consisting of one Ordinary Share and one
# Warrant"), and a few equity forms must pre-empt a broader pattern that would
# otherwise swallow them.
_NAME_CLASSES = [
    # Master limited partnerships list common units as their primary equity.
    # They are ordinary exposure and must not fall into the SPAC "unit" rule.
    ("common_stock", re.compile(r"\bcommon units? representing\b", re.I)),
    # Derivative classes first: a depositary-receipt warrant is a warrant, so
    # these must pre-empt the equity patterns below.
    ("note", re.compile(r"\b(notes?\s+due|debentures?|senior notes?)\b", re.I)),
    ("right", re.compile(r"\brights?\b", re.I)),
    ("warrant", re.compile(r"\bwarrants?\b", re.I)),
    ("unit", re.compile(r"\bunits?\b", re.I)),
    # Depositary receipts and registry shares are ordinary equity exposure.
    # Tested before "preferred", whose pattern also matches "depositary".
    (
        "common_stock",
        re.compile(
            r"\b(american depositary|american depository|adss?|adrs?|"
            r"new york registry|global registry)\b",
            re.I,
        ),
    ),
    (
        "preferred",
        re.compile(r"\b(preferred|depositary shares?|depositary receipts?)\b|%", re.I),
    ),
    # Closed-end funds are pooled vehicles, screened with funds rather than
    # operating companies.
    ("etf", re.compile(r"\b(closed[\s-]?end fund|etf|exchange[\s-]?traded)\b", re.I)),
    (
        "common_stock",
        re.compile(
            r"\b(common stock|ordinary shares?|ord\.? shares?|common shares?|"
            r"capital stock|subordinate voting shares?|voting shares?|"
            r"class [a-z] (common|ordinary|capital|shares?)|"
            r"registered shares?|"
            r"shares? of beneficial interest|"
            # REITs and BDCs are listed operating companies, not pooled funds.
            r"real estate investment trust|business development company)\b",
            re.I,
        ),
    ),
]

DIRECTORY_COLUMNS = ["ticker", "name", "exchange", "asset_type"]

# Which directory venues make up each configured exchange universe. Links are
# always built from each ticker's own venue, so a multi-venue universe stays
# correct per instrument.
US_EXCHANGES = {
    "US": ["NASDAQ", "NYSE", "NYSEAMERICAN", "NYSEARCA", "CBOEBZX", "IEX"],
    "NASDAQ": ["NASDAQ"],
    "NYSE": ["NYSE", "NYSEAMERICAN", "NYSEARCA"],
}


def classify_security_name(name: str, is_etf: bool = False) -> str:
    """Classify a directory security name into an asset type.

    Args:
        name: Security name as published by the exchange.
        is_etf: The directory's ETF flag, which is authoritative.

    Returns:
        One of unit/right/warrant/preferred/note/etf/common_stock/unknown.
    """
    if is_etf:
        return "etf"
    if not name:
        return "unknown"

    for asset_type, pattern in _NAME_CLASSES:
        if pattern.search(name):
            return asset_type
    return "unknown"


def _read_directory(text: str, symbol_column: str, kind: str) -> pd.DataFrame:
    """Parse one pipe-delimited directory file into normalised rows."""
    # The trailing "File Creation Time" line is not a record.
    lines = [line for line in text.splitlines() if line and "File Creation Time" not in line]
    frame = pd.read_csv(io.StringIO("\n".join(lines)), sep="|", dtype=str).fillna("")

    if symbol_column not in frame.columns:
        raise ValueError(
            f"{kind} directory is missing the '{symbol_column}' column; got {list(frame.columns)}"
        )

    # Test symbols are not tradeable instruments.
    if "Test Issue" in frame.columns:
        frame = frame[frame["Test Issue"].str.upper() != "Y"]

    is_etf = frame.get("ETF", pd.Series("", index=frame.index)).str.upper() == "Y"

    if kind == "nasdaq":
        exchange = pd.Series("NASDAQ", index=frame.index)
    else:
        exchange = frame["Exchange"].str.upper().map(_EXCHANGE_CODES).fillna("UNKNOWN")

    result = pd.DataFrame(
        {
            "ticker": frame[symbol_column].str.strip(),
            "name": frame["Security Name"].str.strip(),
            "exchange": exchange,
            "asset_type": [
                classify_security_name(name, etf)
                for name, etf in zip(frame["Security Name"], is_etf, strict=True)
            ],
        }
    )
    return result[result["ticker"] != ""]


def _download(url: str, timeout: int) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def fetch_symbol_directory(
    timeout: int = 60,
    nasdaq_text: str | None = None,
    other_text: str | None = None,
) -> pd.DataFrame:
    """Download and merge both directory files.

    Args:
        timeout: Per-request timeout in seconds.
        nasdaq_text: Pre-fetched nasdaqlisted.txt content, for tests.
        other_text: Pre-fetched otherlisted.txt content, for tests.

    Returns:
        One row per symbol with ticker, name, exchange and asset_type.

    Raises:
        ValueError: a directory file could not be parsed.
    """
    if nasdaq_text is None:
        nasdaq_text = _download(NASDAQ_LISTED_URL, timeout)
    if other_text is None:
        other_text = _download(OTHER_LISTED_URL, timeout)

    nasdaq = _read_directory(nasdaq_text, "Symbol", "nasdaq")
    other = _read_directory(other_text, "ACT Symbol", "other")

    combined = pd.concat([nasdaq, other], ignore_index=True)
    # A symbol listed in both files keeps its Nasdaq row, which comes first.
    combined = combined.drop_duplicates(subset=["ticker"], keep="first")
    return combined[DIRECTORY_COLUMNS].reset_index(drop=True)
