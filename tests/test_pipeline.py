"""Tests for the Python SQLite publisher that replaced the shell loader."""

import os
import sqlite3

import pytest
from conftest import make_series
from test_integration import build_project, write_prices, write_universe

import pipeline
from config import GROWTH_COLUMN_TYPES
from pipeline import build_consistent_growth, load_csv, publish

GROWTH_SQL = pipeline.GROWTH_TABLE_SQL


def test_load_csv_applies_declared_types(tmp_path):
    """Types must not depend on what values happen to be present."""
    csv_path = tmp_path / "g.csv"
    header = ",".join(name for name, _ in GROWTH_COLUMN_TYPES)
    csv_path.write_text(
        header + "\n"
        "AAA,Alpha,NYSE,common_stock,2025-01-01,10.5,2026-01-01,20.25,92.86,"
        "251,364,0.997,0.958,1000000,adjusted,2026-01-01,run-1,https://x\n"
    )
    conn = sqlite3.connect(":memory:")
    assert load_csv(conn, str(csv_path), "g", GROWTH_SQL) == 1

    row = conn.execute(
        "SELECT typeof(pct_change), typeof(observations), typeof(ticker), pct_change FROM g"
    ).fetchone()
    assert row[:3] == ("real", "integer", "text")
    assert row[3] == pytest.approx(92.86)


def test_empty_csv_still_creates_a_typed_table(tmp_path):
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text(",".join(name for name, _ in GROWTH_COLUMN_TYPES) + "\n")
    conn = sqlite3.connect(":memory:")
    assert load_csv(conn, str(csv_path), "g", GROWTH_SQL) == 0

    types = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(g)")}
    assert types["pct_change"] == "FLOAT"
    assert types["observations"] == "INTEGER"


def test_missing_csv_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="Expected output missing"):
        load_csv(sqlite3.connect(":memory:"), str(tmp_path / "nope.csv"), "g", GROWTH_SQL)


def test_consistent_growth_intersects_given_labels():
    conn = sqlite3.connect(":memory:")
    for label, tickers in [("a", ["X", "Y"]), ("b", ["X"])]:
        conn.execute(
            f'CREATE TABLE "p_growth_{label}" (ticker TEXT, name TEXT, exchange TEXT,'
            " pct_change REAL, data_as_of TEXT, run_id TEXT)"
        )
        conn.executemany(
            f'INSERT INTO "p_growth_{label}" VALUES (?,?,?,?,?,?)',
            [(t, t, "NYSE", 1.0, "2026-01-01", "r") for t in tickers],
        )
    assert build_consistent_growth(conn, "p", ["a", "b"]) == 1
    assert [r[0] for r in conn.execute("SELECT ticker FROM consistent_growth_stocks")] == ["X"]


def test_consistent_growth_skipped_without_labels():
    conn = sqlite3.connect(":memory:")
    assert build_consistent_growth(conn, "p", []) == 0
    assert not conn.execute(
        "SELECT name FROM sqlite_master WHERE name='consistent_growth_stocks'"
    ).fetchall()


def test_publish_is_atomic_and_leaves_no_temp_file(tmp_path, monkeypatch):
    cfg = build_project(tmp_path, monkeypatch)
    write_universe(cfg, [("AAA", "Alpha Inc")])
    write_prices(cfg, make_series("AAA", "Alpha Inc", "2026-05-01", "2026-06-02", 100, 150))

    from analysis import analyze_stocks

    analyze_stocks(cfg, allow_stale=True)
    path = publish(cfg)

    assert os.path.exists(path)
    leftovers = [f for f in os.listdir(os.path.dirname(path)) if ".building." in f]
    assert not leftovers, leftovers


def test_publish_leaves_previous_db_intact_on_failure(tmp_path, monkeypatch):
    """A failed build must not replace a known-good database."""
    cfg = build_project(tmp_path, monkeypatch)
    write_universe(cfg, [("AAA", "Alpha Inc")])
    write_prices(cfg, make_series("AAA", "Alpha Inc", "2026-05-01", "2026-06-02", 100, 150))

    from analysis import analyze_stocks

    analyze_stocks(cfg, allow_stale=True)
    publish(cfg)
    good = open(cfg.db_path, "rb").read()

    # Remove an expected output so the next publish fails part-way.
    os.remove(cfg.growth_csv("1_month"))
    with pytest.raises(FileNotFoundError):
        publish(cfg)

    assert open(cfg.db_path, "rb").read() == good
    assert not [f for f in os.listdir(os.path.dirname(cfg.db_path)) if ".building." in f]


def test_upload_requires_boto3(monkeypatch, tmp_path):
    db = tmp_path / "x.db"
    db.write_bytes(b"")
    monkeypatch.setitem(__import__("sys").modules, "boto3", None)
    with pytest.raises((RuntimeError, TypeError, AttributeError)):
        pipeline.upload_to_s3(str(db), "s3://bucket")


