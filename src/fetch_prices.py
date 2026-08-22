"""Fetch historical end-of-day market data from Yahoo Finance.

Tickers are downloaded in batches rather than one request per symbol, then a
single retry pass re-attempts whatever failed. Prices are stored unadjusted
(``close``) alongside the split/dividend adjusted series (``adj_close``);
downstream analysis uses the adjusted one.
"""

import functools
import logging
import logging.handlers
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import click
import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFException, YFRateLimitError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    EXCHANGE_SUFFIXES,
    INSTRUMENTS,
    PROJECT_ROOT,
    StockConfig,
    load_config,
    load_dotenv,
)
from runmeta import RunManifest, atomic_write_csv, code_revision, new_run_id
from universe import default_asset_type_for, filter_universe, load_universe

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 1000)
pd.set_option("display.max_colwidth", 1000)

# Number of symbols per Yahoo Finance request. Yahoo accepts large batches but
# a failure costs the whole batch, so this trades throughput against blast
# radius.
DEFAULT_BATCH_SIZE = 100

# Only transient faults are worth retrying. A delisted or misspelled symbol
# fails identically every time, so retrying it just multiplies the wait.
TRANSIENT_ERRORS = (
    YFRateLimitError,
    ConnectionError,
    TimeoutError,
    OSError,  # covers socket errors and most requests/urllib3 transport failures
)

# Provider-side faults a batch can legitimately recover from. Deliberately
# excludes bare ValueError and KeyError: those are raised throughout pandas and
# this codebase, so treating them as recoverable turns a response-shape change
# or a normalisation bug into "these tickers failed" and publishes a partial
# dataset that hides the defect.
RECOVERABLE_ERRORS = TRANSIENT_ERRORS + (YFException,)

PRICE_COLUMNS = ["Open", "High", "Low", "Close", "Adj Close"]

# Characters the exchange directory uses to separate a share class or preferred
# series from the root symbol. Yahoo Finance uses a hyphen for all of them.
_SHARE_CLASS_SEPARATORS = re.compile(r"[./$]")

# Local timezone of each exchange, used to decide which calendar date the
# market is currently on. A host in Australia fetching NASDAQ is already a day
# ahead, so a host-local boundary would request a session that has not started.
EXCHANGE_TIMEZONES = {
    "US": "America/New_York",
    "NASDAQ": "America/New_York",
    "NYSE": "America/New_York",
    "ASX": "Australia/Sydney",
    "NSE": "Asia/Kolkata",
    "BSE": "Asia/Kolkata",
}

# Recorded per row so downstream code can tell verified adjusted prices from a
# raw-close fallback. Screening treats the two differently.
BASIS_ADJUSTED = "adjusted"
BASIS_RAW_FALLBACK = "raw_fallback"


class PartialFetchError(RuntimeError):
    """Too few tickers returned data to publish the result as a dataset."""


logger = logging.getLogger(__name__)


def setup_logging(
    log_level: str = "INFO",
    log_file: str | None = None,
    log_format: str = "%(asctime)s - %(name)s - %(filename)s:%(lineno)d - %(levelname)s - %(message)s",
) -> None:
    """Configure root logging to the console and, optionally, a rotating file."""
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {log_level}")

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    formatter = logging.Formatter(log_format)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    if log_file:
        log_dir = os.path.dirname(os.path.abspath(log_file))
        os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # Third-party libraries are noisy at INFO.
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.ERROR)


def retry(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type, ...] = TRANSIENT_ERRORS,
):
    """Retry a function on transient errors with exponential backoff.

    Args:
        max_attempts: Total number of calls, including the first.
        delay: Seconds to wait before the second attempt.
        backoff: Multiplier applied to the delay after each failure.
        exceptions: Exception types considered transient.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            current_delay = delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    if attempt == max_attempts:
                        logger.error(f"{func.__name__} failed after {max_attempts} attempts: {exc}")
                        raise
                    logger.warning(
                        f"{func.__name__} attempt {attempt}/{max_attempts} failed "
                        f"({exc}); retrying in {current_delay:.1f}s"
                    )
                    time.sleep(current_delay)
                    current_delay *= backoff

        return wrapper

    return decorator


def _chunk(items: Sequence, size: int) -> Iterable[Sequence]:
    """Yield consecutive slices of ``items`` of at most ``size`` elements."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


