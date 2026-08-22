# Stocks Data Fetcher

Fetches historical end-of-day market data from Yahoo Finance, identifies
tickers that grew materially over trailing windows, and loads the results into
SQLite.

## Setup

This project uses [`uv`](https://github.com/astral-sh/uv).

```bash
uv venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
uv pip install -r requirements.txt
```

`scripts/run_analysis.sh` also needs the `sqlite3` and `sqlite-utils` CLIs on
your `PATH`.

## Where data goes

Generated CSVs and SQLite databases are written under `<repo>/data` by default.
Set `STOCKS_DATA_ROOT` to put them somewhere else:

```bash
export STOCKS_DATA_ROOT=~/Desktop/temp/data
```

Ticker lists in `config/` are repo inputs and are always read from the repo,
regardless of that variable.

## Usage

```bash
# 1. Fetch prices (logs to logs/, rotated)
./scripts/fetch_prices.sh NASDAQ stocks 365
./scripts/fetch_prices.sh ASX etf 365

# 2. Analyse growth and load into SQLite
./scripts/run_analysis.sh NASDAQ stocks

# Optional: also upload the DB to S3
S3_BUCKET=s3://your-bucket S3_REGION=ap-southeast-2 \
  ./scripts/run_analysis.sh NASDAQ stocks --upload
```

The Python entry points can be run directly for more control:

```bash
uv run src/fetch_prices.py --exchange NASDAQ --instrument-type stocks \
    --period 365 --batch-size 100
uv run src/analysis.py --exchange NASDAQ --instrument-type stocks
uv run src/fetch_prices.py --help
```

## How growth is measured

For each configured window, a ticker's growth is the percentage change between
a **robust** opening and closing price:

```
pct_change = (median(last N closes) / median(first N closes) - 1) * 100
```

Prices are **split- and dividend-adjusted** (`adj_close`). Endpoints are the
median of `endpoint_window` trading days rather than a single close, so one bad
print cannot define a window's return.

A ticker is only reported for a window if it passes every eligibility rule:

| Rule | Setting | Why |
|---|---|---|
| Coverage | `min_coverage` (0.8) | The ticker must span most of the window. Without this a stock listed two weeks ago appears in the 1-year table with its two-week return. |
| Freshness | 10 calendar days | A ticker with no recent prints is suspended or delisted, not a growth pick. |
| Liquidity | `min_median_volume` | A percentage move in a name trading a few hundred shares a day is not realisable. |
| Price floor | `min_price` | Filters sub-dollar names whose percentages are noise. Denominated in the exchange's own currency, so it is set per config. |

Each run prints how many tickers each rule excluded, so an empty result is
never ambiguous.

## Configuration

One file per exchange/instrument pair in `config/`, e.g.
`config/nasdaq_stocks_config.yaml`:

```yaml
config:
  ticker_file: config/nasdaq_stocks.csv   # relative to the repo
  data_dir: nasdaq/stocks                 # relative to STOCKS_DATA_ROOT
  db_path: nasdaq.db

  analysis:
    min_price: 10.0
    min_median_volume: 50000
    min_coverage: 0.8
    endpoint_window: 3
    windows:
      - {months: 12, label: 1_year, threshold: 25.0}
      - {months: 6, label: 6_months, threshold: 25.0}
      - {months: 3, label: 3_months, threshold: 25.0}
      - {months: 1, label: 1_month, threshold: 10.0}
```

Ticker files are `TICKER~Name` per line; blank lines and `#` comments are
skipped.

## Outputs

| File | Contents |
|---|---|
| `<prefix>_eod.csv` | Full price history, one row per ticker per day |
| `<prefix>_eod_growth_<label>.csv` | Qualifying tickers for one window, with `pct_change`, coverage and volume diagnostics |
| `<prefix>_eod_growth.csv` | Price history for every ticker that grew in any window, with `growth_count` and `growth_periods` |
| `<prefix>_error.csv` | Tickers that returned no data |

SQLite tables mirror those CSVs, plus `consistent_growth_stocks` — tickers that
qualified in **every** configured window.

## Tests

```bash
uv run pytest
```

## Resources
- [NASDAQ Screener](https://www.nasdaq.com/market-activity/stocks/screener)
