"""Send the "database is live" email, formatted and in Melbourne local time.

Two things about the raw S3 event make a bad email, and this function exists
to fix both.

The time. EventBridge delivers ``$.time`` in UTC and its input transformer
substitutes values without ever converting them, so a plain SNS target could
only ever quote "2026-08-25T21:34:50Z" for a database that landed at 07:34 the
next morning. Everything else in this stack is Melbourne local -- the
schedules, the runbook's 07:15 and 09:30, the person reading this at
breakfast -- and the correction is ten hours for half the year and eleven for
the other half, with the date moving either way.

The format. SNS email is plain text only, wrapped in a quoted string and an
unsubscribe footer, under the subject "AWS Notification Message". SES takes an
HTML body and a subject of our choosing, so the mail can say what happened in
its first line and put the details where they can be skimmed.

It stays deliberately thin either way: the object still has to reach S3 for
this to run at all, so nothing here can make the email claim a database that
is not in the bucket.
"""

import html
import logging
import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Defaulted rather than required so the module imports without a Lambda
# environment around it; terraform always sets it.
TIMEZONE = os.environ.get("TIMEZONE", "Australia/Melbourne")

_ses = None


def _client():
    """The SES client, created on first use.

    Not at import time: a module-level client resolves credentials and a
    region as a side effect of importing, which is fine in Lambda and stops
    the unit test importing this file at all.
    """
    global _ses
    if _ses is None:
        _ses = boto3.client("sesv2")
    return _ses


