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

UNIVERSE_COLUMNS = [
    "ticker",
    "name",
    "exchange",
    "asset_type",
    # Who runs the fund, and what it holds. Both are derived from the title
    # when blank (see infer_issuer / infer_category), so a value written here
    # by hand is authoritative and a new listing still arrives classified.
    "issuer",
    "category",
    "currency",
    "source_date",
]

# Yahoo throttles metadata lookups; back off rather than recording the failure
# as though the security had no exchange.
RATE_LIMIT_ATTEMPTS = 4

# Refuse to prune more than this share of a universe in one run. A provider
# outage would otherwise look like a mass delisting and empty the file.
MAX_PRUNE_SHARE = 0.30

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


# Issuers whose name in a fund title is not simply its first word: multi-word
# brands, spellings the exchange directory abbreviates, and two cases where
# the brand on the product is no longer the brand that runs it (ETF Securities
# was acquired by and rebranded to Global X in 2022; the ASX directory still
# carries the old titles).
#
# Longest first, because "Global X" has to beat a bare "Global".
_ISSUER_ALIASES = [
    ("betasharesw", "Betashares"),  # BetasharesWthBdr... -- unspaced in the directory
    ("global x", "Global X"),
    ("stt strt", "SPDR"),
    ("state street", "SPDR"),
    ("spdr", "SPDR"),
    # A real ETP provider whose name opens with a word the stop-list below
    # treats as a description; the alias has to win before that.
    ("leverage shares", "Leverage Shares"),
    ("first trust", "First Trust"),
    ("first sentier", "First Sentier"),
    ("intelligent investor", "Intelligent Investor"),
    ("investors mutual", "Investors Mutual"),
    ("russell inv", "Russell Investments"),
    ("janus henderson", "Janus Henderson"),
    ("goldman sachs", "Goldman Sachs"),
    ("t. rowe price", "T. Rowe Price"),
    ("northern trust", "Northern Trust"),
    ("loomis sayles", "Loomis Sayles"),
    ("barrow hanley", "Barrow Hanley"),
    ("vaughan nelson", "Vaughan Nelson"),
    ("western asset", "Western Asset"),
    ("resolution cap", "Resolution Capital"),
    ("loftus peak", "Loftus Peak"),
    ("l1 capital", "L1 Capital"),
    ("ten cap", "Ten Cap"),
    ("perth mint", "Perth Mint"),
    ("alpha architect", "Alpha Architect"),
    ("capital group", "Capital Group"),
    ("neuberger berman", "Neuberger Berman"),
    ("etfs ", "Global X"),
    ("dimsnl", "Dimensional"),
    ("vaneck", "VanEck"),
    ("betashares", "Betashares"),
    ("bs ", "Betashares"),
    ("hejaz", "Hejaz"),
    ("ft ", "First Trust"),
]

# A first word that describes the product rather than who issues it. Without
# this, "Australian Major Bank Subordinated Debt ETF" would be attributed to an
# issuer called "Australian".
_GENERIC_FIRST_WORDS = {
    "australian",
    "global",
    "us",
    "usa",
    "international",
    "emerging",
    "core",
    "leverage",
    "leveraged",
    "ultra",
    "max",
    "daily",
    "the",
    "short",
    "long",
    "monthly",
    "weekly",
    "enhanced",
    "active",
    "managed",
    "listed",
    "world",
    "asia",
    "asian",
    "europe",
    "european",
    "japan",
    "china",
    "india",
    "gold",
    "silver",
    "bitcoin",
    "ethereum",
    "crypto",
    "nasdaq",
    "s&p",
    "msci",
    "ftse",
    "high",
    "low",
    "small",
    "mid",
    "large",
    "total",
    "equity",
    "bond",
    "cash",
}


# Products that are issued by a manager. A common stock has no issuer in this
# sense -- the company is the security -- and inferring one from its name
# yields an "ATA" for "ATA Creativity Global", which is a fragment of a
# company name masquerading as a fund manager. Category is gated the same way:
# a stock's category is its sector, which its name does not carry.
FUND_ASSET_TYPES = frozenset({"etf", "note"})


# How the exchange directory tacks a security description onto a company name,
# in the two shapes it uses: "Company - Class A Ordinary Shares" and "Company
# Common Stock". Both have to come off, or the issuer of every SPAC's three
# lines reads as three different companies.
_SECURITY_SUFFIX = re.compile(
    r"\s+(?:"
    r"class\s+[a-z]\b.*|common\s+stock.*|ordinary\s+share.*|units?\b.*|"
    r"warrants?\b.*|rights?\b.*|preferred\s+stock.*|depositary\s+share.*|"
    r"american\s+depositary.*|subordinat.*|notes?\s+due.*|\d+(?:\.\d+)?%\s.*"
    r")$",
    re.IGNORECASE,
)


