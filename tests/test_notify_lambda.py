"""The notification lambda: the timestamp it converts and the mail it renders.

Two things can be wrong here. The offset -- +10 for half the year and +11 for
the other half, moving the date across midnight either way -- and the HTML,
which is assembled by string formatting and therefore has to be checked for
the values landing in the right places and being escaped on the way in.
"""

import importlib.util
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

MELBOURNE = ZoneInfo("Australia/Melbourne")

_SOURCE = Path(__file__).resolve().parents[1] / "infra" / "lambda" / "notify_published.py"


def _load():
    """Import the lambda by path -- infra/lambda is not an importable package."""
    spec = importlib.util.spec_from_file_location("notify_published", _SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


notify = _load()


def event(time="2026-08-25T21:34:50Z", key="asx.db", size=1982464, bucket="hive-in-the-cloud"):
    """An S3 Object Created event, trimmed to the keys the lambda reads."""
    return {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "time": time,
        "detail": {"bucket": {"name": bucket}, "object": {"key": key, "size": size}},
    }


@pytest.fixture
def published_db(tmp_path):
    """A database shaped like a real published one, small enough to build here.

    Two tables carry stock_price_date and disagree about their span, which is
    the case the date range has to get right: the mail reports the history the
    database holds, not one table's view of it.
    """
    path = tmp_path / "asx.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ASX_1_YEAR_HISTORY (ticker TEXT, stock_price_date TEXT)")
    conn.executemany(
        "INSERT INTO ASX_1_YEAR_HISTORY VALUES ('VAS', ?)",
        [("2025-09-02",), ("2026-01-05",), ("2026-08-25",)],
    )
    conn.execute("CREATE TABLE asx_etf_growth (ticker TEXT, stock_price_date TEXT)")
    conn.executemany("INSERT INTO asx_etf_growth VALUES ('VAS', ?)", [("2026-03-10",)] * 2)
    conn.execute("CREATE TABLE asx_etf_growth_7_days (ticker TEXT)")
    conn.execute("INSERT INTO asx_etf_growth_7_days VALUES ('VAS')")
    # data_as_of is the session the screen ran to. Deliberately older than
    # the newest row above: on 29 Aug 2026 ten of 450 ASX tickers carried the
    # 28th and the rest stopped at the 27th, and that is the case the mail has
    # to say out loud.
    conn.execute("CREATE TABLE run_metadata (exchange TEXT, instrument_type TEXT, data_as_of TEXT)")
    conn.execute("INSERT INTO run_metadata VALUES ('ASX', 'etf', '2026-08-24')")
    conn.commit()
    conn.close()
    return path


def details(contents=None, **kwargs):
    return notify.details_for(event(**kwargs), MELBOURNE, contents)


# ------------------------------------------------------------------- time


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        # The mail that prompted this: 21:34 UTC is breakfast the next day in
        # Melbourne, not half past nine the previous evening.
        ("2026-08-25T21:34:50Z", ("07:34 AEST", "Wed 26 Aug 2026")),
        # January is daylight saving: +11, and the date has already rolled.
        ("2026-01-15T22:30:00Z", ("09:30 AEDT", "Fri 16 Jan 2026")),
    ],
)
def test_local_time(utc, expected):
    assert notify.local_time(utc, MELBOURNE) == expected


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        # Daylight saving ends 03:00 AEDT on the first Sunday in April 2026,
        # which is 16:00 UTC the day before. Either side of that instant the
        # local clock reads 02:59 and 02:00 -- and the label is the only thing
        # that says which two o'clock it is.
        ("2026-04-04T15:59:00Z", ("02:59 AEDT", "Sun 05 Apr 2026")),
        ("2026-04-04T16:00:00Z", ("02:00 AEST", "Sun 05 Apr 2026")),
        # And it starts again 02:00 AEST on the first Sunday in October,
        # skipping the 2 a.m. hour entirely.
        ("2026-10-03T15:59:00Z", ("01:59 AEST", "Sun 04 Oct 2026")),
        ("2026-10-03T16:00:00Z", ("03:00 AEDT", "Sun 04 Oct 2026")),
    ],
)
def test_local_time_across_the_dst_transitions(utc, expected):
    assert notify.local_time(utc, MELBOURNE) == expected


def test_unknown_timezone_says_utc_rather_than_mislabelling():
    """A timestamp labelled AEST that is really UTC reads ten hours early."""
    assert notify.local_time("2026-08-25T21:34:50Z", notify._zone("Mars/Olympus")) == (
        "21:34 UTC",
        "Tue 25 Aug 2026",
    )


