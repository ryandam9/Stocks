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
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Sequence, Tuple

import click
import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFRateLimitError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import EXCHANGE_SUFFIXES, INSTRUMENTS, StockConfig, load_config

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

PRICE_COLUMNS = ["Open", "High", "Low", "Close", "Adj Close"]

logger = logging.getLogger(__name__)


def setup_logging(
    log_level: str = "INFO",
    log_file: Optional[str] = None,
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
    exceptions: Tuple[type, ...] = TRANSIENT_ERRORS,
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
                        logger.error(
                            f"{func.__name__} failed after {max_attempts} attempts: {exc}"
                        )
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
    ):
        """
        Args:
            exchange: Exchange code (e.g., 'NSE', 'NASDAQ').
            instrument_type: Instrument type (e.g., 'stocks', 'etf').
            period: Days of history to fetch.
            batch_size: Symbols per request.
        """
        self.config: StockConfig = load_config(exchange, instrument_type)
        self.exchange = self.config.exchange
        self.instrument_type = self.config.instrument_type
        self.period = period
        self.batch_size = max(1, batch_size)

        # Created up front so the error report can always be written, even when
        # every ticker fails and no price CSV is produced.
        os.makedirs(self.config.data_dir, exist_ok=True)

    def fetch_historical_data(self) -> pd.DataFrame:
        """Fetch history for every ticker in the ticker file.

        Returns:
            One row per ticker per trading day, sorted by ticker then date.
        """
        start_date = (datetime.now() - timedelta(days=self.period)).strftime("%Y-%m-%d")
        end_date = datetime.now().strftime("%Y-%m-%d")
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        logger.info(f"Starting data fetch from {start_date} to {end_date}")
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
                failed,
                names,
                start_date,
                end_date,
                current_time,
                batch_size=max(1, self.batch_size // 10),
            )
            frames.extend(retry_frames)

        if failed:
            logger.warning(f"Failed to fetch data for {len(failed)}/{len(symbols)} tickers")
            error_df = pd.DataFrame(
                [(symbol, names.get(symbol, symbol)) for symbol in failed],
                columns=["ticker", "name"],
            )
            error_df.to_csv(self.config.error_csv, index=False)
            logger.info(f"Error tickers saved to {self.config.error_csv}")

        if not frames:
            logger.warning("No data was fetched")
            logger.info(f"FETCH SUMMARY: 0/{len(symbols)} tickers fetched successfully")
            return pd.DataFrame()

        df_all = pd.concat(frames, ignore_index=True)
        df_all = df_all.sort_values(["ticker", "stock_price_date"], kind="mergesort")

        success_count = df_all["ticker"].nunique()
        logger.info(
            f"FETCH SUMMARY: {success_count}/{len(symbols)} tickers fetched successfully"
        )
        if failed:
            logger.info(f"ERROR FILE: {self.config.error_csv} ({len(failed)} failed tickers)")

        return df_all

    def _fetch_all(
        self,
        symbols: Sequence[str],
        names: dict,
        start_date: str,
        end_date: str,
        current_time: str,
        batch_size: Optional[int] = None,
    ) -> Tuple[List[pd.DataFrame], List[str]]:
        """Fetch ``symbols`` in batches, returning frames and failed symbols."""
        batch_size = batch_size or self.batch_size
        batches = list(_chunk(list(symbols), batch_size))
        frames: List[pd.DataFrame] = []
        failed: List[str] = []

        for index, batch in enumerate(batches, start=1):
            logger.info(f"Batch {index}/{len(batches)} ({len(batch)} tickers)")
            try:
                batch_frames, batch_failed = self._fetch_batch(
                    batch, names, start_date, end_date, current_time
                )
                frames.extend(batch_frames)
                failed.extend(batch_failed)
            except Exception as exc:
                logger.error(f"Batch {index} failed entirely: {exc}")
                failed.extend(batch)

        return frames, failed

    def _read_ticker_file(self) -> List[Tuple[str, str]]:
        """Parse the ``ticker~name`` file, skipping blanks and comments."""
        with open(self.config.ticker_file, "r") as handle:
            lines = handle.readlines()

        tickers = []
        seen = set()
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("~")
            symbol = parts[0].strip()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            tickers.append((symbol, parts[1].strip() if len(parts) > 1 else symbol))

        logger.info(f"Found {len(tickers)} tickers in {self.config.ticker_file}")
        return tickers

    @abstractmethod
    def _fetch_batch(
        self,
        symbols: Sequence[str],
        names: dict,
        start_date: str,
        end_date: str,
        current_time: str,
    ) -> Tuple[List[pd.DataFrame], List[str]]:
        """Fetch one batch of symbols.

        Returns:
            (frames for symbols that returned data, symbols that returned none)
        """

    def save_data(self, data: pd.DataFrame, filename: Optional[str] = None) -> str:
        """Write the fetched data to CSV and return the path ("" if empty)."""
        if data.empty:
            logger.warning("No data to save")
            return ""

        filepath = (
            os.path.join(self.config.data_dir, filename)
            if filename
            else self.config.eod_csv
        )
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        data.to_csv(filepath, index=False)
        logger.info(f"Data saved to {filepath}")
        return filepath


