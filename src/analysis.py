"""Identify tickers whose price grew materially over a set of trailing windows.

Growth for a window is the percentage change between an opening and a closing
price, subject to eligibility rules that keep the output tradeable. How those
two endpoints are picked is set by ``analysis.return_basis``:

``google_finance`` (the default)
    Reproduces the percentage on a Google Finance quote page, so a result can
    be checked against one: single closes, a window starting at the last
    session on or before the calendar anchor, and the raw (split-adjusted,
    dividend-unadjusted) close.

``robust``
    Median of the first and last N trading days on the fully adjusted series,
    so one bad print cannot define a window's return and dividends count
    toward it. Reads higher than Google Finance on anything with a yield.

Every run publishes a complete set of outputs. A window that matches nothing
still writes an empty CSV with the correct header, so a later run can never
leave an earlier run's results in place to be republished as current.
"""

import os
import sys
from typing import Any

import click
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    EXCHANGE_SUFFIXES,
    GROWTH_COLUMNS,
    INSTRUMENTS,
    PROJECT_ROOT,
    RETURN_BASIS_GOOGLE_FINANCE,
    UNIVERSE_HISTORY_MONTHS,
    AnalysisSettings,
    load_config,
    load_dotenv,
    settings_for_window,
)
from runmeta import (
    RunManifest,
    atomic_write_csv,
    code_revision,
    new_run_id,
    read_manifest,
)
from universe import (
    EXCHANGE_UNKNOWN,
    UNIVERSE_COLUMNS,
    default_asset_type_for,
    filter_universe,
    load_universe,
)

# Maps analysis window length (months) to the Google Finance chart parameter.
GF_WINDOW = {12: "1Y", 6: "6M", 3: "3M", 1: "1M"}

# A ticker with no prints in this many calendar days is treated as no longer
# trading and excluded regardless of how well it performed.
MAX_STALENESS_DAYS = 10

REQUIRED_COLUMNS = {"ticker", "name", "close", "stock_price_date"}

# Columns omitted from the combined price-history output. Each is either
# constant across a whole run or joinable from the per-window tables, so
# storing it on every daily row is pure duplication.
REDUNDANT_HISTORY_COLUMNS = [
    "name",
    "fetch_time",
    "price_basis",
    "fetch_run_id",
    "run_id",
]

# Price provenance. Only a known raw fallback is excluded: an unadjusted series
# spanning a split produces a badly wrong return. UNKNOWN marks data fetched
# before provenance was recorded, which is screened with a warning.
BASIS_ADJUSTED = "adjusted"
BASIS_RAW_FALLBACK = "raw_fallback"
BASIS_UNKNOWN = "unknown"
SCREENABLE_BASES = (BASIS_ADJUSTED, BASIS_UNKNOWN)


pd.set_option("display.max_columns", None)
pd.set_option("display.max_rows", None)
pd.set_option("display.width", 1000)
pd.set_option("display.max_colwidth", 1000)


class StaleDataError(RuntimeError):
    """The price dataset is too old to screen."""