# ------------------------------------------------------------------- size


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (1982464, "1.89 MB"),
        (41288192, "39.38 MB"),
        # Small enough to be suspicious, and KB is what makes that obvious.
        (204800, "200 KB"),
        # The event is malformed rather than the object tiny: say so instead
        # of dividing a string.
        ("?", "?"),
        (None, "None"),
    ],
)
def test_format_size(size, expected):
    assert notify.format_size(size) == expected


# ------------------------------------------------------------------- mail


def test_subject_names_the_database_and_its_size():
    """The two things worth knowing without opening the mail."""
    assert notify.subject_for(details()) == "stocks: asx.db is ready to use (1.89 MB)"


def test_text_body_carries_the_local_time_and_no_utc_stamp():
    body = notify.render_text(details())
    assert "07:34 AEST on Wed 26 Aug 2026" in body
    assert "s3://hive-in-the-cloud/asx.db" in body
    assert "1,982,464 bytes" in body
    assert "2026-08-25T21:34:50Z" not in body


def test_html_body_is_a_whole_document_carrying_the_same_facts():
    body = notify.render_html(details())
    assert body.startswith("<!doctype html>")
    assert body.rstrip().endswith("</html>")
    assert "asx.db is ready to use" in body
    assert "07:34 AEST" in body
    assert "Wed 26 Aug 2026" in body
    assert "1.89 MB" in body
    assert "2026-08-25T21:34:50Z" not in body


def test_html_escapes_the_values_it_interpolates():
    """Key and bucket are strings from an AWS event, not literals in this file.

    They are interpolated into markup, so they go through html.escape -- an
    object named with a bracket would otherwise close a tag early and break
    the rest of the mail.
    """
    body = notify.render_html(details(key="a<script>.db"))
    assert "<script>" not in body
    assert "a&lt;script&gt;.db" in body


# -------------------------------------------------------------- contents


def test_inspect_reports_every_table_and_its_row_count(published_db):
    found = notify.inspect_database(str(published_db))
    assert found["tables"] == [
        ("ASX_1_YEAR_HISTORY", 3),
        ("asx_etf_growth", 2),
        ("asx_etf_growth_7_days", 1),
        ("run_metadata", 1),
    ]


def test_inspect_spans_every_table_that_carries_dates(published_db):
    """Not one table's range: the earliest and latest the database holds."""
    found = notify.inspect_database(str(published_db))
    assert (found["first_date"], found["last_date"]) == ("02 Sep 2025", "25 Aug 2026")


def test_inspect_names_the_universe_from_run_metadata(published_db):
    assert notify.inspect_database(str(published_db))["universe"] == "ASX ETF"


def test_inspect_survives_a_database_without_run_metadata(tmp_path):
    """A publish that never reached the analysis manifest still gets a mail."""
    path = tmp_path / "bare.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE prices (ticker TEXT)")
    conn.commit()
    conn.close()

    found = notify.inspect_database(str(path))
    assert found["tables"] == [("prices", 0)]
    assert "universe" not in found
    assert "first_date" not in found


@pytest.mark.parametrize(
    ("exchange", "kind", "expected"),
    [("ASX", "etf", "ASX ETF"), ("US", "stocks", "US stocks"), ("ASX", "", "ASX")],
)
def test_universe_label(exchange, kind, expected):
    assert notify.universe_label(exchange, kind) == expected


def test_an_unreadable_database_costs_the_detail_not_the_mail(monkeypatch):
    """The mail's job is to say a database landed; it can still say that."""

    class BrokenS3:
        def download_file(self, bucket, key, path):
            raise RuntimeError("AccessDenied")

    monkeypatch.setattr(notify, "_s3", BrokenS3())
    assert notify.contents_of("hive-in-the-cloud", "asx.db") == {}

    body = notify.render_text(details(contents={}))
    assert "asx.db is ready to use" in body
    assert "Tables" not in body


# ------------------------------------------------------------------ mail


def test_the_lead_sentence_names_the_universe(published_db):
    d = details(contents=notify.inspect_database(str(published_db)))
    assert notify.lead_for(d) == (
        "Stock price history has been downloaded for ASX ETF tickers and "
        "analysis is done. Database s3://hive-in-the-cloud/asx.db is ready to use."
    )


def test_the_lead_sentence_drops_the_universe_when_it_is_unknown():
    assert notify.lead_for(details(contents={})) == (
        "Stock price history has been downloaded and analysis is done. "
        "Database s3://hive-in-the-cloud/asx.db is ready to use."
    )


