"""Identify tickers whose price grew materially over a set of trailing windows.

Growth for a window is the percentage change between a *robust* opening and
closing price, subject to eligibility rules that keep the output tradeable:

  coverage   the ticker must actually span most of the window. Without this a
             stock listed two weeks ago is reported in the 1-year table with
             its two-week return.
  freshness  the ticker must still be trading as of the latest date in the
             dataset, so suspended or delisted names are not surfaced.
  liquidity  median daily volume must clear a floor, since a percentage move
             in a name that trades a few hundred shares is not realisable.
  price      the latest price must clear a floor, denominated in the
             exchange's own currency (see min_price in the config).

Endpoints are the median of the first and last N trading days rather than a
single close, so one bad print cannot define a whole window's return.
"""

import os
import sys
from typing import Dict, List, Optional

import click
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import EXCHANGE_SUFFIXES, INSTRUMENTS, AnalysisSettings, load_config

# Maps analysis window length (months) to the Google Finance chart parameter.
GF_WINDOW = {12: "1Y", 6: "6M", 3: "3M", 1: "1M"}

# A ticker with no prints in this many calendar days is treated as no longer
# trading and is excluded regardless of how well it performed.
MAX_STALENESS_DAYS = 10

REQUIRED_COLUMNS = {"ticker", "name", "close", "stock_price_date"}

pd.set_option("display.max_columns", None)
pd.set_option("display.max_rows", None)
pd.set_option("display.width", 1000)
pd.set_option("display.max_colwidth", 1000)


def load_price_data(file_path: str) -> pd.DataFrame:
    """Read and normalise the EOD price CSV.

    Raises:
        FileNotFoundError: the CSV does not exist.
        ValueError: required columns are missing.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Price data not found: {file_path}")

    df = pd.read_csv(file_path)

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"{file_path} is missing required column(s): {', '.join(sorted(missing))}"
        )

    df["stock_price_date"] = pd.to_datetime(df["stock_price_date"])

    # Prefer the split/dividend adjusted series. fetch_prices.py guarantees
    # adj_close is genuinely adjusted; close is only equivalent while
    # yfinance's auto_adjust default holds, so do not rely on it here.
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

    # Endpoint selection below relies on rows being in date order per ticker.
    return df.sort_values(["ticker", "stock_price_date"], kind="mergesort")


def _endpoint_prices(window_df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Median price over the first and last ``n`` trading days per ticker."""
    grouped = window_df.groupby("ticker", sort=False)
    first = grouped.head(n).groupby("ticker", sort=False)["price"].median()
    last = grouped.tail(n).groupby("ticker", sort=False)["price"].median()
    return pd.DataFrame({"first_price": first, "last_price": last})


def compute_window_growth(
    df: pd.DataFrame,
    months: int,
    threshold: float,
    settings: AnalysisSettings,
    latest_date: pd.Timestamp,
    exchange: str,
) -> pd.DataFrame:
    """Return the growth table for one trailing window.

    Args:
        df: normalised price data for all tickers.
        months: window length in months.
        threshold: minimum percentage change to qualify.
        settings: eligibility thresholds.
        latest_date: most recent date present in ``df``.
        exchange: exchange code, used to build Google Finance links.

    Returns:
        Qualifying tickers sorted by percentage change, descending.
    """
    cutoff = latest_date - pd.DateOffset(months=months)
    window_df = df[df["stock_price_date"] >= cutoff]
    if window_df.empty:
        return pd.DataFrame()

    window_days = (latest_date - cutoff).days
    grouped = window_df.groupby("ticker", sort=False)

    stats = grouped.agg(
        name=("name", "first"),
        first_date=("stock_price_date", "min"),
        last_date=("stock_price_date", "max"),
        observations=("price", "size"),
        median_volume=("volume", "median"),
    )
    stats = stats.join(_endpoint_prices(window_df, settings.endpoint_window))

    stats["days_covered"] = (stats["last_date"] - stats["first_date"]).dt.days
    stats["coverage"] = stats["days_covered"] / window_days
    stats["staleness_days"] = (latest_date - stats["last_date"]).dt.days

    eligible = (
        (stats["coverage"] >= settings.min_coverage)
        & (stats["staleness_days"] <= MAX_STALENESS_DAYS)
        & (stats["median_volume"] >= settings.min_median_volume)
        & (stats["last_price"] >= settings.min_price)
        # Guards against a divide-by-zero producing an infinite return.
        & (stats["first_price"] > 0)
        # At least two observations, else first and last are the same print.
        & (stats["observations"] >= 2)
    )
    result = stats[eligible].copy()
    if result.empty:
        return pd.DataFrame()

    result["pct_change"] = (
        (result["last_price"] - result["first_price"]) / result["first_price"] * 100
    ).round(2)
    result = result[result["pct_change"] > threshold]
    if result.empty:
        return pd.DataFrame()

    result = result.reset_index().rename(columns={"last_price": "latest_price"})
    result["first_price"] = result["first_price"].round(4)
    result["latest_price"] = result["latest_price"].round(4)
    result["coverage"] = result["coverage"].round(3)
    result["median_volume"] = result["median_volume"].round(0)
    result["first_date"] = result["first_date"].dt.strftime("%Y-%m-%d")
    result["last_date"] = result["last_date"].dt.strftime("%Y-%m-%d")
    result["google_finance"] = (
        "https://www.google.com/finance/beta/quote/"
        + result["ticker"].str.upper()
        + ":"
        + exchange.upper()
        + "?window="
        + GF_WINDOW.get(months, "1Y")
    )

    columns = [
        "ticker",
        "name",
        "first_date",
        "first_price",
        "last_date",
        "latest_price",
        "pct_change",
        "observations",
        "days_covered",
        "coverage",
        "median_volume",
        "google_finance",
    ]
    return result[columns].sort_values("pct_change", ascending=False)


