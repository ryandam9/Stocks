"""The notification lambda's timestamp conversion.

The function exists only to turn EventBridge's UTC `$.time` into Melbourne
local time, so these tests are about the one thing that can be wrong: the
offset, which is +10 for half the year and +11 for the other half, and moves
the date across midnight either way.
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


def event(time="2026-08-25T08:39:25Z", key="asx.db", size=2015232):
    """An S3 Object Created event, trimmed to the keys the lambda reads."""
    return {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "time": time,
        "detail": {
            "bucket": {"name": "hive-in-the-cloud"},
            "object": {"key": key, "size": size},
        },
    }


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        # The message that prompted this: 08:39 UTC is a quarter to seven in
        # the evening in Melbourne, not twenty to nine in the morning.
        ("2026-08-25T08:39:25Z", "18:39 AEST on Tue 25 Aug 2026"),
        # January is daylight saving: +11, and the date has already rolled.
        ("2026-01-15T22:30:00Z", "09:30 AEDT on Fri 16 Jan 2026"),
        # An ASX run publishing at 07:20 local on a winter morning.
        ("2026-06-01T21:20:00Z", "07:20 AEST on Tue 02 Jun 2026"),
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
        ("2026-04-04T15:59:00Z", "02:59 AEDT on Sun 05 Apr 2026"),
        ("2026-04-04T16:00:00Z", "02:00 AEST on Sun 05 Apr 2026"),
        # And it starts again 02:00 AEST on the first Sunday in October,
        # skipping the 2 a.m. hour entirely.
        ("2026-10-03T15:59:00Z", "01:59 AEST on Sun 04 Oct 2026"),
        ("2026-10-03T16:00:00Z", "03:00 AEDT on Sun 04 Oct 2026"),
    ],
)
def test_local_time_across_the_dst_transitions(utc, expected):
    assert notify.local_time(utc, MELBOURNE) == expected


def test_message_reads_like_the_email():
    assert notify.message_for(event(), MELBOURNE) == (
        "stocks: asx.db published to s3://hive-in-the-cloud/asx.db "
        "at 18:39 AEST on Tue 25 Aug 2026 (2015232 bytes). "
        "The task completed and the database is live."
    )


def test_unknown_timezone_says_utc_rather_than_mislabelling():
    """A timestamp labelled AEST that is really UTC reads ten hours early."""
    assert notify.local_time("2026-08-25T08:39:25Z", notify._zone("Mars/Olympus")) == (
        "08:39 UTC on Tue 25 Aug 2026"
    )


def test_handler_publishes_the_message(monkeypatch):
    published = {}

    class FakeSNS:
        def publish(self, **kwargs):
            published.update(kwargs)

    monkeypatch.setattr(notify, "_sns", FakeSNS())
    monkeypatch.setattr(notify, "TIMEZONE", "Australia/Melbourne")
    monkeypatch.setitem(
        os.environ, "TOPIC_ARN", "arn:aws:sns:ap-southeast-2:1:stocks-notifications"
    )

    result = notify.handler(event(key="us.db"), None)

    assert published["TopicArn"] == "arn:aws:sns:ap-southeast-2:1:stocks-notifications"
    assert published["Subject"] == "stocks: us.db published"
    assert "18:39 AEST on Tue 25 Aug 2026" in published["Message"]
    assert "Z (" not in published["Message"]
    assert result["message"] == published["Message"]