def load_price_data(
    file_path: str, return_basis: str = RETURN_BASIS_GOOGLE_FINANCE
) -> pd.DataFrame:
    """Read and normalise the EOD price CSV.

    Args:
        file_path: the EOD price CSV.
        return_basis: which series ``price`` is taken from. See
            :data:`config.RETURN_BASIS`.

    Raises:
        FileNotFoundError: the CSV does not exist.
        ValueError: required columns are missing or no usable rows remain.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Price data not found: {file_path}")

    df = pd.read_csv(file_path)

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"{file_path} is missing required column(s): {', '.join(sorted(missing))}")

    df["stock_price_date"] = pd.to_datetime(df["stock_price_date"])

    # price_basis distinguishes a genuinely adjusted series from a raw-close
    # fallback. It gates the adjusted series only: under "google_finance" the
    # raw close is what is wanted, and the provider reports it split-adjusted
    # already, so the fallback marker says nothing about its usability.
    if "price_basis" not in df.columns:
        # Written before provenance existed, so the basis genuinely cannot be
        # determined. It must not be guessed from close == adj_close: under the
        # provider's auto_adjust default both columns hold the *adjusted*
        # series, so equality means "both adjusted", not "unadjusted".
        # Screening such a file is allowed but warned about; excluding it
        # silently would report "nothing grew" for an entire dataset.
        df["price_basis"] = BASIS_UNKNOWN

    if return_basis == RETURN_BASIS_GOOGLE_FINANCE:
        # Yahoo's raw close is split-adjusted but not dividend-adjusted, which
        # is precisely the series Google Finance charts and quotes.
        price_col = "close"
    else:
        price_col = "adj_close" if "adj_close" in df.columns else "close"
    df["price"] = pd.to_numeric(df[price_col], errors="coerce")

    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    else:
        # Without volume the liquidity filter cannot bite; make that explicit
        # rather than silently dropping every ticker.
        df["volume"] = float("inf")

    df = df.dropna(subset=["price"])
    df = df[df["price"] > 0]

    # Duplicate (ticker, date) rows would make endpoint selection arbitrary.
    df = df.drop_duplicates(subset=["ticker", "stock_price_date"], keep="last")

    if df.empty:
        raise ValueError(f"No usable price rows in {file_path}")

    # Endpoint selection relies on rows being in date order per ticker.
    return df.sort_values(["ticker", "stock_price_date"], kind="mergesort")


def assert_data_is_fresh(
    latest_date: pd.Timestamp, max_age_days: int, allow_stale: bool = False
) -> int:
    """Reject a dataset whose newest row is too old.

    Per-ticker staleness is measured against the dataset's own latest date, so
    it cannot detect that the dataset as a whole is out of date. A fetch that
    has not succeeded for a month would otherwise be screened as current.

    Returns:
        Age of the dataset in days.

    Raises:
        StaleDataError: the dataset is older than ``max_age_days``.
    """
    age_days = (pd.Timestamp.now().normalize() - latest_date.normalize()).days
    if age_days > max_age_days and not allow_stale:
        raise StaleDataError(
            f"Price data is {age_days} days old (newest row {latest_date:%Y-%m-%d}, "
            f"limit {max_age_days} days). Re-run the fetch, or pass --allow-stale "
            f"to screen it anyway."
        )
    return age_days


def _worst_price_basis(values) -> str:
    """Reduce a ticker's row-level bases to its weakest one.

    Lexical ``min`` would return "adjusted" for a window mixing adjusted and
    raw_fallback rows, letting partially unadjusted history pass as verified.
    Quality must degrade to the worst row, not the alphabetically first.
    """
    seen = set(values)
    if BASIS_RAW_FALLBACK in seen:
        return BASIS_RAW_FALLBACK
    if BASIS_UNKNOWN in seen:
        return BASIS_UNKNOWN
    return BASIS_ADJUSTED


def _endpoint_prices(window_df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Median price over the first and last ``n`` trading days per ticker.

    ``n = 1`` degenerates to the window's opening and closing close, which is
    what the "google_finance" return_basis uses.
    """
    grouped = window_df.groupby("ticker", sort=False)
    first = grouped.head(n).groupby("ticker", sort=False)["price"].median()
    last = grouped.tail(n).groupby("ticker", sort=False)["price"].median()
    return pd.DataFrame({"first_price": first, "last_price": last})


def window_cutoff(latest_date: pd.Timestamp, window: dict) -> pd.Timestamp:
    """Start of a trailing window, which may be given in months or days."""
    if "days" in window:
        return latest_date - pd.Timedelta(days=int(window["days"]))
    return latest_date - pd.DateOffset(months=int(window["months"]))


def endpoint_window_for(settings: AnalysisSettings) -> int:
    """Trading days median-averaged at each endpoint, honouring return_basis.

    "google_finance" is defined on single closes, so the configured (and
    per-window overridden) endpoint_window does not apply to it. Forcing it
    here rather than rejecting the combination in ``load_config`` keeps a
    config valid when return_basis is switched back to "robust".
    """
    if settings.return_basis == RETURN_BASIS_GOOGLE_FINANCE:
        return 1
    return settings.endpoint_window


