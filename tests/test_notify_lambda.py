"""The notification lambda: the timestamp it converts and the mail it renders.

Two things can be wrong here. The offset -- +10 for half the year and +11 for
the other half, moving the date across midnight either way -- and the HTML,
which is assembled by string formatting and therefore has to be checked for
the values landing in the right places and being escaped on the way in.
"""

import importlib.util
import os
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


def details(**kwargs):
    return notify.details_for(event(**kwargs), MELBOURNE)


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
    assert notify.subject_for(details()) == "stocks: asx.db is live (1.89 MB)"


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
    assert "asx.db is live" in body
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


def test_a_missing_timestamp_still_produces_a_mail():
    """No $.time is a malformed event, not a reason to send nothing."""
    broken = event()
    del broken["time"]
    assert "is live" in notify.subject_for(notify.details_for(broken, MELBOURNE))


# ---------------------------------------------------------------- sending


def test_handler_sends_both_bodies_from_the_configured_address(monkeypatch):
    sent = {}

    class FakeSES:
        def send_email(self, **kwargs):
            sent.update(kwargs)

    monkeypatch.setattr(notify, "_ses", FakeSES())
    monkeypatch.setattr(notify, "TIMEZONE", "Australia/Melbourne")
    monkeypatch.setitem(os.environ, "FROM_ADDRESS", "Stocks <stocks@example.com>")
    monkeypatch.setitem(os.environ, "TO_ADDRESS", "someone@example.com")

    result = notify.handler(event(key="us.db"), None)

    assert sent["FromEmailAddress"] == "Stocks <stocks@example.com>"
    assert sent["Destination"] == {"ToAddresses": ["someone@example.com"]}

    simple = sent["Content"]["Simple"]
    assert simple["Subject"]["Data"] == result["subject"] == "stocks: us.db is live (1.89 MB)"
    # Both parts, always: a mail with no text alternative scores worse with
    # spam filters and is unreadable in a client that refuses HTML.
    assert "07:34 AEST" in simple["Body"]["Text"]["Data"]
    assert "07:34 AEST" in simple["Body"]["Html"]["Data"]
