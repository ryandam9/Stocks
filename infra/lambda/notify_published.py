"""Send the "database is live" email with a Melbourne-local timestamp.

EventBridge could publish the S3 event straight to SNS, and did. What it
cannot do is say what time it is in Melbourne: an input transformer
substitutes values and never converts them, so the only timestamp available
to it is the UTC one in ``$.time``. Every other time in this stack is local
-- the schedules run on Australia/Melbourne, the runbook quotes 07:15 and
09:30, and the person reading the mail is at breakfast in that zone -- so a
line reading "at 2026-08-25T08:39:25Z" was the one thing in the message that
had to be worked out rather than read. The subtraction is also the kind that
goes wrong: it is ten hours for half the year and eleven for the other half,
and the day rolls over either way.

This function exists only to do that conversion. It stays deliberately thin:
the event still has to reach S3 for it to run at all, so nothing here can
make the email claim a database that is not in the bucket.
"""

import logging
import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Defaulted rather than required so the module imports without a Lambda
# environment around it; terraform always sets both.
TIMEZONE = os.environ.get("TIMEZONE", "Australia/Melbourne")

# SNS subjects are capped at 100 characters and a subject that breaks the cap
# fails the whole publish -- which would turn a cosmetic problem into a
# missing email.
SUBJECT_LIMIT = 100

_sns = None


def _client():
    """The SNS client, created on first use.

    Not at import time: a module-level client resolves credentials and a
    region as a side effect of importing, which is fine in Lambda and stops
    the unit test importing this file at all.
    """
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    return _sns


def _zone(name: str):
    """The named zone, or UTC if the runtime carries no tz database.

    Managed Lambda runtimes ship /usr/share/zoneinfo, so this should not
    happen. If it ever does the message says UTC and means it: a timestamp
    labelled AEST that is really UTC would be worse than no conversion at
    all, because it reads as a run that landed ten hours early.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("No tz database entry for %s; leaving the timestamp in UTC", name)
        return UTC


def local_time(stamp: str, zone) -> str:
    """Render an EventBridge UTC timestamp as "18:39 AEST on Tue 25 Aug 2026".

    %Z is what makes this readable at a glance: zoneinfo resolves it to AEST
    or AEDT according to the date, so the message names the offset in force
    that night instead of leaving the reader to remember whether daylight
    saving had started.
    """
    moment = datetime.fromisoformat(stamp).astimezone(zone)
    return moment.strftime("%H:%M %Z on %a %d %b %Y")


def message_for(event: dict, zone) -> str:
    """The email body for one S3 Object Created event."""
    detail = event.get("detail", {})
    obj = detail.get("object", {})
    bucket = detail.get("bucket", {}).get("name", "?")
    key = obj.get("key", "?")
    size = obj.get("size", "?")

    # A missing $.time should not cost the email; the function runs within
    # seconds of the event, so now() is the same minute.
    stamp = event.get("time") or datetime.now(UTC).isoformat()

    return (
        f"stocks: {key} published to s3://{bucket}/{key} at {local_time(stamp, zone)} "
        f"({size} bytes). The task completed and the database is live."
    )


def handler(event, context):
    """Publish one notification for one S3 event."""
    message = message_for(event, _zone(TIMEZONE))
    key = event.get("detail", {}).get("object", {}).get("key", "database")

    # SNS defaults the subject to "AWS Notification Message", which says
    # nothing in an inbox list. Now that this stack does the publishing, it
    # can name the database instead.
    _client().publish(
        TopicArn=os.environ["TOPIC_ARN"],
        Subject=f"stocks: {key} published"[:SUBJECT_LIMIT],
        Message=message,
    )
    logger.info(message)
    return {"message": message}