def select_window(df: pd.DataFrame, cutoff: pd.Timestamp, return_basis: str) -> pd.DataFrame:
    """Rows belonging to the trailing window starting at ``cutoff``.

    The two bases disagree on which side of the anchor the window opens. The
    calendar anchor is frequently not a trading day -- a 6-month window off a
    Monday lands on a Saturday -- and "robust" then takes the *next* session
    while Google Finance takes the *previous* one, a difference worth close to
    a percentage point on a volatile name.

    The anchor is resolved per ticker rather than once for the whole frame:
    exchanges keep different holiday calendars, and a halted ticker has no
    print on a day its neighbours do.
    """
    if return_basis != RETURN_BASIS_GOOGLE_FINANCE:
        return df[df["stock_price_date"] >= cutoff]

    prior = df[df["stock_price_date"] <= cutoff]
    if prior.empty:
        return df[df["stock_price_date"] >= cutoff]

    # Tickers listed after the anchor have no prior session; they keep the
    # plain cutoff and are dropped later by the coverage rule anyway.
    starts = prior.groupby("ticker", sort=False)["stock_price_date"].max()
    row_start = df["ticker"].map(starts).fillna(cutoff)
    return df[df["stock_price_date"] >= row_start]


def window_label_short(window: dict) -> str:
    """Short form used in growth_periods and Google Finance links."""
    if "days" in window:
        days = int(window["days"])
        # Google Finance has no arbitrary-day range; 5D is the closest chart.
        return "5D" if days <= 7 else f"{days}D"
    return GF_WINDOW.get(int(window["months"]), "1Y")


def _window_stats(
    df: pd.DataFrame, window: dict, settings: AnalysisSettings, latest_date: pd.Timestamp
) -> tuple[pd.DataFrame, int]:
    """Per-ticker aggregates for one window, plus the window's calendar length."""
    cutoff = window_cutoff(latest_date, window)
    window_df = select_window(df, cutoff, settings.return_basis)
    if window_df.empty:
        return pd.DataFrame(), 0

    window_days = (latest_date - cutoff).days
    # Business days approximate the exchange calendar closely enough to detect
    # a history that is mostly missing; holidays cost a few percent at most.
    expected_sessions = max(1, len(pd.bdate_range(cutoff, latest_date)))

    stats = window_df.groupby("ticker", sort=False).agg(
        name=("name", "first"),
        first_date=("stock_price_date", "min"),
        last_date=("stock_price_date", "max"),
        observations=("price", "size"),
        median_volume=("volume", "median"),
        price_basis=("price_basis", _worst_price_basis),
    )
    stats = stats.join(_endpoint_prices(window_df, endpoint_window_for(settings)))

    stats["days_covered"] = (stats["last_date"] - stats["first_date"]).dt.days
    stats["coverage"] = stats["days_covered"] / window_days
    stats["observation_ratio"] = stats["observations"] / expected_sessions
    stats["staleness_days"] = (latest_date - stats["last_date"]).dt.days
    return stats, window_days


