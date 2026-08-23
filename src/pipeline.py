"""Publish screen results to SQLite.

Replaces the SQLite half of ``scripts/run_analysis.sh``. Doing it in Python
rather than shelling out removes three dependencies from a deployment image --
the ``sqlite3`` CLI, ``sqlite-utils`` and Bash 4.4+ -- and keeps the publication
rules (declared schema, atomic swap, VACUUM) in one testable place.

The database is built in a temporary file and moved into place only when
complete, so a reader never sees a half-populated database.
"""

import csv
import json
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
                 threshold AS threshold_shortest_window,
                 data_as_of, run_id
          FROM "{prefix}_growth_{labels[-1]}"
          WHERE ticker IN ({intersect})
          ORDER BY ticker
        """
    )
    return conn.execute("SELECT COUNT(*) FROM consistent_growth_stocks").fetchone()[0]


def load_manifest_tables(conn: sqlite3.Connection, manifest_path: str) -> int:
    """Embed the run's provenance manifest into the database as two tables.

    The manifest is written next to the CSVs, on a volume that is ephemeral in
    a container -- so on Fargate it is discarded with the task. Publishing it
    as a separate object would mean fetching a second file to answer questions
    about the first; carrying it inside the database means the receipt travels
    with the data and is queryable where the data already is.

    ``run_metadata`` holds one row: which commit ran, against what, when, and
    under which settings. ``screen_funnel`` holds the per-window attrition that
    makes an empty result explainable -- whether nothing qualified or a filter
    removed everything.

    Returns:
        Number of funnel rows written, or 0 when there is no manifest.
    """
    conn.execute("DROP TABLE IF EXISTS run_metadata")
    conn.execute("DROP TABLE IF EXISTS screen_funnel")
    conn.execute(
        """
        CREATE TABLE run_metadata (
          run_id TEXT, code_revision TEXT, exchange TEXT, instrument_type TEXT,
          data_as_of TEXT, started_at TEXT, finished_at TEXT, status TEXT,
          universe_total INTEGER, universe_screened INTEGER, provider TEXT,
          source_run_id TEXT, source_status TEXT, settings_json TEXT
        )
        """
    )
    conn.execute(
        'CREATE TABLE screen_funnel ("window" TEXT, position INTEGER, stage TEXT, count INTEGER)'
    )

    if not os.path.exists(manifest_path):
        # A publish without a preceding analyze in the same run. The tables are
        # created empty rather than skipped, so a consumer's query still works.
        logger.info("  No analysis manifest; run_metadata and screen_funnel are empty")
        return 0

    with open(manifest_path) as handle:
        manifest = json.load(handle)

    conn.execute(
        "INSERT INTO run_metadata VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            manifest.get("run_id"),
            manifest.get("code_revision"),
            manifest.get("exchange"),
            manifest.get("instrument_type"),
            manifest.get("data_as_of"),
            manifest.get("started_at"),
            manifest.get("finished_at"),
            manifest.get("status"),
            manifest.get("universe_total"),
            manifest.get("universe_screened"),
            manifest.get("provider"),
            manifest.get("source_run_id"),
            manifest.get("source_status"),
            json.dumps(manifest.get("thresholds", {}), sort_keys=True),
        ),
    )

    # JSON preserves insertion order, so position keeps the funnel readable in
    # the order the filters actually ran rather than alphabetically.
    rows = [
        (window, position, stage, count)
        for window, stages in manifest.get("counts", {}).items()
        for position, (stage, count) in enumerate(stages.items())
    ]
    conn.executemany("INSERT INTO screen_funnel VALUES (?,?,?,?)", rows)
    return len(rows)


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

            funnel_rows = load_manifest_tables(
                conn,
                os.path.join(cfg.data_dir, f"{cfg.prefix}_analysis_manifest.json"),
            )
            if funnel_rows:
                revision = conn.execute("SELECT code_revision FROM run_metadata").fetchone()
                logger.info(
                    f"  run_metadata + screen_funnel: {funnel_rows} funnel rows "
                    f"(code_revision {revision[0]})"
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


def should_auto_upload() -> bool:
    """Whether to publish to S3 without an explicit --upload flag.

    Sending data to a remote bucket is not something to do by default, so it
    stays opt-in: both S3_BUCKET and S3_AUTO_UPLOAD must be set.
    """
    truthy = {"1", "true", "yes", "on"}
    return bool(os.environ.get("S3_BUCKET")) and (
        os.environ.get("S3_AUTO_UPLOAD", "").strip().lower() in truthy
    )


def upload_to_s3(db_path: str, bucket: str, region: str | None = None) -> str:
    """Upload the published database to S3.

    Raises:
        RuntimeError: boto3 is not installed.
        FileNotFoundError: the database does not exist.
    """
    try:
        import boto3
    except ImportError:
        raise RuntimeError("S3 upload needs boto3: uv pip install boto3") from None

    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Nothing to upload: {db_path}")

    # Accept the bucket with or without the s3:// prefix, and an optional
    # key prefix after it.
    location = bucket.removeprefix("s3://").strip("/")
    bucket, _, prefix = location.partition("/")
    key = f"{prefix}/{os.path.basename(db_path)}" if prefix else os.path.basename(db_path)
    client = boto3.client("s3", region_name=region) if region else boto3.client("s3")
    size_mb = os.path.getsize(db_path) / 1048576
    logger.info(f"  Uploading {size_mb:.2f} MB -> s3://{bucket}/{key}")
    client.upload_file(db_path, bucket, key)
    return f"s3://{bucket}/{key}"