class BaseDataCollector(ABC):
    """Common structure for EOD data collectors.

    Handles ticker loading, batching, failure accounting and persistence.
    Subclasses implement :meth:`_fetch_batch` to talk to a specific source.
    """

    def __init__(
        self,
        exchange: str,
        instrument_type: str,
        period: int = 365,
        batch_size: int = DEFAULT_BATCH_SIZE,
        run_id: str = "",
    ):
        """
        Args:
            exchange: Exchange code (e.g., 'US', 'ASX').
            instrument_type: Instrument type (e.g., 'stocks', 'etf').
            period: Days of history to fetch.
            batch_size: Symbols per request.
            run_id: Provenance id; generated when omitted.
        """
        self.config: StockConfig = load_config(exchange, instrument_type)
        self.exchange = self.config.exchange
        self.instrument_type = self.config.instrument_type
        self.period = period
        self.batch_size = max(1, batch_size)
        self.run_id = run_id or new_run_id()
        self.failed: list[tuple[str, str, str]] = []
        self.requested_count = 0
        self.success_count = 0
        self.universe_total = 0
        self.started_at = datetime.now(UTC).isoformat()

        # Created up front so the error report can always be written, even when
        # every ticker fails and no price CSV is produced.
        os.makedirs(self.config.data_dir, exist_ok=True)

    def fetch_historical_data(self) -> pd.DataFrame:
        """Fetch history for every ticker in the ticker file.

        Returns:
            One row per ticker per trading day, sorted by ticker then date.
        """
        now = datetime.now(UTC)
        exchange_now = now.astimezone(self._exchange_timezone())
        start_date = (exchange_now - timedelta(days=self.period)).strftime("%Y-%m-%d")
        # yfinance treats `end` as exclusive, so passing today drops today's
        # completed session. Request the day after the exchange's current date
        # and let the provider return whatever sessions have actually closed.
        # Using exchange-local rather than host-local time keeps the boundary
        # correct when the host has already rolled over to the next date.
        end_date = (exchange_now + timedelta(days=1)).strftime("%Y-%m-%d")
        current_time = now.strftime("%Y-%m-%d %H:%M:%S")

        logger.info(f"Starting data fetch from {start_date} to {end_date} (end exclusive)")
        logger.info(f"Exchange: {self.exchange}, Instrument type: {self.instrument_type}")

        tickers = self._read_ticker_file()
        names = dict(tickers)
        symbols = [symbol for symbol, _ in tickers]

        frames, failed = self._fetch_all(symbols, names, start_date, end_date, current_time)

        # Transient faults can take out a whole batch; give those symbols one
        # more pass in smaller batches before declaring them failed.
        if failed:
            logger.info(f"Retrying {len(failed)} failed tickers in smaller batches")
            retry_frames, failed = self._fetch_all(
                [symbol for symbol, _, _ in failed],
                names,
                start_date,
                end_date,
                current_time,
                batch_size=max(1, self.batch_size // 10),
            )
            frames.extend(retry_frames)

        # Always rewrite the failure report, even when empty. Skipping it left
        # a previous run's failures on disk looking like this run's.
        error_df = pd.DataFrame(
            [
                (symbol, names.get(symbol, symbol), reason, detail, self.run_id)
                for symbol, reason, detail in failed
            ],
            columns=["ticker", "name", "error_type", "detail", "run_id"],
        )
        atomic_write_csv(error_df, self.config.error_csv)
        if failed:
            logger.warning(f"Failed to fetch data for {len(failed)}/{len(symbols)} tickers")
            logger.info(f"Error tickers saved to {self.config.error_csv}")

        if not frames:
            logger.warning("No data was fetched")
            logger.info(f"FETCH SUMMARY: 0/{len(symbols)} tickers fetched successfully")
            self.requested_count = len(symbols)
            self.success_count = 0
            return pd.DataFrame()

        df_all = pd.concat(frames, ignore_index=True)
        df_all = df_all.sort_values(["ticker", "stock_price_date"], kind="mergesort")

        success_count = df_all["ticker"].nunique()
        self.requested_count = len(symbols)
        self.success_count = int(success_count)
        logger.info(f"FETCH SUMMARY: {success_count}/{len(symbols)} tickers fetched successfully")
        if failed:
            logger.info(f"ERROR FILE: {self.config.error_csv} ({len(failed)} failed tickers)")
        self.failed = failed

        return df_all

    def _fetch_all(
        self,
        symbols: Sequence[str],
        names: dict,
        start_date: str,
        end_date: str,
        current_time: str,
        batch_size: int | None = None,
    ) -> tuple[list[pd.DataFrame], list[tuple[str, str, str]]]:
        """Fetch ``symbols`` in batches.

        Returns:
            (frames, failures as (symbol, error_type, detail))
        """
        batch_size = batch_size or self.batch_size
        batches = list(_chunk(list(symbols), batch_size))
        frames: list[pd.DataFrame] = []
        failed: list[tuple[str, str, str]] = []

        for index, batch in enumerate(batches, start=1):
            logger.info(f"Batch {index}/{len(batches)} ({len(batch)} tickers)")
            try:
                batch_frames, batch_failed = self._fetch_batch(
                    batch, names, start_date, end_date, current_time
                )
                frames.extend(batch_frames)
                failed.extend(batch_failed)
            except RECOVERABLE_ERRORS as exc:
                logger.error(f"Batch {index} failed entirely: {exc}")
                failed.extend((symbol, type(exc).__name__, str(exc)[:200]) for symbol in batch)
            except Exception:
                # An unexpected exception is a defect, not a ticker failure.
                # Publishing a partial dataset would hide it.
                logger.exception(f"Batch {index} raised an unexpected error; aborting run")
                raise

        return frames, failed

    def _read_ticker_file(self) -> list[tuple[str, str]]:
        """Load the universe, restricted to the configured asset types."""
        universe = load_universe(
            self.config.ticker_file,
            default_asset_type=default_asset_type_for(self.config.instrument_type),
        )
        wanted = self.config.analysis.asset_types
        screened = filter_universe(universe, wanted)

        self.universe_total = len(universe)
        excluded = len(universe) - len(screened)
        logger.info(
            f"Universe: {len(screened)} of {len(universe)} instruments match "
            f"{wanted} ({excluded} excluded) in {self.config.ticker_file}"
        )
        return list(zip(screened["ticker"], screened["name"], strict=True))

    @abstractmethod
    def _fetch_batch(
        self,
        symbols: Sequence[str],
        names: dict,
        start_date: str,
        end_date: str,
        current_time: str,
    ) -> tuple[list[pd.DataFrame], list[tuple[str, str, str]]]:
        """Fetch one batch of symbols.

        Returns:
            (frames, failures as (symbol, error_type, detail))
        """

    @property
    def success_ratio(self) -> float:
        """Fraction of requested tickers that returned data."""
        if not self.requested_count:
            return 0.0
        return self.success_count / self.requested_count

    def assert_fetch_is_complete(self, min_ratio: float, allow_partial: bool = False) -> None:
        """Refuse to publish a materially incomplete dataset.

        Screening is cross-sectional: a run missing a third of the universe can
        look entirely plausible while omitting most of the candidates, so
        completeness of the population is part of correctness rather than an
        operational detail.

        Raises:
            PartialFetchError: the success ratio is below ``min_ratio``.
        """
        if self.success_ratio >= min_ratio or allow_partial:
            return
        raise PartialFetchError(
            f"Only {self.success_count}/{self.requested_count} tickers "
            f"({self.success_ratio:.1%}) returned data, below the required "
            f"{min_ratio:.1%}. The previous price file has been left untouched. "
            f"Re-run, or pass --allow-partial to publish anyway."
        )

    def write_manifest(
        self,
        status: str,
        error: str | None = None,
        eod_path: str | None = None,
        data_as_of: str | None = None,
    ) -> str:
        """Record what this fetch did, so analysis can verify its lineage."""
        manifest = RunManifest(
            run_id=self.run_id,
            stage="fetch",
            exchange=self.exchange,
            instrument_type=self.instrument_type,
            started_at=self.started_at,
            code_revision=code_revision(PROJECT_ROOT),
            data_as_of=data_as_of,
            universe_file=self.config.ticker_file,
            universe_total=self.universe_total,
            universe_screened=self.requested_count,
            counts={
                "requested": self.requested_count,
                "succeeded": self.success_count,
                "failed": len(self.failed),
                "success_ratio": round(self.success_ratio, 4),
            },
            outputs={"eod_csv": eod_path, "error_csv": self.config.error_csv},
        )
        manifest.finish(status, error)
        return manifest.write(
            os.path.join(self.config.data_dir, f"{self.config.prefix}_fetch_manifest.json")
        )

    def save_data(self, data: pd.DataFrame, filename: str | None = None) -> str:
        """Write the fetched data to CSV and return the path ("" if empty)."""
        if data.empty:
            logger.warning("No data to save")
            return ""

        filepath = os.path.join(self.config.data_dir, filename) if filename else self.config.eod_csv
        atomic_write_csv(data, filepath)
        logger.info(f"Data saved to {filepath}")
        return filepath


class YahooFinanceDataFetcher(BaseDataCollector):
    """Fetches EOD data from Yahoo Finance in batched requests."""

    def _exchange_timezone(self):
        """Timezone of the exchange being fetched, defaulting to UTC."""
        name = EXCHANGE_TIMEZONES.get(self.exchange)
        if not name:
            return UTC
        try:
            return ZoneInfo(name)
        except Exception:
            logger.warning(f"Unknown timezone {name} for {self.exchange}; using UTC")
            return UTC

    def _yahoo_symbol(self, ticker: str) -> str:
        """Translate a directory symbol into Yahoo Finance's symbology.

        Share classes and preferred lines are written ``BF.B`` / ``ABR$D`` by
        the exchange directory but ``BF-B`` / ``ABR-D`` by Yahoo, which returns
        an empty frame for the directory form.
        """
        symbol = _SHARE_CLASS_SEPARATORS.sub("-", ticker)
        suffix = EXCHANGE_SUFFIXES[self.exchange]
        return f"{symbol}.{suffix}" if suffix else symbol

    def _fetch_batch(
        self,
        symbols: Sequence[str],
        names: dict,
        start_date: str,
        end_date: str,
        current_time: str,
    ) -> tuple[list[pd.DataFrame], list[tuple[str, str, str]]]:
        yahoo_symbols = [self._yahoo_symbol(symbol) for symbol in symbols]
        raw = self._download(yahoo_symbols, start_date, end_date)

        frames: list[pd.DataFrame] = []
        failed: list[str] = []

        for symbol, yahoo_symbol in zip(symbols, yahoo_symbols, strict=True):
            try:
                ticker_df = self._extract(raw, yahoo_symbol)
            except KeyError:
                ticker_df = None

            if ticker_df is None or ticker_df.empty:
                logger.warning(f"No data returned for {symbol}")
                failed.append((symbol, "NoData", "provider returned no rows"))
                continue

            frames.append(
                self._normalise(
                    ticker_df, symbol, names.get(symbol, symbol), current_time, self.run_id
                )
            )

        return frames, failed

    @retry(max_attempts=3, delay=2.0, backoff=2.0)
    def _download(
        self, yahoo_symbols: Sequence[str], start_date: str, end_date: str
    ) -> pd.DataFrame:
        """Download one batch, retrying only on transient transport errors."""
        return yf.download(
            tickers=list(yahoo_symbols),
            start=start_date,
            end=end_date,
            # Keep raw and adjusted prices distinct so adj_close is meaningful
            # rather than a copy of close.
            auto_adjust=False,
            group_by="ticker",
            threads=True,
            progress=False,
        )

    @staticmethod
    def _extract(raw: pd.DataFrame, yahoo_symbol: str) -> pd.DataFrame | None:
        """Pull one symbol's sub-frame out of a batched download."""
        if raw is None or raw.empty:
            return None

        if isinstance(raw.columns, pd.MultiIndex):
            if yahoo_symbol not in raw.columns.get_level_values(0):
                return None
            frame = raw[yahoo_symbol]
        else:
            # A single-symbol download can come back flat.
            frame = raw

        # Symbols Yahoo does not recognise still occupy columns, filled with NaN.
        return frame.dropna(how="all")

    @staticmethod
    def _normalise(
        frame: pd.DataFrame,
        ticker: str,
        name: str,
        current_time: str,
        run_id: str = "",
    ) -> pd.DataFrame:
        """Standardise columns, round prices and attach identifying fields."""
        frame = frame.copy()

        # Older/edge responses omit Adj Close. Fall back to the raw close so
        # the column always exists, but record the substitution: an unadjusted
        # series spanning a split produces a badly wrong return, and must not
        # be indistinguishable from verified adjusted data.
        if "Adj Close" not in frame.columns and "Close" in frame.columns:
            frame["Adj Close"] = frame["Close"]
            basis = BASIS_RAW_FALLBACK
        else:
            basis = BASIS_ADJUSTED

        for column in PRICE_COLUMNS:
            if column in frame.columns:
                frame[column] = frame[column].round(4)

        frame.columns = [str(column).lower() for column in frame.columns]
        frame = frame.rename(columns={"adj close": "adj_close"})

        frame = frame.reset_index()
        frame = frame.rename(columns={"Date": "stock_price_date", "date": "stock_price_date"})
        frame["stock_price_date"] = pd.to_datetime(frame["stock_price_date"]).dt.strftime(
            "%Y-%m-%d"
        )

        frame.insert(1, "ticker", ticker)
        frame.insert(2, "name", name)
        frame.insert(3, "fetch_time", current_time)
        frame["price_basis"] = basis
        frame["fetch_run_id"] = run_id

        logger.debug(f"Fetched {len(frame)} rows for {ticker}")
        return frame


@click.command(help="Fetch historical market data from Yahoo Finance")
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
@click.option("--period", type=int, default=365, help="Number of days of historical data to fetch")
@click.option(
    "--batch-size",
    type=int,
    default=DEFAULT_BATCH_SIZE,
    help=f"Symbols per Yahoo Finance request (default: {DEFAULT_BATCH_SIZE})",
)
@click.option(
    "--min-success-ratio",
    type=float,
    default=0.95,
    help="Refuse to publish unless this fraction of tickers returned data",
)
@click.option(
    "--allow-partial",
    is_flag=True,
    help="Publish even when the success ratio is below the threshold",
)
@click.option("--log-file", default=None, help="Also write logs to this rotating file")
@click.option(
    "--log-level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
)
def main(
    exchange,
    instrument_type,
    period,
    batch_size,
    min_success_ratio,
    allow_partial,
    log_file,
    log_level,
):
    load_dotenv()
    setup_logging(log_level=log_level, log_file=log_file)

    if period < 1:
        click.echo("Error: --period must be a positive number of days", err=True)
        sys.exit(1)

    try:
        fetcher = YahooFinanceDataFetcher(
            exchange=exchange,
            instrument_type=instrument_type,
            period=period,
            batch_size=batch_size,
        )
        data = fetcher.fetch_historical_data()

        if data.empty:
            fetcher.write_manifest(status="failed", error="no data fetched")
            click.echo("No data fetched.", err=True)
            sys.exit(1)

        # Check completeness *before* overwriting the previous price file, so a
        # bad run leaves the last good dataset intact.
        try:
            fetcher.assert_fetch_is_complete(min_success_ratio, allow_partial)
        except PartialFetchError as exc:
            fetcher.write_manifest(status="partial", error=str(exc))
            click.echo(f"Error: {exc}", err=True)
            sys.exit(3)

        path = fetcher.save_data(data)
        fetcher.write_manifest(
            status="partial_published"
            if allow_partial and fetcher.success_ratio < min_success_ratio
            else "success",
            eod_path=path,
            data_as_of=str(data["stock_price_date"].max()),
        )
        click.echo(f"Data saved to: {path}")
    except PartialFetchError:
        raise
    except Exception as exc:
        logger.error(f"Error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