def compute_window_growth(
    df: pd.DataFrame,
    window: dict,
    threshold: float,
    settings: AnalysisSettings,
    latest_date: pd.Timestamp,
    exchange: str,
    metadata: pd.DataFrame | None = None,
    run_id: str = "",
) -> tuple[pd.DataFrame, list[tuple[str, int]]]:
    """Return the growth table for one trailing window, plus its funnel.

    Args:
        df: normalised price data for all tickers.
        window: window definition, carrying either ``months`` or ``days``.
        threshold: minimum percentage change to qualify.
        settings: eligibility thresholds.
        latest_date: most recent date present in ``df``.
        exchange: configured exchange code, used only when a ticker's own
            exchange is unknown.
        metadata: optional universe frame supplying exchange and asset_type.
        run_id: stamped onto every row for provenance.

    Returns:
        (qualifying tickers sorted by pct_change desc, funnel stage counts)
    """
    stats, _ = _window_stats(df, window, settings, latest_date)
    if stats.empty:
        return pd.DataFrame(columns=GROWTH_COLUMNS), [("Universe in window", 0)]

    # Endpoint windows must not overlap, or first and last describe partly the
    # same days and the measured change is damped toward zero.
    min_observations = max(2, 2 * endpoint_window_for(settings))

    funnel: list[tuple[str, int]] = [("Universe in window", len(stats))]
    stages = [
        ("Enough span", stats["coverage"] >= settings.min_coverage),
        (
            "Enough observations",
            (stats["observation_ratio"] >= settings.min_observation_ratio)
            & (stats["observations"] >= min_observations),
        ),
        ("Still trading", stats["staleness_days"] <= MAX_STALENESS_DAYS),
        # Only the adjusted series can be silently unadjusted. Under
        # "google_finance" the raw close is the intended input, so a
        # raw_fallback ticker is not degraded and must not be dropped.
        (
            "Adjusted prices",
            pd.Series(True, index=stats.index)
            if settings.return_basis == RETURN_BASIS_GOOGLE_FINANCE
            else stats["price_basis"].isin(SCREENABLE_BASES),
        ),
        ("Liquid enough", stats["median_volume"] >= settings.min_median_volume),
        ("Above price floor", stats["last_price"] >= settings.min_price),
        ("Valid baseline", stats["first_price"] > 0),
    ]

    kept = stats
    for label, condition in stages:
        kept = kept[condition.reindex(kept.index, fill_value=False)]
        funnel.append((label, len(kept)))

    if kept.empty:
        funnel.append((f"Return above {threshold}%", 0))
        return pd.DataFrame(columns=GROWTH_COLUMNS), funnel

    result = kept.copy()
    result["pct_change"] = (
        (result["last_price"] - result["first_price"]) / result["first_price"] * 100
    ).round(2)
    result = result[result["pct_change"] > threshold]
    funnel.append((f"Return above {threshold}%", len(result)))

    if result.empty:
        return pd.DataFrame(columns=GROWTH_COLUMNS), funnel

    result = result.reset_index().rename(columns={"last_price": "latest_price"})

    # Attach the ticker's real listing exchange. Falling back to the configured
    # exchange for everything is what produced links labelling NYSE securities
    # as NASDAQ.
    if metadata is not None and not metadata.empty:
        result = result.merge(
            metadata[["ticker", "exchange", "asset_type"]], on="ticker", how="left"
        )
    else:
        result["exchange"] = EXCHANGE_UNKNOWN
        result["asset_type"] = ""
    result["exchange"] = result["exchange"].fillna(EXCHANGE_UNKNOWN).replace("", EXCHANGE_UNKNOWN)
    result["asset_type"] = result["asset_type"].fillna("")

    result["first_price"] = result["first_price"].round(4)
    result["latest_price"] = result["latest_price"].round(4)
    result["coverage"] = result["coverage"].round(3)
    result["observation_ratio"] = result["observation_ratio"].round(3)
    result["median_volume"] = result["median_volume"].round(0)
    result["first_date"] = result["first_date"].dt.strftime("%Y-%m-%d")
    result["last_date"] = result["last_date"].dt.strftime("%Y-%m-%d")
    result["data_as_of"] = latest_date.strftime("%Y-%m-%d")
    result["run_id"] = run_id

    # A link is only emitted when the true exchange is known; a guessed
    # exchange code produces a page for the wrong security.
    known = result["exchange"] != EXCHANGE_UNKNOWN
    result["google_finance"] = ""
    result.loc[known, "google_finance"] = (
        "https://www.google.com/finance/quote/"
        + result.loc[known, "ticker"].str.upper()
        + ":"
        + result.loc[known, "exchange"].str.upper()
        + "?window="
        + window_label_short(window)
    )

    result["threshold"] = threshold
    return result[GROWTH_COLUMNS].sort_values("pct_change", ascending=False), funnel


def _print_funnel(funnel: list[tuple[str, int]]) -> None:
    """Print the eligibility funnel so an empty result is never ambiguous."""
    for label, count in funnel:
        print(f"    {label:<28} {count:>7,}")


def _growth_output_path(eod_path: str, suffix: str) -> str:
    """Derive a growth CSV path from the EOD CSV path."""
    stem, extension = os.path.splitext(eod_path)
    return f"{stem}{suffix}{extension or '.csv'}"