def test_both_bodies_list_the_tables_with_counts(published_db):
    d = details(contents=notify.inspect_database(str(published_db)))

    text = notify.render_text(d)
    assert "ASX_1_YEAR_HISTORY" in text
    # To the screen's date, not the newest row -- see the next test.
    assert "02 Sep 2025 to 24 Aug 2026" in text

    body = notify.render_html(d)
    assert "ASX_1_YEAR_HISTORY" in body
    assert "02 Sep 2025" in body and "24 Aug 2026" in body


def test_the_mail_reports_the_screen_date_not_the_newest_row(published_db):
    """The case that made a live quote look like it disagreed with the app.

    The file holds a 25 Aug row, but the screen ran to the 24th because that
    is the session every ticker reached. Reporting the 25th would say the
    database is a day fresher than anything in it was measured to.
    """
    d = details(contents=notify.inspect_database(str(published_db)))
    assert d["prices_to"] == "24 Aug 2026"
    assert d["newest_row"] == "25 Aug 2026"

    for body in (notify.render_text(d), notify.render_html(d)):
        assert "24 Aug 2026" in body
        assert "25 Aug 2026" in body
        assert "staleness_days" in body


def test_no_lag_notice_when_every_ticker_reached_the_screen_date(tmp_path):
    """The common case: nothing to warn about, so nothing is said."""
    path = tmp_path / "current.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE hist (ticker TEXT, stock_price_date TEXT)")
    conn.execute("INSERT INTO hist VALUES ('VAS', '2026-08-28')")
    conn.execute("CREATE TABLE run_metadata (exchange TEXT, instrument_type TEXT, data_as_of TEXT)")
    conn.execute("INSERT INTO run_metadata VALUES ('ASX', 'etf', '2026-08-28')")
    conn.commit()
    conn.close()

    d = details(contents=notify.inspect_database(str(path)))
    assert d["newest_row"] == ""
    assert "staleness_days" not in notify.render_html(d)


def test_a_run_metadata_missing_a_column_costs_only_that_field(tmp_path):
    """The table has gained columns before; a missing one is not fatal."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE hist (ticker TEXT, stock_price_date TEXT)")
    conn.execute("INSERT INTO hist VALUES ('VAS', '2026-08-28')")
    conn.execute("CREATE TABLE run_metadata (exchange TEXT, instrument_type TEXT)")
    conn.execute("INSERT INTO run_metadata VALUES ('ASX', 'etf')")
    conn.commit()
    conn.close()

    found = notify.inspect_database(str(path))
    assert found["universe"] == "ASX ETF"
    assert "as_of" not in found
    # Falls back to the newest row, which is all this database knows.
    assert details(contents=found)["prices_to"] == "28 Aug 2026"


def test_a_missing_timestamp_still_produces_a_mail():
    """No $.time is a malformed event, not a reason to send nothing."""
    broken = event()
    del broken["time"]
    assert "is ready to use" in notify.subject_for(notify.details_for(broken, MELBOURNE))


# ---------------------------------------------------------------- sending


def test_handler_sends_both_bodies_from_the_configured_address(monkeypatch, published_db):
    sent = {}

    class FakeSES:
        def send_email(self, **kwargs):
            sent.update(kwargs)

    class FakeS3:
        def download_file(self, bucket, key, path):
            shutil.copy(published_db, path)

    monkeypatch.setattr(notify, "_ses", FakeSES())
    monkeypatch.setattr(notify, "_s3", FakeS3())
    monkeypatch.setattr(notify, "TIMEZONE", "Australia/Melbourne")
    monkeypatch.setitem(os.environ, "FROM_ADDRESS", "Stocks <stocks@example.com>")
    monkeypatch.setitem(os.environ, "TO_ADDRESS", "someone@example.com")

    result = notify.handler(event(key="us.db"), None)

    assert sent["FromEmailAddress"] == "Stocks <stocks@example.com>"
    assert sent["Destination"] == {"ToAddresses": ["someone@example.com"]}

    simple = sent["Content"]["Simple"]
    assert (
        simple["Subject"]["Data"] == result["subject"] == "stocks: us.db is ready to use (1.89 MB)"
    )
    # Both parts, always: a mail with no text alternative scores worse with
    # spam filters and is unreadable in a client that refuses HTML.
    assert "07:34 AEST" in simple["Body"]["Text"]["Data"]
    assert "07:34 AEST" in simple["Body"]["Html"]["Data"]
    # The database was opened, not just the event read.
    assert "ASX_1_YEAR_HISTORY" in simple["Body"]["Html"]["Data"]