# ------------------------------------------------------------------ S3 upload


@pytest.mark.parametrize(
    "bucket,auto,expected",
    [
        ("s3://b", "true", True),
        ("s3://b", "1", True),
        ("s3://b", "yes", True),
        ("s3://b", "false", False),
        ("s3://b", "", False),
        ("", "true", False),
    ],
)
def test_auto_upload_requires_both_settings(monkeypatch, bucket, auto, expected):
    """Sending data off the machine stays opt-in, never a silent default."""
    monkeypatch.setenv("S3_BUCKET", bucket)
    monkeypatch.setenv("S3_AUTO_UPLOAD", auto)
    assert pipeline.should_auto_upload() is expected


def test_auto_upload_off_when_unset(monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("S3_AUTO_UPLOAD", raising=False)
    assert pipeline.should_auto_upload() is False


def test_upload_builds_the_expected_key(tmp_path, monkeypatch):
    """Bucket may be given with or without s3://, and with a key prefix."""
    db = tmp_path / "us.db"
    db.write_bytes(b"x")
    calls = []

    class FakeClient:
        def upload_file(self, path, bucket, key):
            calls.append((path, bucket, key))

    fake_boto3 = type("m", (), {"client": staticmethod(lambda *a, **k: FakeClient())})
    monkeypatch.setitem(__import__("sys").modules, "boto3", fake_boto3)

    assert pipeline.upload_to_s3(str(db), "s3://hive") == "s3://hive/us.db"
    assert pipeline.upload_to_s3(str(db), "hive") == "s3://hive/us.db"
    assert pipeline.upload_to_s3(str(db), "s3://hive/daily") == "s3://hive/daily/us.db"
    assert [c[1] for c in calls] == ["hive", "hive", "hive"]
    assert [c[2] for c in calls] == ["us.db", "us.db", "daily/us.db"]


def test_upload_missing_file_raises(tmp_path, monkeypatch):
    fake_boto3 = type("m", (), {"client": staticmethod(lambda *a, **k: None)})
    monkeypatch.setitem(__import__("sys").modules, "boto3", fake_boto3)
    with pytest.raises(FileNotFoundError, match="Nothing to upload"):
        pipeline.upload_to_s3(str(tmp_path / "gone.db"), "s3://b")


# ------------------------------------------------------- upload is gated on publishing


def _run_cli(monkeypatch, job, *extra):
    """Invoke run.main with every stage stubbed out, recording S3 uploads."""
    import click.testing

    import run

    uploaded = []
    monkeypatch.setattr(run, "_sync", lambda *a, **k: None)
    monkeypatch.setattr(run, "_fetch", lambda *a, **k: None)
    monkeypatch.setattr(run, "analyze_stocks", lambda *a, **k: None)
    monkeypatch.setattr(run.pipeline, "publish", lambda cfg: cfg.db_path)
    monkeypatch.setattr(
        run.pipeline,
        "upload_to_s3",
        lambda path, bucket, region=None: uploaded.append(bucket) or f"s3://x/{job}",
    )

    result = click.testing.CliRunner().invoke(
        run.main,
        [job, "--exchange", "US", "--instrument-type", "stocks", *extra],
        standalone_mode=False,
    )
    return result, uploaded


@pytest.mark.parametrize("job", ["sync", "fetch"])
def test_non_publishing_jobs_never_upload(job, monkeypatch):
    """A job that builds no database must not republish a stale one.

    `sync` and `fetch` leave whatever database an earlier run wrote in place.
    Uploading it would send results stamped with that run's run_id and
    data_as_of, presented as though this run had produced them.
    """
    monkeypatch.setenv("S3_BUCKET", "s3://example-bucket")
    monkeypatch.setenv("S3_AUTO_UPLOAD", "true")

    result, uploaded = _run_cli(monkeypatch, job)

    assert uploaded == [], f"{job} uploaded a database it did not build"
    assert result.exit_code == 0


@pytest.mark.parametrize("job", ["analyze", "publish", "all"])
def test_publishing_jobs_still_upload(job, monkeypatch):
    """The guard must not break the jobs that do build a database."""
    monkeypatch.setenv("S3_BUCKET", "s3://example-bucket")
    monkeypatch.setenv("S3_AUTO_UPLOAD", "true")

    _, uploaded = _run_cli(monkeypatch, job)

    assert uploaded == ["s3://example-bucket"]


def test_explicit_upload_on_a_non_publishing_job_is_an_error(monkeypatch):
    """--upload asks for something `sync` cannot do; say so rather than no-op."""
    monkeypatch.setenv("S3_BUCKET", "s3://example-bucket")

    result, uploaded = _run_cli(monkeypatch, "sync", "--upload")

    assert uploaded == []
    assert result.exit_code != 0