def _sample_price_history(combined: pd.DataFrame, sampling: str) -> pd.DataFrame:
    """Thin published price history down to one row per ticker per period.

    Every mode keeps the **last trading day** of each period rather than a
    fixed calendar date. Anchoring on calendar dates does not survive contact
    with a trading calendar: over the last year the 1st of the month was a
    trading day in only 6 of 13 months and the 15th in 8 of 13, so a fixed
    anchor needs a nearest-session rule and still lands unevenly.

    Keeping each period's last session also guarantees the series ends on the
    newest close, which is the point a chart is read from.

    Args:
        sampling: one of :data:`config.PRICE_HISTORY_SAMPLING`.

    Returns:
        ``combined`` unchanged for ``"daily"``, else its sampled subset.
    """
    if sampling == "daily" or combined.empty:
        return combined

    dates = combined["stock_price_date"]
    if sampling == "weekly":
        period = dates.dt.to_period("W").astype(str)
    elif sampling == "month_end":
        period = dates.dt.to_period("M").astype(str)
    elif sampling == "semi_monthly":
        # Two periods a month, split at the 15th.
        half = dates.dt.day.gt(15).map({False: "H1", True: "H2"})
        period = dates.dt.to_period("M").astype(str) + "-" + half
    else:  # pragma: no cover - load_config rejects anything else
        raise ValueError(f"unknown price_history_sampling: {sampling!r}")

    last = combined.groupby(["ticker", period], sort=False)["stock_price_date"].transform("max")
    return combined[dates.eq(last)]


# Per-row facts kept in the universe-wide history table. Everything constant
# for a run (fetch_time, price_basis, run ids) or joinable on ticker (name,
# exchange, asset_type) is deliberately absent: on a universe-wide table those
# columns are repeated on every row of every ticker, and they are already in
# the manifest and the universe file.
UNIVERSE_HISTORY_COLUMNS = [
    "ticker",
    "stock_price_date",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
]


def build_universe_table(metadata: pd.DataFrame, file_path: str) -> str:
    """Write the resolved universe as a lookup table for the history table.

    The history table stores only per-row facts, so nothing in it names a
    ticker: for one that matched no window there is no per-window row to join
    ``name`` from either. This supplies that join, once per ticker rather than
    once per weekly row.

    Every ticker in the universe is written, including the ones the provider
    returned no prices for. A ``LEFT JOIN`` from here therefore shows which
    part of the universe is chartable and which is missing, which an inner
    join built the other way round would silently hide.

    Returns:
        Path to the CSV written.
    """
    output_path = _growth_output_path(file_path, "_universe")
    atomic_write_csv(metadata[UNIVERSE_COLUMNS].sort_values("ticker"), output_path)
    print(f"Universe lookup: {len(metadata):,} tickers -> {output_path}")
    return output_path


def build_universe_history(
    df: pd.DataFrame,
    file_path: str,
    latest_date: pd.Timestamp,
    sampling: str = "weekly",
    months: int = UNIVERSE_HISTORY_MONTHS,
) -> str:
    """Write a trailing-``months`` sampled price history for the whole universe.

    Unlike :func:`build_combined_growth` this is not restricted to tickers that
    matched a screen window, and the ``asset_types`` filter is not applied --
    the table exists so any ticker in the universe can be charted, including
    the ones the screen deliberately ignores.

    The series is trimmed to a clean ``months``-month span even though the
    fetch pulls 400 days, so the row count matches the name of the table it
    lands in. Trimming happens before sampling, so the first weekly point sits
    inside the window rather than before it.

    Always writes a file, empty-but-headed when nothing qualifies, so a later
    run cannot leave an earlier run's history in place to be republished.

    Returns:
        Path to the CSV written.
    """
    output_path = _growth_output_path(file_path, "_universe_history")
    cutoff = latest_date - pd.DateOffset(months=months)

    history = df[df["stock_price_date"] >= cutoff]
    sampled = _sample_price_history(history, sampling)

    # adj_close is absent from price files written before it was recorded.
    # Emit the column regardless so the published table's schema does not
    # depend on the age of the CSV underneath it.
    sampled = sampled.reindex(columns=UNIVERSE_HISTORY_COLUMNS)
    sampled = sampled.copy()
    if not sampled.empty:
        sampled["stock_price_date"] = pd.to_datetime(sampled["stock_price_date"]).dt.strftime(
            "%Y-%m-%d"
        )
    sampled = sampled.sort_values(["ticker", "stock_price_date"], kind="mergesort")

    atomic_write_csv(sampled, output_path)
    tickers = sampled["ticker"].nunique() if not sampled.empty else 0
    print(
        f"\nUniverse history ({sampling}, {months}m): {len(sampled):,} rows for "
        f"{tickers:,} tickers -> {output_path}"
    )
    return output_path


