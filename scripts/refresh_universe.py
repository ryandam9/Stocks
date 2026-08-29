#!/usr/bin/env python3
"""Update a committed universe CSV against the exchange's own listings.

Run this on a developer machine, review the diff, commit it, then build the
image. It is deliberately not part of the pipeline: nothing at runtime adds a
ticker or removes one, so the committed CSV is the only thing that decides
what gets screened, and a change to it is reviewable in git like any other.

The two markets need different treatment, because their sources are different
kinds of thing.

US -- Nasdaq Trader publishes ``nasdaqlisted.txt`` and ``otherlisted.txt``,
which between them list every US-listed security and carry an authoritative
ETF flag. Membership is simply whatever those files say.

ASX -- there is no such file. The company directory gives membership but not
classification: it lists every quoted code, ETPs included, without saying
which are funds. So an ASX refresh drops what the directory no longer lists
and asks the price provider about each *new* code before letting it into a
fund universe. That lookup happens here, once, rather than on every run.

    uv run scripts/refresh_universe.py --exchange US  --instrument-type stocks
    uv run scripts/refresh_universe.py --exchange ASX --instrument-type etf --dry-run

If the exchange blocks the download -- the ASX does, intermittently -- save
the file by hand and pass --from-file. That also accepts the ASX Investment
Products report, the monthly PDF listing exchange-traded products and nothing
else:

    uv run scripts/refresh_universe.py --exchange ASX --instrument-type etf \
        --from-file asx-investment-products-aug-2026.pdf --dry-run

which is the better ASX source when you can get it: the company directory
says which codes exist and never which of them are funds.
"""

import datetime
import os
import sys

try:
    import click
    import pandas as pd
except ImportError as exc:  # pragma: no cover - depends on how it was invoked
    raise SystemExit(
        f"{exc.name} is missing: this reads the project's own modules, so it "
        f"needs the project environment.\n"
        f"    uv run scripts/refresh_universe.py --exchange US --instrument-type stocks"
    ) from exc

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from config import load_config  # noqa: E402
from symbol_directory import (  # noqa: E402
    US_EXCHANGES,
    fetch_asx_directory,
    fetch_symbol_directory,
    parse_asx_report_text,
    read_pdf_text,
)
from universe import (  # noqa: E402
    UNIVERSE_COLUMNS,
    default_asset_type_for,
    load_universe,
    write_universe,
)

# A refresh that would drop more than this share of the universe is refusing to
# run. The failure it guards against is not a wave of delistings -- it is a
# source that changed shape, or one that turns out not to list funds at all,
# in which case every ETF looks delisted at once.
MAX_DROP_SHARE = 0.10

# Provider instrument types that belong in a fund universe.
_FUND_TYPES = {"ETF", "MUTUALFUND"}


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def is_asx_report(from_file: str | None) -> bool:
    """Whether the supplied file is the ASX Investment Products report."""
    return bool(from_file) and from_file.lower().endswith(".pdf")


def fetch_directory(exchange: str, from_file: str | None) -> pd.DataFrame:
    """The exchange's own list of what is currently quoted."""
    if exchange in US_EXCHANGES:
        if from_file:
            # Both files are needed; the second is expected beside the first.
            other = from_file.replace("nasdaqlisted", "otherlisted")
            return fetch_symbol_directory(nasdaq_text=_read(from_file), other_text=_read(other))
        return fetch_symbol_directory()
    if is_asx_report(from_file):
        return parse_asx_report_text(read_pdf_text(from_file))
    return fetch_asx_directory(text=_read(from_file) if from_file else None)


def classify_new_asx(tickers: list[str]) -> dict[str, str]:
    """Ask the price provider what each new ASX code actually is.

    Only new codes, and only on this machine: a handful a month against a
    universe of hundreds. The ASX directory cannot answer this, and guessing
    from the name is what put eight operating companies into an ETF screen.
    """
    import yfinance as yf

    resolved = {}
    for ticker in tickers:
        try:
            metadata = yf.Ticker(f"{ticker}.AX").history_metadata or {}
        except Exception:
            metadata = {}
        # "" means the provider did not answer, which is not the same as
        # answering "not a fund". Treating the two alike would drop a genuine
        # new ETF on the floor every time the provider throttles.
        resolved[ticker] = str(metadata.get("instrumentType") or "").upper()
    return resolved


