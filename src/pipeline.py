"""Publish screen results to SQLite.

Replaces the SQLite half of ``scripts/run_analysis.sh``. Doing it in Python
rather than shelling out removes three dependencies from a deployment image --
the ``sqlite3`` CLI, ``sqlite-utils`` and Bash 4.4+ -- and keeps the publication
rules (declared schema, atomic swap, VACUUM) in one testable place.

The database is built in a temporary file and moved into place only when
complete, so a reader never sees a half-populated database.
"""

import csv
import logging
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import GROWTH_COLUMN_TYPES, StockConfig

logger = logging.getLogger(__name__)

GROWTH_TABLE_SQL = ", ".join(f'"{name}" {sql}' for name, sql in GROWTH_COLUMN_TYPES)

# Columns whose declared type should be honoured when loading a CSV; anything
# else is stored as text.
_NUMERIC = {name for name, sql in GROWTH_COLUMN_TYPES if sql in {"FLOAT", "INTEGER"}}
_INTEGER = {name for name, sql in GROWTH_COLUMN_TYPES if sql == "INTEGER"}


def _coerce(column: str, value: str):
    """Convert a CSV field according to the declared column type."""
    if value == "":
        return None
    if column not in _NUMERIC:
        return value
    try:
        return int(float(value)) if column in _INTEGER else float(value)
    except ValueError:
        return None


def load_csv(
    conn: sqlite3.Connection, csv_path: str, table: str, schema_sql: str | None = None
) -> int:
    """Create ``table`` and load ``csv_path`` into it.

    The table is always created from a declared schema when one is given, so
    its column types do not change between an empty and a populated run.

    Returns:
        Number of rows inserted.

    Raises:
        FileNotFoundError: the CSV is missing.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Expected output missing: {csv_path}")

    with open(csv_path, newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"{csv_path} has no header row") from None

        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        if schema_sql:
            conn.execute(f'CREATE TABLE "{table}" ({schema_sql})')
            columns = [name for name, _ in GROWTH_COLUMN_TYPES]
        else:
            columns = header
            conn.execute(
                f'CREATE TABLE "{table}" (' + ", ".join(f'"{c}" TEXT' for c in columns) + ")"
            )

        placeholders = ", ".join("?" for _ in columns)
        insert = (
            f'INSERT INTO "{table}" ('
            + ", ".join(f'"{c}"' for c in columns)
            + f") VALUES ({placeholders})"
        )

        index = {name: position for position, name in enumerate(header)}
        rows = 0
        batch = []
        for record in reader:
            batch.append(
                tuple(_coerce(c, record[index[c]]) if c in index else None for c in columns)
            )
            if len(batch) >= 5000:
                conn.executemany(insert, batch)
                rows += len(batch)
                batch = []
        if batch:
            conn.executemany(insert, batch)
            rows += len(batch)

    return rows


def build_consistent_growth(conn: sqlite3.Connection, prefix: str, labels: list[str]) -> int:
    """Create ``consistent_growth_stocks`` from month-scale windows.

    Returns:
        Row count, or 0 when there are no eligible windows.
    """
    conn.execute("DROP TABLE IF EXISTS consistent_growth_stocks")
    if not labels:
        logger.info("  Skipping consistent_growth_stocks: no month-scale windows")
        return 0

    intersect = " INTERSECT ".join(
        f'SELECT ticker FROM "{prefix}_growth_{label}"' for label in labels
    )
    conn.execute(
        f"""
        CREATE TABLE consistent_growth_stocks AS
          SELECT ticker, name, exchange, pct_change AS pct_change_shortest_window,
                 data_as_of, run_id
          FROM "{prefix}_growth_{labels[-1]}"
          WHERE ticker IN ({intersect})
          ORDER BY ticker
        """
    )
    return conn.execute("SELECT COUNT(*) FROM consistent_growth_stocks").fetchone()[0]


def publish(cfg: StockConfig) -> str:
    """Build the database from this run's CSVs and publish it atomically.

    Returns:
        Path to the published database.
    """
    os.makedirs(os.path.dirname(cfg.db_path), exist_ok=True)
    temp_db = f"{cfg.db_path}.building.{os.getpid()}"
    if os.path.exists(temp_db):
        os.remove(temp_db)

    try:
        conn = sqlite3.connect(temp_db)
        try:
            for label in cfg.growth_labels:
                rows = load_csv(
                    conn,
                    cfg.growth_csv(label),
                    f"{cfg.prefix}_growth_{label}",
                    GROWTH_TABLE_SQL,
                )
                logger.info(f"  Loaded {rows} rows -> {cfg.prefix}_growth_{label}")

            if cfg.analysis.include_price_history:
                rows = load_csv(conn, cfg.combined_growth_csv, f"{cfg.prefix}_growth")
                logger.info(f"  Loaded {rows} rows -> {cfg.prefix}_growth")
            else:
                # Drop history left by an earlier run that had it enabled.
                conn.execute(f'DROP TABLE IF EXISTS "{cfg.prefix}_growth"')
                logger.info("  Skipping price history (include_price_history: false)")

            count = build_consistent_growth(conn, cfg.prefix, cfg.consistent_growth_labels)
            logger.info(
                f"  consistent_growth_stocks: {count} rows "
                f"(windows: {' '.join(cfg.consistent_growth_labels) or 'none'})"
            )
            conn.commit()

            # Bulk inserts leave a large freelist; reclaim it before publishing.
            logger.info("  Compacting")
            conn.execute("VACUUM")
        finally:
            conn.close()

        os.replace(temp_db, cfg.db_path)
        logger.info(f"  DB published: {cfg.db_path}")
        return cfg.db_path
    except Exception:
        if os.path.exists(temp_db):
            os.remove(temp_db)
        raise


def upload_to_s3(db_path: str, bucket: str, region: str | None = None) -> str:
    """Upload the published database to S3.

    Raises:
        RuntimeError: boto3 is not installed.
    """
    try:
        import boto3
    except ImportError:
        raise RuntimeError("S3 upload needs boto3: uv pip install boto3") from None

    bucket = bucket.removeprefix("s3://").rstrip("/")
    key = os.path.basename(db_path)
    client = boto3.client("s3", region_name=region) if region else boto3.client("s3")
    client.upload_file(db_path, bucket, key)
    return f"s3://{bucket}/{key}"