def build_combined_growth(
    df: pd.DataFrame,
    results: dict[str, pd.DataFrame],
    file_path: str,
    abbreviations: dict[str, str],
    run_id: str = "",
    sampling: str = "weekly",
) -> str:
    """Write sampled price history for every ticker that grew in any window.

    Each row carries ``growth_count`` (how many windows the ticker qualified
    in) and ``growth_periods`` (which ones). Built with pandas rather than awk
    so the comma-separated period list and names containing commas are quoted
    correctly on the way out.

    Always writes a file, empty-but-headed when nothing qualified, so a later
    run cannot leave an earlier run's combined history in place.
    """
    flags: dict[str, list[str]] = {}
    for label, result in results.items():
        if result.empty:
            continue
        short_label = abbreviations.get(label, label)
        for ticker in result["ticker"]:
            flags.setdefault(ticker, []).append(short_label)

    output_path = _growth_output_path(file_path, "_growth")
    base_columns = [c for c in df.columns if c != "price"]

    if not flags:
        empty = pd.DataFrame(
            columns=[c for c in base_columns if c not in REDUNDANT_HISTORY_COLUMNS]
            + ["growth_count", "growth_periods"]
        )
        atomic_write_csv(empty, output_path)
        print(f"\nCombined growth history: 0 rows (no ticker qualified) -> {output_path}")
        return output_path

    order = {abbrev: i for i, abbrev in enumerate(abbreviations.values())}
    summary = pd.DataFrame(
        [
            {
                "ticker": ticker,
                "growth_count": len(periods),
                "growth_periods": ",".join(sorted(periods, key=lambda p: order.get(p, 99))),
            }
            for ticker, periods in flags.items()
        ]
    )

    combined = df.merge(summary, on="ticker", how="inner")
    combined = combined.drop(columns=["price"], errors="ignore")

    # Sample before formatting the date: the sampler needs real datetimes.
    daily_rows = len(combined)
    combined = _sample_price_history(combined, sampling)

    combined["stock_price_date"] = combined["stock_price_date"].dt.strftime("%Y-%m-%d")

    # Keep only per-row facts. Columns that are constant for a run (fetch_time,
    # price_basis, fetch_run_id, run_id) or derivable by joining on ticker
    # (name) were repeated on all 438k rows and accounted for 72% of the
    # database. They live in the manifest and the per-window tables instead.
    combined = combined.drop(columns=REDUNDANT_HISTORY_COLUMNS, errors="ignore")

    atomic_write_csv(combined, output_path)
    kept = f"{len(combined) / daily_rows:.0%} of daily" if daily_rows else "empty"
    print(
        f"\nCombined growth history ({sampling}): {len(combined):,} rows for "
        f"{len(summary):,} tickers, {kept} -> {output_path}"
    )
    return output_path