def _log_exclusions(
    df: pd.DataFrame,
    months: int,
    settings: AnalysisSettings,
    latest_date: pd.Timestamp,
    label: str,
) -> None:
    """Report how many tickers each eligibility rule removed.

    Silent filtering reads as "nothing qualified" when the real cause is a
    threshold, so the counts are printed on every run.
    """
    cutoff = latest_date - pd.DateOffset(months=months)
    window_df = df[df["stock_price_date"] >= cutoff]
    if window_df.empty:
        return
    window_days = (latest_date - cutoff).days
    grouped = window_df.groupby("ticker", sort=False)
    stats = grouped.agg(
        first_date=("stock_price_date", "min"),
        last_date=("stock_price_date", "max"),
        median_volume=("volume", "median"),
    )
    coverage = (stats["last_date"] - stats["first_date"]).dt.days / window_days
    stale = (latest_date - stats["last_date"]).dt.days
    short = int((coverage < settings.min_coverage).sum())
    inactive = int((stale > MAX_STALENESS_DAYS).sum())
    illiquid = int((stats["median_volume"] < settings.min_median_volume).sum())
    print(
        f"  eligibility ({label}): {len(stats)} tickers in window; "
        f"excluded {short} short-history, {inactive} not currently trading, "
        f"{illiquid} illiquid"
    )


def _growth_output_path(eod_path: str, suffix: str) -> str:
    """Derive a growth CSV path from the EOD CSV path."""
    stem, extension = os.path.splitext(eod_path)
    return f"{stem}{suffix}{extension or '.csv'}"


def build_combined_growth(
    df: pd.DataFrame,
    results: Dict[str, pd.DataFrame],
    file_path: str,
    abbreviations: Dict[str, str],
) -> Optional[str]:
    """Write full price history for every ticker that grew in any window.

    Each row carries ``growth_count`` (how many windows the ticker qualified
    in) and ``growth_periods`` (which ones). Built with pandas rather than awk
    so that the comma-separated period list and names containing commas are
    quoted correctly on the way out.

    Args:
        df: normalised price data for all tickers.
        results: per-window growth tables, keyed by window label.
        file_path: path of the EOD CSV the outputs sit alongside.
        abbreviations: window label -> short form used in growth_periods.
    """
    flags: Dict[str, List[str]] = {}
    for label, result in results.items():
        if result.empty:
            continue
        short_label = abbreviations.get(label, label)
        for ticker in result["ticker"]:
            flags.setdefault(ticker, []).append(short_label)

    if not flags:
        return None

    # Preserve the window ordering from the config rather than insertion order.
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
    combined["stock_price_date"] = combined["stock_price_date"].dt.strftime("%Y-%m-%d")

    output_path = _growth_output_path(file_path, "_growth")
    combined.to_csv(output_path, index=False)
    print(
        f"\nCombined growth history: {len(combined):,} rows for "
        f"{len(summary):,} tickers -> {output_path}"
    )
    return output_path


def analyze_stocks(file_path: str, exchange: str, settings: AnalysisSettings) -> None:
    """Run every configured window and write one CSV per window."""
    df = load_price_data(file_path)
    if df.empty:
        print(f"No usable price rows in {file_path}")
        return

    latest_date = df["stock_price_date"].max()
    print(f"Loaded {len(df):,} rows for {df['ticker'].nunique():,} tickers")
    print(f"Latest price date: {latest_date:%Y-%m-%d}")

    # Window label -> short form used in the growth_periods column, in the
    # order the windows are configured.
    abbreviations = {
        str(w["label"]): GF_WINDOW.get(int(w["months"]), str(w["label"]))
        for w in settings.windows
    }

    results: Dict[str, pd.DataFrame] = {}
    for window in settings.windows:
        months = int(window["months"])
        label = str(window["label"])
        threshold = float(window["threshold"])

        print(f"\n--- Tickers with >{threshold}% growth over the last {label} ---")
        _log_exclusions(df, months, settings, latest_date, label)

        result = compute_window_growth(
            df, months, threshold, settings, latest_date, exchange
        )
        results[label] = result

        if result.empty:
            print("None found.")
            continue

        print(result.to_string(index=False))
        output_path = _growth_output_path(file_path, f"_growth_{label}")
        result.to_csv(output_path, index=False)
        print(f"Saved {len(result)} rows to: {output_path}")

    build_combined_growth(df, results, file_path, abbreviations)


@click.command(help="Analyze EOD price data for growth over trailing windows")
@click.option(
    "--exchange",
    required=True,
    type=click.Choice(list(EXCHANGE_SUFFIXES), case_sensitive=False),
    help="Exchange code (e.g., NSE, ASX, NASDAQ)",
)
@click.option(
    "--instrument-type",
    required=True,
    type=click.Choice(INSTRUMENTS, case_sensitive=False),
    help="Instrument type (e.g., stocks, etf)",
)
def main(exchange, instrument_type):
    try:
        cfg = load_config(exchange, instrument_type)
        analyze_stocks(cfg.eod_csv, cfg.exchange, cfg.analysis)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