def company_name(name: str) -> str:
    """The company out of a security's directory title.

    A company issues its own shares, so it is its own issuer -- but the
    directory names the *security*, not the company: "ATA Creativity Global -
    American Depositary Shares, each representing two common shares". The
    description comes off so that a company's ordinary shares, its units and
    its warrants all report the same issuer instead of three.
    """
    text = str(name).split(" - ")[0].strip()
    return _SECURITY_SUFFIX.sub("", text).strip(" ,") or text


def _strip_issuer(name: str) -> str:
    """``name`` with its leading issuer removed, for classifying what it holds.

    Without this, "Platinum Asia ETF" is precious metals and "Platinum
    International ETF" is too -- Platinum Asset Management runs both and
    neither holds an ounce of the stuff. An issuer's name is not evidence
    about the assets.
    """
    text = str(name).strip()
    lowered = text.lower()
    for prefix, _ in _ISSUER_ALIASES:
        if lowered.startswith(prefix):
            return text[len(prefix) :].strip()
    first = lowered.split()[0].strip(",.") if lowered.split() else ""
    if first and first not in _GENERIC_FIRST_WORDS and len(first) >= 2:
        return text.split(maxsplit=1)[1] if " " in text else ""
    return text


def infer_issuer(name: str) -> str:
    """The fund manager behind a product, read out of its title.

    Almost every ETF is named "<issuer> <what it holds> ETF", so the first
    word carries it -- 82 distinct across the ASX universe, and every one of
    them a real manager. The aliases above handle the brands that span two
    words or that the directory abbreviates.

    Returns "" rather than a guess when the title opens with a description
    instead of a name: a wrong issuer is worse than a blank one, because a
    blank is visibly missing while a wrong one gets filtered on.
    """
    lowered = str(name).strip().lower()
    if not lowered:
        return ""

    for prefix, issuer in _ISSUER_ALIASES:
        if lowered.startswith(prefix):
            return issuer

    first = lowered.split()[0].strip(",.")
    if first in _GENERIC_FIRST_WORDS or len(first) < 2:
        return ""
    # Preserve the directory's own capitalisation, which is right for the
    # iShares and abrdn spellings that a .title() would ruin.
    return str(name).strip().split()[0].strip(",.")


# Asset class first, theme second, and only where the title actually says so.
# Order matters: "Global X Copper Miners" is metals rather than the equities it
# technically holds, because that is how someone screening for copper thinks
# of it.
_CATEGORY_PATTERNS = [
    ("crypto", r"bitcoin|ethereum|\bcrypto|digital asset|blockchain|solana|\bxrp\b|coinbase"),
    ("precious metals", r"\bgold\b|silver|platinum|palladium|precious metal|bullion"),
    (
        "industrial metals",
        r"copper|lithium|uranium|nickel|aluminium|aluminum|rare earth|"
        r"battery|critical mineral|steel",
    ),
    (
        "energy",
        r"\boil\b|petroleum|natural gas|\bgas\b|energy|hydrogen|carbon|"
        r"renewable|solar|wind\b|nuclear|coal",
    ),
    ("agriculture", r"agricultur|farmland|livestock|wheat|corn\b|soybean|\bfood\b"),
    ("property", r"propert|\breit\b|real estate|residential|mortgage"),
    ("infrastructure", r"infrastructure|\binfra\b|utilit|pipeline|airport|toll road"),
    (
        "fixed income",
        # "Bd" and "Bnd" because the ASX directory abbreviates titles to fit a
        # length limit: "iShares 15+ Year Australian Gov Bd ETF" is a bond fund
        # and nothing in it spells the word.
        r"\bbond|\bbd\b|\bbnd\b|ausbond|fixed income|treasur|\bcredit\b|\bcrdt|"
        r"\bhyb\b|hybrid|floating rate|"
        r"subordinated|\bdebt\b|\bgilt|income fund|"
        r"yield maximiser|term deposit|\bcash\b|money market",
    ),
    ("currency", r"currency fund|\bfx\b|dollar index|\byen\b|\beuro\b currency"),
    (
        "technology",
        r"technolog|semiconductor|robotic|automation|cyber|\bcloud\b|"
        r"artificial intelligence|\bai\b|internet|software|fang|"
        r"nasdaq ?100|\bdisruptio|innovat|digital",
    ),
    ("healthcare", r"health|biotech|pharma|medical|genomic"),
    ("financials", r"\bbank|financial|\bfincl|insur|fintech"),
    ("resources", r"resource|\bres sect|mining|miner|commodit"),
    (
        "multi-asset",
        r"diversified|divrs|divers|balanced|multi-?asset|conservative|growth fund|"
        r"retirement|target date",
    ),
    (
        "equity",
        r"\bequit|\beqs?\b|\bshares?\b|share fund|\bstock|companies|\bcoms\b|"
        r"\bcos\b|\bsect\b|top ?\\d+|ex-?\\d+|hvstr|harvest|small ?cap|"
        r"mid ?cap|large ?cap|smid|\bvalue\b|\bgrowth\b|dividend|\bindex\b|"
        r"\bs&p\b|\bmsci\b|\bftse\b|asx ?\d|all ?ord|quality|momentum|"
        r"\balpha\b|\bcore\b|\bsust|\besg\b",
    ),
]