def analyze_stocks(
    cfg,
    allow_stale: bool = False,
    run_id: str = "",
) -> RunManifest:
    """Run every configured window and publish a complete set of outputs."""
    settings = cfg.analysis
    run_id = run_id or new_run_id()
    manifest = RunManifest(
        run_id=run_id,
        stage="analysis",
        exchange=cfg.exchange,
        instrument_type=cfg.instrument_type,
        started_at=pd.Timestamp.now(tz="UTC").isoformat(),
        code_revision=code_revision(PROJECT_ROOT),
        universe_file=cfg.ticker_file,
        thresholds={
            "min_price": settings.min_price,
            "min_median_volume": settings.min_median_volume,
            "min_coverage": settings.min_coverage,
            "min_observation_ratio": settings.min_observation_ratio,
            "endpoint_window": endpoint_window_for(settings),
            # Consumers cannot tell a price return from a total return by
            # looking at the numbers, so the definition travels with them.
            "return_basis": settings.return_basis,
            "max_data_age_days": settings.max_data_age_days,
            "asset_types": settings.asset_types,
            # The history table's row semantics depend on these, so a consumer
            # cannot interpret it from the rows alone.
            "include_price_history": settings.include_price_history,
            "price_history_sampling": settings.price_history_sampling,
            "include_universe_history": settings.include_universe_history,
            "universe_history_months": UNIVERSE_HISTORY_MONTHS,
            "windows": settings.windows,
        },
    )

    cfg.ensure_universe()
    df = load_price_data(cfg.eod_csv, settings.return_basis)
    latest_date = df["stock_price_date"].max()

    # Lineage: record which fetch produced the price file being screened, so a
    # surprising screen can be traced back to its source dataset.
    fetch_manifest = read_manifest(os.path.join(cfg.data_dir, f"{cfg.prefix}_fetch_manifest.json"))
    if fetch_manifest:
        manifest.source_run_id = fetch_manifest.get("run_id")
        manifest.source_status = fetch_manifest.get("status")
        if fetch_manifest.get("status") not in (None, "success"):
            print(
                f"Warning: the price file was produced by a fetch reported as "
                f"'{fetch_manifest.get('status')}'; results may be incomplete."
            )
    elif "fetch_run_id" in df.columns and df["fetch_run_id"].notna().any():
        manifest.source_run_id = str(df["fetch_run_id"].dropna().iloc[0])
    age_days = assert_data_is_fresh(latest_date, settings.max_data_age_days, allow_stale)

    manifest.data_as_of = latest_date.strftime("%Y-%m-%d")

    # Restrict to the requested instrument categories. Warrants, units and
    # preferred lines are not ordinary equity exposure and should not appear
    # in a screen labelled "stocks".
    metadata = load_universe(
        cfg.ticker_file,
        default_asset_type=default_asset_type_for(cfg.instrument_type),
    )
    manifest.universe_total = len(metadata)
    screened = filter_universe(metadata, settings.asset_types)
    manifest.universe_screened = len(screened)

    excluded_types = len(metadata) - len(screened)

    # The universe-wide history is taken before the asset_types filter: the
    # screen ignores warrants and units, but a chart of one is still a chart
    # someone may want. Restricted to the current universe file all the same,
    # so a price CSV still carrying a delisted ticker does not resurrect it.
    universe_df = df[df["ticker"].isin(set(metadata["ticker"]))]

    df = df[df["ticker"].isin(set(screened["ticker"]))]
    if df.empty:
        raise ValueError(
            f"No price rows remain after filtering to asset types "
            f"{settings.asset_types}. Check the universe file: {cfg.ticker_file}"
        )

    print(f"Run {run_id}")
    print(f"Loaded {len(df):,} rows for {df['ticker'].nunique():,} tickers")
    print(f"Data as of {latest_date:%Y-%m-%d} ({age_days} day(s) old)")
    if settings.return_basis == RETURN_BASIS_GOOGLE_FINANCE:
        print(
            "Returns use the google_finance basis: single closes, the last session "
            "on or before each anchor, and unadjusted-for-dividends prices."
        )
    elif (df["price_basis"] == BASIS_UNKNOWN).any():
        print(
            "Warning: this price file predates adjusted-price provenance, so the "
            "basis cannot be verified. Re-run the fetch to record it."
        )
    if excluded_types:
        print(
            f"Universe: {len(screened):,} of {len(metadata):,} instruments match "
            f"{settings.asset_types} ({excluded_types:,} excluded)"
        )
    unknown_exchange = int((screened["exchange"] == EXCHANGE_UNKNOWN).sum())
    if unknown_exchange:
        print(
            f"Note: {unknown_exchange:,} instruments have an unknown listing exchange; "
            f"their Google Finance links are omitted. Run: "
            f"uv run src/universe.py refresh {cfg.exchange} {cfg.instrument_type}"
        )

    abbreviations = {str(w["label"]): window_label_short(w) for w in settings.windows}

    results: dict[str, pd.DataFrame] = {}
    outputs: dict[str, Any] = {}
    counts: dict[str, Any] = {}

    for window in settings.windows:
        label = str(window["label"])
        threshold = float(window["threshold"])
        # Short windows need looser endpoint/coverage rules than a 1-year one.
        window_settings = settings_for_window(settings, window)

        print(f"\n--- Tickers with >{threshold}% growth over the last {label} ---")
        result, funnel = compute_window_growth(
            df,
            window,
            threshold,
            window_settings,
            latest_date,
            cfg.exchange,
            screened,
            run_id,
        )
        _print_funnel(funnel)
        results[label] = result
        counts[label] = dict(funnel)

        if not result.empty:
            print(result.to_string(index=False))

    # Publish only after every window has computed. Writing each file as it
    # finished meant a failure part-way left some outputs from this run and
    # the rest from the previous one, which a consumer cannot distinguish.
    for label, result in results.items():
        output_path = _growth_output_path(cfg.eod_csv, f"_growth_{label}")
        atomic_write_csv(result, output_path)
        outputs[label] = {"path": output_path, "rows": len(result)}
        print(f"Wrote {len(result)} rows to: {output_path}")

    if settings.include_price_history:
        combined_path = build_combined_growth(
            df,
            results,
            cfg.eod_csv,
            abbreviations,
            run_id,
            sampling=settings.price_history_sampling,
        )
        outputs["combined"] = {"path": combined_path}
    else:
        # Remove any history published by an earlier run that had it enabled.
        # Leaving the file behind would let the loader republish stale prices
        # as though they belonged to this run.
        stale = _growth_output_path(cfg.eod_csv, "_growth")
        if os.path.exists(stale):
            os.remove(stale)
            print(f"\nRemoved price history from a previous run: {stale}")
        outputs["combined"] = None

    if settings.include_universe_history:
        outputs["universe"] = {
            "path": build_universe_table(metadata, cfg.eod_csv),
            "table": cfg.universe_table,
        }
        outputs["universe_history"] = {
            "path": build_universe_history(
                universe_df,
                cfg.eod_csv,
                latest_date,
                sampling=settings.price_history_sampling,
            ),
            "table": cfg.universe_history_table,
        }
    else:
        # Same reasoning as the combined history above: a file left behind by
        # a run that had this enabled would be republished as current.
        for suffix, what in (("_universe_history", "history"), ("_universe", "lookup")):
            stale = _growth_output_path(cfg.eod_csv, suffix)
            if os.path.exists(stale):
                os.remove(stale)
                print(f"\nRemoved universe {what} from a previous run: {stale}")
        outputs["universe_history"] = None
        outputs["universe"] = None

    manifest.counts = counts
    manifest.outputs = outputs
    manifest.finish("success")
    manifest_path = manifest.write(
        os.path.join(cfg.data_dir, f"{cfg.prefix}_analysis_manifest.json")
    )
    print(f"\nManifest: {manifest_path}")
    return manifest


@click.command(help="Analyze EOD price data for growth over trailing windows")
@click.option(
    "--exchange",
    required=True,
    type=click.Choice(list(EXCHANGE_SUFFIXES), case_sensitive=False),
    help="Exchange code (e.g., US, ASX, NSE)",
)
@click.option(
    "--instrument-type",
    required=True,
    type=click.Choice(INSTRUMENTS, case_sensitive=False),
    help="Instrument type (e.g., stocks, etf)",
)
@click.option(
    "--allow-stale",
    is_flag=True,
    help="Screen the data even if it is older than max_data_age_days",
)
def main(exchange, instrument_type, allow_stale):
    load_dotenv()
    try:
        cfg = load_config(exchange, instrument_type)
        analyze_stocks(cfg, allow_stale=allow_stale)
    except StaleDataError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