@click.command(help=__doc__)
@click.option("--exchange", required=True)
@click.option("--instrument-type", required=True)
@click.option("--from-file", type=click.Path(exists=True), help="A directory file saved by hand")
@click.option(
    "--trust-file",
    is_flag=True,
    help="Treat every row in --from-file as this instrument type, skipping the provider lookup "
    "(implied for the ASX Investment Products PDF)",
)
@click.option("--dry-run", is_flag=True, help="Report the change without writing it")
@click.option("--force", is_flag=True, help="Write even if the drop guard trips")
def main(exchange, instrument_type, from_file, trust_file, dry_run, force):
    exchange = exchange.upper()
    if trust_file and not from_file:
        raise SystemExit("--trust-file only means something with --from-file")
    cfg = load_config(exchange, instrument_type)
    path = cfg.bundled_ticker_file
    default_type = default_asset_type_for(cfg.instrument_type)

    existing = load_universe(path, default_asset_type=default_type)
    print(f"{path}: {len(existing)} tickers")

    if is_asx_report(from_file):
        # The report covers the whole ASX product suite -- listed investment
        # companies, A-REITs and infrastructure funds share its tables with
        # the funds -- but it names the form of each security in the column
        # beside its code, and the parser keeps only the fund forms. So the
        # document has already classified every row it returns, and asking
        # the provider to confirm it would only add a way to fail.
        trust_file = True
        print("reading the ASX Investment Products report (funds and ETPs only)")

    directory = fetch_directory(exchange, from_file)
    print(f"exchange directory: {len(directory)} listings")
    if is_asx_report(from_file):
        for row in directory.head(3).itertuples():
            print(f"    e.g. {row.ticker:<8} {row.name[:52]}")

    if exchange in US_EXCHANGES:
        wanted = {e.upper() for e in US_EXCHANGES[exchange]}
        directory = directory[directory["exchange"].str.upper().isin(wanted)]

    active = set(directory["ticker"])
    held = set(existing["ticker"])
    removed = sorted(held - active)
    candidates = sorted(active - held)

    share = len(removed) / max(1, len(existing))
    if share > MAX_DROP_SHARE and not force:
        raise SystemExit(
            f"\nRefusing to write: {len(removed)} of {len(existing)} tickers "
            f"({share:.0%}) are missing from the directory, above the "
            f"{MAX_DROP_SHARE:.0%} guard.\nThat usually means the source changed "
            f"shape or does not list this kind of instrument, not that the "
            f"market delisted them.\nCheck the download, then re-run with "
            f"--force if the drop is real."
        )

    # Which of the new listings belong in *this* universe.
    if trust_file:
        # The file is the list, not a whole-market directory: an ETP report,
        # or a hand-built one. Asking the provider to confirm what the file
        # already asserts would only add a way for the run to fail.
        added, skipped, unresolved = candidates, 0, []
    elif exchange in US_EXCHANGES:
        # Everything the directory lists, whatever its type. A US universe
        # file mirrors the exchange -- 13,135 rows across eight asset types --
        # and the config's asset_types decides what is screened out of it at
        # read time. Adding only the configured type dropped 46 new listings
        # in a single week and would quietly stop the file being a mirror.
        added, skipped, unresolved = candidates, 0, []
    elif candidates:
        print(f"asking the provider about {len(candidates)} new ASX code(s)...")
        kinds = classify_new_asx(candidates)
        is_fund = default_type in {"etf"}
        unresolved = sorted(t for t in candidates if not kinds[t])
        added = sorted(t for t in candidates if kinds[t] and (kinds[t] in _FUND_TYPES) == is_fund)
        skipped = len(candidates) - len(added) - len(unresolved)
    else:
        added, skipped, unresolved = [], 0, []

    names_preview = directory.set_index("ticker")["name"]
    print(f"\n  removed  {len(removed):>5}   no longer listed")
    print(f"  added    {len(added):>5}   newly listed")
    if skipped:
        print(f"  skipped  {skipped:>5}   new listings of another type")
    print(f"  kept     {len(held & active):>5}")
    if added and "asset_type" in directory.columns:
        kinds = directory.set_index("ticker")["asset_type"].reindex(added).value_counts()
        print("           " + ", ".join(f"{n} {kind}" for kind, n in kinds.items()))
    for ticker in removed[:20]:
        name = existing.loc[existing["ticker"] == ticker, "name"].iloc[0]
        print(f"    - {ticker:<8} {name[:56]}")
    if len(removed) > 20:
        print(f"    - ... and {len(removed) - 20} more")
    names = names_preview
    for ticker in added[:20]:
        print(f"    + {ticker:<8} {str(names.get(ticker, ''))[:56]}")
    if len(added) > 20:
        print(f"    + ... and {len(added) - 20} more")
    if unresolved:
        # Not silently dropped: a provider that did not answer is the one case
        # where doing nothing loses a real listing.
        print(
            f"\n  {len(unresolved)} new code(s) the provider would not classify. "
            f"They are NOT added; re-run when it answers, or add them by hand:"
        )
        for ticker in unresolved[:20]:
            print(f"    ? {ticker:<8} {str(names_preview.get(ticker, ''))[:56]}")

    if dry_run:
        print("\n--dry-run: nothing written")
        return

    today = datetime.date.today().isoformat()
    kept = existing[existing["ticker"].isin(active)].copy()
    # Refresh what the directory is authoritative about, and leave alone what
    # it is not: issuer and category are ours, and a hand correction to either
    # has to survive this.
    kept["name"] = kept["ticker"].map(names).fillna(kept["name"])
    kept["source_date"] = today

    fresh = pd.DataFrame(
        [
            {
                "ticker": ticker,
                "name": str(names.get(ticker, "")),
                "exchange": exchange if exchange not in US_EXCHANGES else "",
                "asset_type": default_type,
                "issuer": "",
                "category": "",
                "currency": "",
                "source_date": today,
            }
            for ticker in added
        ],
        columns=UNIVERSE_COLUMNS,
    )
    if exchange in US_EXCHANGES and not fresh.empty:
        directory_rows = directory.set_index("ticker")
        fresh["exchange"] = fresh["ticker"].map(directory_rows["exchange"])
        fresh["asset_type"] = fresh["ticker"].map(directory_rows["asset_type"])
        fresh["currency"] = "USD"

    combined = pd.concat([kept, fresh], ignore_index=True).sort_values("ticker")
    write_universe(combined, path)

    # Read it back through the loader, which fills issuer and category for the
    # new rows exactly as the pipeline would, then write the filled version.
    write_universe(load_universe(path, default_asset_type=default_type), path)
    print(f"\nwrote {len(combined)} tickers to {path}")
    print("Review with `git diff`, commit, then build the image.")


if __name__ == "__main__":
    main()
