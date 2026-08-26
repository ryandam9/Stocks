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
import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Every published database carries this column on its price tables, and it is
# how the mail finds the span of the history without knowing their names.
DATE_COLUMN = "stock_price_date"

# Defaulted rather than required so the module imports without a Lambda
# environment around it; terraform always sets it.
TIMEZONE = os.environ.get("TIMEZONE", "Australia/Melbourne")

_ses = None
_s3 = None


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


def _s3_client():
    """The S3 client, created on first use. See :func:`_client`."""
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


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


def universe_label(exchange: str, instrument_type: str) -> str:
    """ "ASX" + "etf" -> "ASX ETF"; "US" + "stocks" -> "US stocks".

    Read out of the database's own run_metadata rather than guessed from the
    object key, so the sentence names what was actually screened. A three
    letter type is an initialism and is capitalised; anything longer is an
    ordinary word.
    """
    kind = instrument_type.upper() if len(instrument_type) <= 3 else instrument_type.lower()
    return f"{exchange.upper()} {kind}".strip()


def _pretty_date(value: str) -> str:
    """ "2025-09-02" -> "02 Sep 2025", or the raw value if it is not a date."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d %b %Y")
    except (TypeError, ValueError):
        return str(value)


def inspect_database(path: str) -> dict:
    """What the published database actually contains.

    The event says a file of some size arrived. That is enough to know a run
    finished and not enough to know what it produced, so the mail opens the
    database and reports the tables, their row counts and the span of price
    history in them -- the figures you would otherwise open a SQL client to
    check.

    Read-only, on a copy in /tmp: nothing here can alter what was published.

    Returns:
        ``{"tables": [(name, rows)], "first_date", "last_date", "universe"}``,
        with any key absent when the database does not carry it.
    """
    found: dict = {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name COLLATE NOCASE"
            )
        ]

        tables = []
        dates: list[str] = []
        for name in names:
            quoted = name.replace('"', '""')
            tables.append((name, conn.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0]))

            # Only the price tables carry dates, and they are not named
            # consistently -- the universe history is named for the exchange,
            # the matched-ticker history for the config. Ask each table what
            # columns it has instead of hardcoding either.
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{quoted}")')}
            if DATE_COLUMN in columns:
                span = conn.execute(
                    f'SELECT MIN("{DATE_COLUMN}"), MAX("{DATE_COLUMN}") FROM "{quoted}"'
                ).fetchone()
                dates += [value for value in span if value]

        found["tables"] = tables
        if dates:
            found["first_date"] = _pretty_date(min(dates))
            found["last_date"] = _pretty_date(max(dates))

        # One row, written by the publish step. Checked for presence rather
        # than queried and caught: a database built by a run that never
        # reached the analysis manifest has no such table, and letting that
        # raise would lose the table list above with it.
        if "run_metadata" in names:
            row = conn.execute(
                "SELECT exchange, instrument_type FROM run_metadata LIMIT 1"
            ).fetchone()
            if row and row[0]:
                found["universe"] = universe_label(row[0], row[1] or "")
    finally:
        conn.close()

    return found


def contents_of(bucket: str, key: str) -> dict:
    """Download the published database and inspect it.

    Failure here must not cost the notification. The mail's job is to say a
    database landed, and it can still say that without the table list, so
    every way this can fail -- a slow download, a missing permission, a file
    that is not SQLite -- degrades to a shorter mail rather than to silence
    and an alarm.
    """
    path = os.path.join("/tmp", os.path.basename(key))  # noqa: S108 - the only writable path
    try:
        _s3_client().download_file(bucket, key, path)
        return inspect_database(path)
    except Exception:
        logger.exception("Could not inspect s3://%s/%s; sending without contents", bucket, key)
        return {}
    finally:
        # Execution environments are reused, and the next invocation is for a
        # different database on the same 512 MB of /tmp.
        if os.path.exists(path):
            os.remove(path)


def details_for(event: dict, zone, contents: dict | None = None) -> dict:
    """Everything both renderings need: one S3 event, plus what it contains."""
    contents = contents or {}
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
        # Absent when the database could not be read; every rendering below
        # treats each of these as optional.
        "universe": contents.get("universe", ""),
        "tables": contents.get("tables", []),
        "prices_from": contents.get("first_date", ""),
        "prices_to": contents.get("last_date", ""),
    }


def lead_for(d: dict) -> str:
    """The opening sentence, naming the universe when the database says so."""
    who = f" for {d['universe']} tickers" if d["universe"] else ""
    return (
        f"Stock price history has been downloaded{who} and analysis is done. "
        f"Database {d['uri']} is ready to use."
    )


def subject_for(d: dict) -> str:
    """The inbox line. Names the database and its size, because those are the
    two things worth knowing without opening the mail."""
    return f"stocks: {d['key']} is ready to use ({d['size']})"


def render_text(d: dict) -> str:
    """The plain-text alternative.

    Sent alongside the HTML, not instead of it: a mail with no text part
    scores worse with spam filters and is unreadable in a client that refuses
    HTML.
    """
    lines = [
        f"{d['key']} is ready to use.",
        "",
        lead_for(d),
        "",
        f"Published  {d['clock']} on {d['date']}",
    ]
    if d["prices_from"]:
        lines.append(f"Prices     {d['prices_from']} to {d['prices_to']}")
    lines.append(f"Size       {d['size']} ({d['bytes']} bytes)")

    if d["tables"]:
        width = max(len(name) for name, _ in d["tables"])
        lines += ["", "Tables"]
        lines += [f"  {name:<{width}}  {rows:>9,}" for name, rows in d["tables"]]

    lines += ["", "Sent when the object reached S3, not when the task exited."]
    return "\n".join(lines) + "\n"


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

_TABLE_ROW = (
    "<tr>"
    '<td style="padding:7px 0;border-top:1px solid #f0f2f4;'
    "font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
    'font-size:13px;color:#0f1720;overflow-wrap:break-word;">{name}</td>'
    '<td align="right" style="padding:7px 0;border-top:1px solid #f0f2f4;'
    'font-size:13px;color:#5b6672;white-space:nowrap;">{rows}</td>'
    "</tr>"
)

_TABLES_BLOCK = """<tr><td style="padding:6px 28px 0;">
<div style="color:#8a949e;font-size:13px;padding-bottom:2px;">Tables</div>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
{rows}
</table>
</td></tr>"""


def render_html(d: dict) -> str:
    """The HTML body.

    Deliberately one card and no images: the mail is read on a phone at
    breakfast, and the table list is the only part that can run long.
    """
    e = {k: html.escape(str(v)) for k, v in d.items()}

    # The publish time is the headline's subtitle, so it is deliberately not
    # repeated in the fields.
    fields = []
    if d["prices_from"]:
        fields.append(("Prices", f"{e['prices_from']} &rarr; {e['prices_to']}"))
    # No "Object" row: the full path is already in the sentence above, and
    # repeating a 30-character URI in a 390px column earns nothing.
    fields.append(("Size", f'{e["size"]} <span style="color:#8a949e;">({e["bytes"]} bytes)</span>'))
    rows = "".join(_ROW.format(label=label, value=value) for label, value in fields)

    tables = ""
    if d["tables"]:
        tables = _TABLES_BLOCK.format(
            rows="".join(
                _TABLE_ROW.format(name=html.escape(name), rows=f"{count:,}")
                for name, count in d["tables"]
            )
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{e["key"]} is ready to use</title>
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
font-weight:650;">{e["key"]} is ready to use</h1>
<p style="margin:0;color:#0f7b4f;font-size:15px;font-weight:600;">
Published {e["clock"]} <span style="color:#8a949e;font-weight:400;">&middot;
{e["date"]}</span></p>
</td></tr>

<tr><td style="padding:18px 28px 0;">
<p style="margin:0;color:#39424c;font-size:15px;line-height:1.6;">
Stock price history has been downloaded{
        f" for {e['universe']} tickers" if d["universe"] else ""
    } and analysis is done.
Database <span style="font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,
monospace;font-size:13px;overflow-wrap:break-word;">{e["uri"]}</span>
is ready to use.</p>
</td></tr>

<tr><td style="padding:20px 28px 4px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
 style="border-top:1px solid #eceef0;padding-top:18px;">
{rows}
</table>
</td></tr>

{tables}

<tr><td style="padding:18px 28px 26px;">
<p style="margin:0;color:#8a949e;font-size:13px;line-height:1.6;">
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
    detail = event.get("detail", {})
    contents = contents_of(
        detail.get("bucket", {}).get("name", ""), detail.get("object", {}).get("key", "")
    )
    details = details_for(event, _zone(TIMEZONE), contents)

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