def infer_category(name: str) -> str:
    """What a fund holds, in the terms someone screening would use.

    Deliberately blank rather than guessed for the actively managed funds
    whose titles describe a strategy and never an asset -- "Aoris
    International B Managed Fund" says nothing this can honestly classify, and
    filling it with "equity" on the assumption would make the column look
    complete while being unverified on a third of the universe.
    """
    lowered = _strip_issuer(name).lower()
    for category, pattern in _CATEGORY_PATTERNS:
        if re.search(pattern, lowered):
            return category
    return ""


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

    # Filled only where the file leaves them empty, so a hand-corrected issuer
    # or category in the universe file always wins over the inference, and
    # only for the asset types that have either.
    is_fund = df["asset_type"].astype(str).str.lower().isin(FUND_ASSET_TYPES)

    blank_issuer = df["issuer"].astype(str).str.strip() == ""
    df.loc[blank_issuer & is_fund, "issuer"] = df.loc[blank_issuer & is_fund, "name"].apply(
        infer_issuer
    )
    # A company issues its own shares, so the issuer is the company itself.
    df.loc[blank_issuer & ~is_fund, "issuer"] = df.loc[blank_issuer & ~is_fund, "name"].apply(
        company_name
    )

    # Category stays fund-only: a company's category is its sector, and its
    # name does not carry one.
    blank_category = is_fund & (df["category"].astype(str).str.strip() == "")
    df.loc[blank_category, "category"] = df.loc[blank_category, "name"].apply(infer_category)

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
        existing = None
        previous = set()

    current = set(incoming["ticker"])
    today = datetime.date.today().isoformat()

    incoming = incoming.assign(currency="USD", source_date=today)

    # The directory knows tickers, names and types; it has never heard of an
    # issuer or a category. Carry the ones already on file across by ticker,
    # or a sync would silently drop every value the universe has accumulated
    # -- including any corrected by hand. Tickers new to this sync fall
    # through to the inference in load_universe.
    for column in ("issuer", "category"):
        carried = (
            incoming["ticker"].map(existing.set_index("ticker")[column])
            if existing is not None and column in existing.columns
            else ""
        )
        incoming[column] = carried
        blank = incoming[column].isna() | (incoming[column].astype(str).str.strip() == "")
        is_fund = incoming["asset_type"].astype(str).str.lower().isin(FUND_ASSET_TYPES)
        if column == "category":
            blank &= is_fund
            incoming.loc[blank, column] = incoming.loc[blank, "name"].apply(infer_category)
        else:
            incoming.loc[blank & is_fund, column] = incoming.loc[blank & is_fund, "name"].apply(
                infer_issuer
            )
            incoming.loc[blank & ~is_fund, column] = incoming.loc[blank & ~is_fund, "name"].apply(
                company_name
            )
        incoming[column] = incoming[column].fillna("")

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
    prune: bool = False,
    progress=None,
) -> pd.DataFrame:
    """Enrich a universe file with provider exchange and instrument metadata.

    Queries the provider for each ticker's listing exchange and instrument
    type, then rewrites ``path`` in structured form. Tickers the provider does
    not recognise keep their inferred values.

    The provider reports warrants and units as plain EQUITY, so the name-based
    classification wins whenever it identifies a non-common security.

    Args:
        prune: Also drop instruments the provider has no metadata for. Only
            applied when nothing was rate limited, since a throttled lookup is
            indistinguishable from a delisted one.
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

    if prune:
        # A ticker the provider has no metadata for, on a run that was not
        # throttled, is delisted rather than merely unresolved. Leaving those
        # in the universe makes every later fetch look incomplete: they are
        # counted as requested, always fail, and drag the success ratio below
        # the publication threshold forever.
        if throttled:
            print(
                f"  Not pruning: {len(throttled)} lookups were rate limited, so a "
                f"failure cannot be distinguished from a dead listing. Re-run later.",
                flush=True,
            )
        else:
            dead = [t for t in needs_lookup if t not in resolved_exchange]
            share = len(dead) / max(1, len(df))
            if share > MAX_PRUNE_SHARE:
                print(
                    f"  Not pruning: {len(dead)} of {len(df)} instruments "
                    f"({share:.0%}) look dead, above the {MAX_PRUNE_SHARE:.0%} "
                    f"safety limit. That usually means a provider outage.",
                    flush=True,
                )
            elif dead:
                df = df[~df["ticker"].isin(dead)].reset_index(drop=True)
                print(f"  Pruned {len(dead)} delisted instrument(s)", flush=True)
    df["currency"] = df["currency"].replace("", pd.NA).fillna("")

    write_universe(df, path)
    return df


def write_universe(df: pd.DataFrame, path: str) -> None:
    """Write a universe frame atomically in structured form."""
    temp_path = f"{path}.tmp"
    df[UNIVERSE_COLUMNS].to_csv(temp_path, index=False)
    os.replace(temp_path, path)
