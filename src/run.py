"""Single entry point for the whole pipeline.

Designed for containers, where one image runs a named job rather than a shell
script chaining others:

    python src/run.py all      --exchange US --instrument-type stocks
    python src/run.py fetch    --exchange ASX --instrument-type etf
    python src/run.py analyze  --exchange US --instrument-type stocks

Exit codes are stable so a scheduler can branch on them without parsing logs:
0 success, 1 error, 2 stale data, 3 incomplete fetch.
"""

import logging
import os
import sys

import click

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pipeline
from analysis import StaleDataError, analyze_stocks
from config import EXCHANGE_SUFFIXES, INSTRUMENTS, load_config, load_dotenv
from fetch_prices import PartialFetchError, YahooFinanceDataFetcher, setup_logging
from symbol_directory import US_EXCHANGES, fetch_symbol_directory
from universe import default_asset_type_for, refresh_universe, sync_universe

EXIT_OK, EXIT_ERROR, EXIT_STALE, EXIT_PARTIAL = 0, 1, 2, 3

logger = logging.getLogger(__name__)


def _sync(cfg) -> None:
    """Refresh universe membership, by directory where one exists."""
    cfg.ensure_universe()
    if cfg.exchange in US_EXCHANGES:
        logger.info(f"Syncing {cfg.exchange} universe from the symbol directory")
        summary = sync_universe(
            cfg.ticker_file,
            fetch_symbol_directory(),
            instrument_type=cfg.instrument_type,
            exchanges=US_EXCHANGES[cfg.exchange],
        )
        logger.info(
            f"  {summary['total']} symbols (+{len(summary['added'])} "
            f"-{len(summary['removed'])}, {summary['retained']} retained)"
        )
    else:
        # No bulk directory for this market; enrich in place and drop members
        # the provider no longer lists.
        logger.info(f"Enriching {cfg.exchange} universe via provider lookups")
        refresh_universe(
            cfg.ticker_file,
            EXCHANGE_SUFFIXES[cfg.exchange],
            default_asset_type=default_asset_type_for(cfg.instrument_type),
            prune=True,
        )


def _fetch(cfg, period: int, batch_size: int, min_ratio: float, allow_partial: bool) -> None:
    fetcher = YahooFinanceDataFetcher(
        exchange=cfg.exchange,
        instrument_type=cfg.instrument_type,
        period=period,
        batch_size=batch_size,
    )
    data = fetcher.fetch_historical_data()
    if data.empty:
        fetcher.write_manifest(status="failed", error="no data fetched")
        raise PartialFetchError("No data fetched")

    fetcher.assert_fetch_is_complete(min_ratio, allow_partial)
    path = fetcher.save_data(data)
    fetcher.write_manifest(
        status="success",
        eod_path=path,
        data_as_of=str(data["stock_price_date"].max()),
    )
    logger.info(f"Data saved to: {path}")


@click.command(help="Run one pipeline stage, or the whole thing")
@click.argument("job", type=click.Choice(["sync", "fetch", "analyze", "publish", "all"]))
@click.option("--exchange", required=True, type=click.Choice(list(EXCHANGE_SUFFIXES), False))
@click.option("--instrument-type", required=True, type=click.Choice(INSTRUMENTS, False))
@click.option("--period", type=int, default=365, help="Days of history to fetch")
@click.option("--batch-size", type=int, default=100)
@click.option("--min-success-ratio", type=float, default=0.95)
@click.option("--allow-partial", is_flag=True)
@click.option("--allow-stale", is_flag=True)
@click.option("--upload", is_flag=True, help="Upload the DB to $S3_BUCKET")
@click.option("--log-level", default="INFO", type=click.Choice(["DEBUG", "INFO", "WARNING"], False))
def main(
    job,
    exchange,
    instrument_type,
    period,
    batch_size,
    min_success_ratio,
    allow_partial,
    allow_stale,
    upload,
    log_level,
):
    load_dotenv()
    # Containers collect stdout; no rotating file handler is wanted there.
    setup_logging(log_level=log_level)

    try:
        cfg = load_config(exchange, instrument_type)

        if job in ("sync", "all"):
            _sync(cfg)
        if job in ("fetch", "all"):
            _fetch(cfg, period, batch_size, min_success_ratio, allow_partial)
        if job in ("analyze", "all"):
            analyze_stocks(cfg, allow_stale=allow_stale)
        if job in ("analyze", "publish", "all"):
            pipeline.publish(cfg)

        # --upload forces publication; S3_AUTO_UPLOAD=true makes it routine.
        if upload or pipeline.should_auto_upload():
            bucket = os.environ.get("S3_BUCKET")
            if not bucket:
                raise RuntimeError("--upload given but S3_BUCKET is not set")
            target = pipeline.upload_to_s3(cfg.db_path, bucket, os.environ.get("S3_REGION"))
            logger.info(f"Uploaded to {target}")

    except StaleDataError as exc:
        logger.error(str(exc))
        sys.exit(EXIT_STALE)
    except PartialFetchError as exc:
        logger.error(str(exc))
        sys.exit(EXIT_PARTIAL)
    except Exception as exc:
        logger.error(f"{type(exc).__name__}: {exc}")
        sys.exit(EXIT_ERROR)

    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
