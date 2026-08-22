# Stocks Data Fetcher

Fetches historical end-of-day market data from Yahoo Finance, identifies
tickers that grew materially over trailing windows, and loads the results into
SQLite.

Each run is a self-contained, self-identifying snapshot: it publishes a
complete set of outputs, stamps every row with a `run_id` and `data_as_of`
date, and refuses to screen price data that has gone stale.

## Setup

This project uses [`uv`](https://github.com/astral-sh/uv).

```bash
uv venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
uv pip install -r requirements.lock
```

`requirements.txt` is the human-readable declaration; `requirements.lock` pins
transitive versions and is what CI and reproducible installs use.

### Requirements

- **Python 3.12+**
- **Bash 4.4+** for `scripts/*.sh` (they use `mapfile` and `${var,,}`; macOS
  ships Bash 3.2, so install a newer Bash or call the Python entry points
  directly)
- **`sqlite3`** and **`sqlite-utils`** on `PATH` for `run_analysis.sh`
- `aws` CLI only if you use `--upload`

The Python entry points themselves are cross-platform; only the shell
orchestration is Bash-specific.

## Where data goes

Generated CSVs and databases are written under `<repo>/data` by default. Set
`STOCKS_DATA_ROOT` to relocate them:

```bash
export STOCKS_DATA_ROOT=~/Desktop/temp/data
```

Universe files in `config/` are repo inputs and are always read from the repo,
regardless of that variable.

## Usage

```bash
# 1. Fetch prices (logs to logs/, rotated)
./scripts/fetch_prices.sh NASDAQ stocks 365

# 2. Analyse growth and load into SQLite
./scripts/run_analysis.sh NASDAQ stocks

# Optional: upload the DB to S3
S3_BUCKET=s3://your-bucket S3_REGION=ap-southeast-2 \
  ./scripts/run_analysis.sh NASDAQ stocks --upload
```

Python entry points, for more control:

```bash
uv run src/fetch_prices.py --exchange NASDAQ --instrument-type stocks \
    --period 365 --batch-size 100
uv run src/analysis.py --exchange NASDAQ --instrument-type stocks
uv run src/universe.py refresh NASDAQ stocks     # enrich the universe file
```

## How growth is measured

For each configured window, a ticker's growth is the percentage change between
a **robust** opening and closing price:

```
pct_change = (median(last N closes) / median(first N closes) - 1) * 100
```

Prices are split- and dividend-adjusted. Endpoints are the median of
`endpoint_window` trading days rather than a single close, so one bad print
cannot define a window's return.

Every run prints an eligibility funnel, so an empty result is never ambiguous:

```
--- Tickers with >25.0% growth over the last 1_year ---
    Universe in window             6,157
    Enough span                    5,570
    Enough observations            5,570
    Still trading                  5,570
    Adjusted prices                5,570
    Liquid enough                  4,302
    Above price floor              2,830
    Valid baseline                 2,830
    Return above 25.0%             1,285
```

| Stage | Setting | Why |
|---|---|---|
| Enough span | `min_coverage` | The ticker must span most of the window. Without this a stock listed two weeks ago appears in the 1-year table with its two-week return. |
| Enough observations | `min_observation_ratio` | Span alone is not coverage: two prints a year apart span the window with no usable history in between. Also requires `2 × endpoint_window` prints so the endpoints cannot overlap. |
| Still trading | 10 calendar days | A ticker with no recent prints is suspended or delisted, not a growth pick. |
| Adjusted prices | `price_basis` | Rows the fetcher recorded as a raw-close fallback are excluded; an unadjusted series spanning a split produces a badly wrong return. Files predating provenance are screened with a warning rather than dropped. |
| Liquid enough | `min_median_volume` | A percentage move in a name trading a few hundred shares a day is not realisable. |
| Above price floor | `min_price` | Filters sub-dollar names whose percentages are noise. Denominated in the exchange's own currency, so it is set per config. |

### Data freshness

Per-ticker staleness is measured against the dataset's own latest date, which
cannot detect that the dataset as a whole is old. Analysis therefore refuses to
run when the newest row is older than `max_data_age_days`:

```
Error: Price data is 34 days old (newest row 2026-07-19, limit 5 days).
Re-run the fetch, or pass --allow-stale to screen it anyway.
```

## The instrument universe

A universe file lists what to screen. The structured form carries real
metadata:

```csv
ticker,name,exchange,asset_type,currency,source_date
A,Agilent Technologies Inc.,NYSE,common_stock,,2026-08-22
AAPL,Apple Inc.,NASDAQ,common_stock,,2026-08-22
AACIW,Armada Acquisition Corp. III Warrant,NASDAQ,warrant,,2026-08-22
```

This matters for two reasons:

- **The NASDAQ screener file is not only NASDAQ.** `A`, `AA` and `ABBV` are
  NYSE-listed. Links are built from each ticker's own exchange; where the
  exchange is unknown, no link is emitted rather than a wrong one.
- **It is not only common stock.** The file contains warrants, units, rights,
  preferred lines and notes. `asset_types` in the config selects what to
  screen; derivative classes are excluded by default.

Legacy `TICKER~Name` files still load — asset type is inferred from the
security name and exchange is recorded as `UNKNOWN`. Upgrade one with:

```bash
uv run src/universe.py refresh NASDAQ stocks
```

## Configuration

One file per exchange/instrument pair in `config/`:

```yaml
config:
  ticker_file: config/nasdaq_stocks.csv   # relative to the repo
  data_dir: nasdaq/stocks                 # relative to STOCKS_DATA_ROOT
  db_path: nasdaq.db

  analysis:
    min_price: 10.0
    min_median_volume: 50000
    min_coverage: 0.8
    min_observation_ratio: 0.5
    endpoint_window: 3
    max_data_age_days: 5
    asset_types: [common_stock]
    windows:
      - {months: 12, label: 1_year, threshold: 25.0}
      - {months: 6, label: 6_months, threshold: 25.0}
      - {months: 3, label: 3_months, threshold: 25.0}
      - {months: 1, label: 1_month, threshold: 10.0}
```

Values are validated at load: ranges are checked, and window labels must be
unique and safe as filenames and SQLite identifiers.

## Outputs

| File | Contents |
|---|---|
| `<prefix>_eod.csv` | Full price history, one row per ticker per day |
| `<prefix>_eod_growth_<label>.csv` | Qualifying tickers for one window, with diagnostics |
| `<prefix>_eod_growth.csv` | Price history for every ticker that grew in any window, with `growth_count` and `growth_periods` |
| `<prefix>_error.csv` | Tickers that returned no data, with error type |
| `<prefix>_analysis_manifest.json` | Run provenance: run id, code revision, thresholds, funnel counts, `data_as_of` |

Every growth file is written on every run, empty-but-headed when nothing
qualifies, so a later run can never leave an earlier run's results in place to
be republished as current. CSVs are written to a temporary file and renamed
into place; the SQLite database is built in full and then moved over the
published one.

SQLite tables mirror those CSVs, plus `consistent_growth_stocks` — tickers
that qualified in **every** configured window.

## Tests

```bash
uv run pytest          # 58 tests, fully offline
uv run ruff check src tests
```

Provider calls are stubbed throughout, so the suite never depends on Yahoo
being reachable.

## Resources
- [NASDAQ Screener](https://www.nasdaq.com/market-activity/stocks/screener)