def _zone(name: str):
    """The named zone, or UTC if the runtime carries no tz database.

    Managed Lambda runtimes ship /usr/share/zoneinfo, so this should not
    happen. If it ever does the mail says UTC and means it: a timestamp
    labelled AEST that is really UTC would be worse than no conversion at
    all, because it reads as a run that landed ten hours early.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("No tz database entry for %s; leaving the timestamp in UTC", name)
        return UTC


def local_time(stamp: str, zone) -> tuple[str, str]:
    """An EventBridge UTC timestamp as ("07:34 AEST", "Wed 26 Aug 2026").

    Split in two because the mail leads with the clock time and treats the
    date as secondary -- the run is daily, so the reader already knows the
    day and is checking the hour.

    %Z is what makes it readable at a glance: zoneinfo resolves it to AEST or
    AEDT according to the date, so the mail names the offset in force that
    morning rather than leaving the reader to remember whether daylight saving
    had started.
    """
    moment = datetime.fromisoformat(stamp).astimezone(zone)
    return moment.strftime("%H:%M %Z"), moment.strftime("%a %d %b %Y")


def format_size(size) -> str:
    """Bytes as "1.89 MB", falling back to the raw value if it is not a number.

    Size is in the mail because it is the cheapest check that the run produced
    a real database: a screen that matched nothing still publishes, and would
    otherwise arrive looking like any other success a few hundred KB short of
    the usual figure. Megabytes are what makes that comparable at a glance --
    nobody eyeballs seven digits.
    """
    try:
        mb = int(size) / 1_048_576
    except (TypeError, ValueError):
        return str(size)
    return f"{mb:.2f} MB" if mb >= 1 else f"{int(size) / 1024:.0f} KB"


def details_for(event: dict, zone) -> dict:
    """Everything both renderings need, pulled out of one S3 event."""
    detail = event.get("detail", {})
    obj = detail.get("object", {})
    bucket = detail.get("bucket", {}).get("name", "?")
    key = obj.get("key", "?")
    size = obj.get("size", "?")

    # A missing $.time should not cost the email; the function runs within
    # seconds of the event, so now() is the same minute.
    stamp = event.get("time") or datetime.now(UTC).isoformat()
    clock, date = local_time(stamp, zone)

    return {
        "key": key,
        "bucket": bucket,
        "uri": f"s3://{bucket}/{key}",
        "size": format_size(size),
        "bytes": f"{int(size):,}" if str(size).isdigit() else str(size),
        "clock": clock,
        "date": date,
    }


def subject_for(d: dict) -> str:
    """The inbox line. Names the database and its size, because those are the
    two things worth knowing without opening the mail."""
    return f"stocks: {d['key']} is live ({d['size']})"


def render_text(d: dict) -> str:
    """The plain-text alternative.

    Sent alongside the HTML, not instead of it: a mail with no text part
    scores worse with spam filters and is unreadable in a client that refuses
    HTML.
    """
    return (
        f"{d['key']} is live.\n\n"
        f"Published  {d['clock']} on {d['date']}\n"
        f"Size       {d['size']} ({d['bytes']} bytes)\n"
        f"Object     {d['uri']}\n\n"
        f"The scheduled task completed and the database is in the bucket.\n"
    )


# Inline styles and a table layout, because that is what mail clients
# support -- Gmail strips <style> blocks, and flexbox and grid are not
# reliable in Outlook.
# Sentence case rather than tracked-out capitals, and a narrow label column:
# at 390px there is about 250px left for the value, which is just enough to
# keep the s3 URI on one line instead of breaking it mid-word.
_ROW = (
    "<tr>"
    '<td style="padding:0 0 12px;color:#8a949e;font-size:13px;'
    'white-space:nowrap;vertical-align:top;width:68px;">{label}</td>'
    '<td style="padding:0 0 12px;color:#0f1720;font-size:15px;'
    'vertical-align:top;overflow-wrap:break-word;">{value}</td>'
    "</tr>"
)


def render_html(d: dict) -> str:
    """The HTML body.

    Deliberately one card, no images and no links: the mail is read on a
    phone at breakfast, and everything it has to say fits above the fold.
    """
    e = {k: html.escape(str(v)) for k, v in d.items()}
    # The time is the headline's subtitle, so it is deliberately not repeated
    # here -- two fields is the whole table.
    rows = "".join(
        _ROW.format(label=label, value=value)
        for label, value in (
            ("Size", f'{e["size"]} <span style="color:#8a949e;">({e["bytes"]} bytes)</span>'),
            (
                "Object",
                f'<span style="font-family:ui-monospace,SFMono-Regular,Menlo,'
                f'Consolas,monospace;font-size:13px;">{e["uri"]}</span>',
            ),
        )
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{e["key"]} is live</title>
</head>
<body style="margin:0;padding:0;background:#f4f5f7;
-webkit-font-smoothing:antialiased;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">
{e["key"]} published at {e["clock"]}, {e["size"]}.</div>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
 style="background:#f4f5f7;">
<tr><td align="center" style="padding:28px 12px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600"
 style="width:100%;max-width:600px;background:#ffffff;border:1px solid #e6e8eb;
 border-radius:14px;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,
 'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">

<tr><td style="height:4px;background:#0f7b4f;font-size:0;line-height:0;">&nbsp;</td></tr>

<tr><td style="padding:26px 28px 0;">
<div style="color:#5b6672;font-size:12px;letter-spacing:.12em;
text-transform:uppercase;font-weight:600;">stocks</div>
<h1 style="margin:10px 0 4px;color:#0f1720;font-size:26px;line-height:1.25;
font-weight:650;">{e["key"]} is live</h1>
<p style="margin:0;color:#0f7b4f;font-size:15px;font-weight:600;">
Published {e["clock"]} <span style="color:#8a949e;font-weight:400;">&middot;
{e["date"]}</span></p>
</td></tr>

<tr><td style="padding:22px 28px 4px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
 style="border-top:1px solid #eceef0;padding-top:18px;">
{rows}
</table>
</td></tr>

<tr><td style="padding:4px 28px 26px;">
<p style="margin:0;color:#5b6672;font-size:13px;line-height:1.6;">
Sent when the object reached S3, not when the task exited &mdash; so it cannot
arrive for a run that published nothing.</p>
</td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


def handler(event, context):
    """Send one notification for one S3 event."""
    details = details_for(event, _zone(TIMEZONE))

    _client().send_email(
        FromEmailAddress=os.environ["FROM_ADDRESS"],
        Destination={"ToAddresses": [os.environ["TO_ADDRESS"]]},
        Content={
            "Simple": {
                "Subject": {"Data": subject_for(details)},
                "Body": {
                    "Text": {"Data": render_text(details)},
                    "Html": {"Data": render_html(details)},
                },
            }
        },
    )
    logger.info("Sent: %s", subject_for(details))
    return {"subject": subject_for(details)}
