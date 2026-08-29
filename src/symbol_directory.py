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

import csv
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


# --------------------------------------------------------------- ASX

# The ASX has no equivalent of the Nasdaq Trader files. Its company directory
# is served as CSV through the research API its website uses; the access token
# is the public one embedded in that site, not a credential.
#
# The directory is *membership*, not classification: it lists every quoted
# code including ETPs, but says nothing about which are funds. That is why a
# new ASX code needs a provider lookup before it can enter an ETF universe,
# and why the ASX refresh is additive-with-a-check rather than a wholesale
# replacement like the US one.
ASX_DIRECTORY_URL = (
    "https://asx.api.markitdigital.com/asx-research/1.0/companies/directory/file"
    "?access_token=83ff96335c2d45a094df02a206a39ff4"
)


def parse_asx_directory(text: str) -> pd.DataFrame:
    """Parse the ASX company directory CSV into ticker/name rows.

    Tolerant about the header, because this file has changed shape before: the
    older static export led with a title block and spelled the columns
    "Company name,ASX code,GICS industry group", while the research API leads
    with the code. The header is found by looking for a row naming both a code
    and a name, whatever the order or wording.

    Raises:
        ValueError: no header row that names a code and a name.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        lowered = line.lower()
        if "code" in lowered and "name" in lowered:
            rows = list(csv.DictReader(io.StringIO("\n".join(lines[index:]))))
            if not rows:
                raise ValueError("ASX directory: parsed no rows")
            break
    else:
        raise ValueError("ASX directory: no header row naming a code and a name")

    def pick(row, *wanted):
        for key in row:
            if key and key.strip().lower() in wanted:
                return (row[key] or "").strip()
        return ""

    frame = pd.DataFrame(
        [
            {
                "ticker": pick(row, "asx code", "code", "ticker", "symbol").upper(),
                "name": pick(row, "company name", "name", "security name"),
                "exchange": "ASX",
            }
            for row in rows
        ]
    )
    frame = frame[frame["ticker"] != ""].drop_duplicates(subset=["ticker"], keep="first")
    if frame.empty:
        raise ValueError("ASX directory: parsed no rows")
    return frame.reset_index(drop=True)


def fetch_asx_directory(timeout: int = 60, text: str | None = None) -> pd.DataFrame:
    """Download the ASX company directory.

    Args:
        timeout: Request timeout in seconds.
        text: Pre-fetched CSV content, for tests and for the days the ASX
            blocks scripted downloads -- the file can be saved by hand and fed
            in with ``--from-file``.
    """
    return parse_asx_directory(text if text is not None else _download(ASX_DIRECTORY_URL, timeout))


# ------------------------------------- ASX investment products report

# A row in the report's ETP tables reads as a code, the fund's name, then
# columns of figures. The name ends where the numbers begin.
#
# An ASX code is three to six alphanumerics, not three or four letters:
# A200, 1GOV and 28BB carry digits, and ETPMAG and PMGOLD are six long.
# Letters-only-and-four dropped 32 funds from a 452-row report, and did it
# under the caller's drop guard, so it would have been written.
_REPORT_ROW = re.compile(r"^([A-Z0-9]{3,6})\s+(.+)$")
# Where the statistics columns begin. Deliberately not "the first digit": a
# fund name carries numbers of its own -- "Betashares Australia 200 ETF",
# "VanEck 1-5 Year Australian Govt Bd ETF" -- and cutting at those truncated
# both to "Betashares Australia" and "VanEck". A column figure is a money or
# percentage value, so it has a decimal point or a percent sign; a number
# inside a fund's name has neither.
_FIRST_FIGURE = re.compile(r"\s(?=\(?-?\$?[\d,]*\d\.\d|\(?-?\$?[\d,]*\d%)")

# Words that head a line in the report but can never be an ASX code. Kept
# deliberately short: USD and AUD look like column labels and are real funds
# (BetaShares US Dollar ETF trades as ASX:USD), and excluding a word that is
# also a ticker loses a fund silently. A stray heading that slips through
# instead shows up as an addition in the diff, where it is seen and removed.
_REPORT_NON_CODES = {
    "TOTAL",
    "FUNDS",
    "CODE",
    "NAME",
    "MARKET",
    "MONTH",
    "REPORT",
    "PAGE",
    # ASX Limited trades as ASX:ASX, but it heads the document's own title
    # here. ETF and ETP head sections. None is an ASX product code, unlike
    # USD and AUD, which are BetaShares currency funds and must stay out of
    # this set.
    "ASX",
    "ETF",
    "ETP",
}


# At most this many continuation lines are pulled into one row. A section
# heading has no figures either, and without a limit it would swallow the page
# beneath it looking for some.
_MAX_WRAPPED_LINES = 2

# The report puts the security's form between the code and its name -- "ABG
# Stapled Abacus Group", "AIQ Units Alternative Investment Trust", "8IH CDI 8I
# Holdings Ltd" -- and it is not part of the name.
_SECURITY_TYPE = re.compile(
    r"^(?:cdi|stapled|shares?|units?|ord|ordinary|fpo|securities)\b\s*", re.IGNORECASE
)

# Columns that are neither a figure nor part of the name.
_NOT_APPLICABLE = re.compile(r"\s+n/a\b", re.IGNORECASE)


def _unwrap(text: str):
    """Yield report lines with wrapped rows rejoined.

    A long fund name wraps in the PDF's table and takes the row's figures onto
    the next line -- "BBFD Betashares Geared Short US Treasury Bond Currency
    Hedged Complex" then "ETF 168.0 78.0 0.68%". Parsed line by line, the
    first half has no figures and the second half has no code, so the fund
    disappears. Lines are therefore joined until they carry a figure.

    A buffer that does not itself begin with a code is a heading, and is
    abandoned rather than extended. Without that, the column header "($m)
    Turnover MER" -- which has no figures either -- absorbs the first fund
    underneath it and that fund is lost.
    """
    buffer = ""
    joined = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if buffer and _REPORT_ROW.match(buffer):
            buffer = f"{buffer} {line}"
            joined += 1
        else:
            buffer, joined = line, 0
        if _FIRST_FIGURE.search(buffer) or joined >= _MAX_WRAPPED_LINES:
            yield buffer
            buffer, joined = "", 0
    if buffer:
        yield buffer


def parse_asx_report_text(text: str) -> pd.DataFrame:
    """ASX codes and fund names out of the Investment Products report.

    The ASX publishes this monthly as a PDF, and it is the only list that is
    *only* exchange-traded products -- the company directory carries every
    quoted code without marking which are funds. That makes it the right
    source for an ETF universe and the wrong shape for a machine: there is no
    CSV, so the rows are read out of the extracted text.

    Deliberately loose about the columns and strict about the code, because
    the report's layout changes between months while an ASX code does not.
    Anything it cannot read is simply not returned, which the caller's drop
    guard then refuses to act on.

    Raises:
        ValueError: nothing in the text looked like a product row.
    """
    rows = []
    for line in _unwrap(text):
        match = _REPORT_ROW.match(line)
        if not match:
            continue
        code, rest = match.group(1), match.group(2).strip()
        # A run of digits is a figure that happens to start a line, not a code.
        if code in _REPORT_NON_CODES or not any(c.isalpha() for c in code):
            continue
        # Cut the name where the figures start. A row with no figures at all
        # is not a product: the report is a statistics table, and every ETP
        # carries a market cap. This is what stops the document's own title,
        # "ASX Investment Products", entering the universe as ticker ASX.
        figure = _FIRST_FIGURE.search(rest)
        if figure is None:
            continue
        name = rest[: figure.start()]
        name = _NOT_APPLICABLE.split(name)[0]
        name = _SECURITY_TYPE.sub("", name).strip(" .,-")
        # A fund name starts with a letter. "TOTAL 402 products" does not, and
        # a heading is otherwise shaped exactly like a row.
        if len(name) < 4 or not name[:1].isalpha():
            continue
        rows.append({"ticker": code, "name": name, "exchange": "ASX"})

    frame = pd.DataFrame(rows)
    if frame.empty:
        raise ValueError(
            "ASX report: no product rows found. Check the file is the "
            "Investment Products report and that its text extracted."
        )
    return frame.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)


def read_pdf_text(path: str) -> str:
    """Extract every page's text from a PDF.

    pypdf is imported here rather than required by the project: this runs on a
    developer machine before an image build, and the container has no business
    carrying a PDF parser.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise SystemExit(
            "Reading the ASX report needs pypdf, which is not a project "
            "dependency because the pipeline never reads a PDF.\n"
            "    uv pip install pypdf     (or: pip install pypdf)"
        ) from exc

    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