class YahooFinanceDataFetcher(BaseDataCollector):
    """Fetches EOD data from Yahoo Finance in batched requests."""

    def _yahoo_symbol(self, ticker: str) -> str:
        """Append the exchange suffix Yahoo Finance expects, if any."""
        suffix = EXCHANGE_SUFFIXES[self.exchange]
        return f"{ticker}.{suffix}" if suffix else ticker

    def _fetch_batch(
        self,
        symbols: Sequence[str],
        names: dict,
        start_date: str,
        end_date: str,
        current_time: str,
    ) -> Tuple[List[pd.DataFrame], List[str]]:
        yahoo_symbols = [self._yahoo_symbol(symbol) for symbol in symbols]
        raw = self._download(yahoo_symbols, start_date, end_date)

        frames: List[pd.DataFrame] = []
        failed: List[str] = []

        for symbol, yahoo_symbol in zip(symbols, yahoo_symbols):
            try:
                ticker_df = self._extract(raw, yahoo_symbol)
            except KeyError:
                ticker_df = None

            if ticker_df is None or ticker_df.empty:
                logger.warning(f"No data returned for {symbol}")
                failed.append(symbol)
                continue

            frames.append(
                self._normalise(ticker_df, symbol, names.get(symbol, symbol), current_time)
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
    def _extract(raw: pd.DataFrame, yahoo_symbol: str) -> Optional[pd.DataFrame]:
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
        frame: pd.DataFrame, ticker: str, name: str, current_time: str
    ) -> pd.DataFrame:
        """Standardise columns, round prices and attach identifying fields."""
        frame = frame.copy()

        # Older/edge responses omit Adj Close; fall back so the column always
        # exists, and record that it is unadjusted.
        if "Adj Close" not in frame.columns and "Close" in frame.columns:
            frame["Adj Close"] = frame["Close"]

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

        logger.debug(f"Fetched {len(frame)} rows for {ticker}")
        return frame


@click.command(help="Fetch historical market data from Yahoo Finance")
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
@click.option(
    "--period", type=int, default=365, help="Number of days of historical data to fetch"
)
@click.option(
    "--batch-size",
    type=int,
    default=DEFAULT_BATCH_SIZE,
    help=f"Symbols per Yahoo Finance request (default: {DEFAULT_BATCH_SIZE})",
)
@click.option("--log-file", default=None, help="Also write logs to this rotating file")
@click.option(
    "--log-level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
)
def main(exchange, instrument_type, period, batch_size, log_file, log_level):
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
            click.echo("No data fetched.", err=True)
            sys.exit(1)

        click.echo(f"Data saved to: {fetcher.save_data(data)}")
    except Exception as exc:
        logger.error(f"Error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
