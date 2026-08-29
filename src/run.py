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

EXIT_OK, EXIT_ERROR, EXIT_STALE, EXIT_PARTIAL = 0, 1, 2, 3

# Jobs that build a database. Only these have anything to upload; the upload
# is gated on membership so a `fetch` cannot republish a database left over
# from an earlier run.
PUBLISHING_JOBS = ("analyze", "publish", "all")

logger = logging.getLogger(__name__)


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
@click.argument("job", type=click.Choice(["fetch", "analyze", "publish", "all"]))
@click.option("--exchange", required=True, type=click.Choice(list(EXCHANGE_SUFFIXES), False))
@click.option("--instrument-type", required=True, type=click.Choice(INSTRUMENTS, False))
# 400, not 365: under return_basis: google_finance a window opens at the last
# session on or before its calendar anchor, so the 1-year window needs history
# reaching *past* that anchor. At exactly 365 it opens short whenever the
# anchor falls on a weekend or holiday.
@click.option("--period", type=int, default=400, help="Days of history to fetch")
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

        if job in ("fetch", "all"):
            _fetch(cfg, period, batch_size, min_success_ratio, allow_partial)
        if job in ("analyze", "all"):
            analyze_stocks(cfg, allow_stale=allow_stale)
        if job in PUBLISHING_JOBS:
            pipeline.publish(cfg)

        # --upload forces publication; S3_AUTO_UPLOAD=true makes it routine.
        # Both are ignored by a job that built nothing: uploading then would
        # send whatever database an earlier run happened to leave behind,
        # stamped with that run's run_id and data_as_of.
        if job not in PUBLISHING_JOBS:
            if upload:
                raise RuntimeError(f"--upload given but the {job!r} job builds no database")
            logger.info(f"Skipping S3 upload (the {job!r} job builds no database)")
        elif upload or pipeline.should_auto_upload():
            bucket = os.environ.get("S3_BUCKET")
            if not bucket:
                raise RuntimeError("--upload given but S3_BUCKET is not set")
            target = pipeline.upload_to_s3(cfg.db_path, bucket, os.environ.get("S3_REGION"))
            logger.info(f"Uploaded to {target}")
        else:
            # Say so out loud. Silence here is indistinguishable from an upload
            # that ran and failed quietly, which is exactly the wrong ambiguity
            # to leave in a container log.
            reason = (
                "S3_AUTO_UPLOAD is not set"
                if os.environ.get("S3_BUCKET")
                else "S3_BUCKET is not set"
            )
            logger.info(f"Skipping S3 upload ({reason}; pass --upload to force)")

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
