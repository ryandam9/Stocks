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

## Supported universes

| `--exchange` | Covers | Shipped config |
|---|---|---|
| `US` | The whole US listed universe: Nasdaq, NYSE, NYSE American, NYSE Arca, Cboe BZX, IEX | ✅ `config/us_stocks_config.yaml` |
| `ASX` | Australian Securities Exchange | ✅ `config/asx_etf_config.yaml` |
| `NASDAQ` | Nasdaq-listed only | — add a config to use |
| `NYSE` | NYSE, NYSE American, NYSE Arca | — add a config to use |
| `NSE` / `BSE` | India | — add a config to use |

`US` is the shipped US universe because the exchange symbol directory covers
every US venue and links are built per ticker. `NASDAQ` and `NYSE` remain
available if you want a venue-restricted universe; create
`config/<exchange>_<type>_config.yaml` and run `universe.py sync` against it.

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
# 1. Sync the instrument universe from the exchange symbol directory
uv run src/universe.py sync US stocks

# 2. Fetch prices (logs to logs/, rotated)
./scripts/fetch_prices.sh US stocks 365

# 3. Analyse growth and load into SQLite
./scripts/run_analysis.sh US stocks

# Optional: upload the DB to S3
S3_BUCKET=s3://your-bucket S3_REGION=ap-southeast-2 \
  ./scripts/run_analysis.sh US stocks --upload
```

Python entry points, for more control:

```bash
uv run src/fetch_prices.py --exchange US --instrument-type stocks \
    --period 365 --batch-size 100
uv run src/analysis.py --exchange US --instrument-type stocks
uv run src/universe.py sync US stocks       # membership + class, US directory
uv run src/universe.py enrich ASX etf           # metadata only, any market
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

### Dataset completeness

Screening is cross-sectional, so a run missing part of its universe can look
entirely plausible while omitting most of the candidates. The fetch therefore
refuses to publish unless `--min-success-ratio` (default 0.95) of requested
tickers returned data:

```
Error: Only 2,800/5,000 tickers (56.0%) returned data, below the required
95.0%. The previous price file has been left untouched.
```

The check runs *before* the old price file is overwritten, so a bad run leaves
the last good dataset intact. `--allow-partial` overrides it.

### Data freshness

Per-ticker staleness is measured against the dataset's own latest date, which
cannot detect that the dataset as a whole is old. Analysis therefore refuses to
run when the newest row is older than `max_data_age_days`:

```
Error: Price data is 34 days old (newest row 2026-07-19, limit 5 days).
Re-run the fetch, or pass --allow-stale to screen it anyway.
```

## The instrument universe

A universe file lists what to screen. For US listings it is built from the
exchange's own symbol directory (`nasdaqlisted.txt` / `otherlisted.txt`), which
is the only source that reliably distinguishes a SPAC's share classes — all
three carry the same company name and the price provider reports every one of
them as `EQUITY`:

```
AACB   Artius II Acquisition Inc. - Class A Ordinary Shares   common_stock
AACBR  Artius II Acquisition Inc. - Rights                    right
AACBU  Artius II Acquisition Inc. - Units                     unit
```

### Instrument types

Every instrument is classified into exactly one `asset_type`. `asset_types` in
the config selects which of them a screen covers.

| `asset_type` | What it is | In screens by default |
|---|---|---|
| `common_stock` | Ordinary equity: common stock, ordinary shares, capital stock, subordinate voting shares, ADRs/ADSs and registry shares, REITs, BDCs, and MLP common units | ✅ for `stocks` |
| `etf` | Pooled vehicles: exchange-traded funds and closed-end funds (from the directory's authoritative ETF flag) | ✅ for `etf` |
| `warrant` | Warrants — a right to buy shares later, not the shares | ❌ |
| `unit` | SPAC units, usually one share bundled with a fraction of a warrant | ❌ |
| `right` | Rights, entitling the holder to a fraction of a share | ❌ |
| `preferred` | Preferred stock and depositary shares representing it | ❌ |
| `note` | Exchange-traded debt: notes due, debentures | ❌ |
| `unknown` | Class could not be established from the security name | ❌ |

Current composition of the shipped universes:

```
config/us_stocks.csv  (13,135)      config/asx_etf.csv  (478)
  common_stock   5,750                etf   477
  etf            5,670                unit    1
  preferred        487
  warrant          473
  unit             295
  note             164
  unknown          156
  right            140
```

`unknown` is never included implicitly: an instrument whose class could not be
established is excluded unless `asset_types` names it, so a derivative can
never enter a screen by defaulting into common stock. To screen them anyway,
list the type explicitly:

```yaml
asset_types: [common_stock, unknown]   # accept unclassified instruments too
```

Warrants and units genuinely trade, so screening them is a legitimate choice —
just an explicit one.

Two commands, deliberately distinct:

| Command | Does |
|---|---|
| `universe.py sync <EX> <TYPE>` | Replaces membership *and* metadata from the US symbol directory, reporting adds/removes |
| `universe.py enrich <EX> <TYPE>` | Fills metadata for tickers already in the file via provider lookups; works for any market |

The structured form carries real metadata:

```csv
ticker,name,exchange,asset_type,currency,source_date
A,Agilent Technologies Inc.,NYSE,common_stock,,2026-08-22
AAPL,Apple Inc.,NASDAQ,common_stock,,2026-08-22
AACIW,Armada Acquisition Corp. III Warrant,NASDAQ,warrant,,2026-08-22
```

This matters for two reasons:

- **A US universe is not one exchange.** `AAPL` is Nasdaq; `A`, `AA` and
  `ABBV` are NYSE; `SPY` is NYSE Arca. Links are built from each ticker's own
  venue, and where the venue is unknown no link is emitted rather than a wrong
  one. This is why the universe is `US` rather than `NASDAQ`: a recent
  one-year screen returned 604 Nasdaq names and 595 NYSE ones.
- **It is not only common stock.** The directory lists warrants, units,
  rights, preferred lines and notes alongside ordinary shares. See
  [Instrument types](#instrument-types) below.

Legacy `TICKER~Name` files still load — asset type is inferred from the
security name and exchange is recorded as `UNKNOWN`. Upgrade one with:

```bash
uv run src/universe.py sync US stocks
```

## Configuration

One file per exchange/instrument pair in `config/`:

```yaml
config:
  ticker_file: config/us_stocks.csv       # relative to the repo
  data_dir: us/stocks                     # relative to STOCKS_DATA_ROOT
  db_path: us.db

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
| `<prefix>_fetch_manifest.json` | Fetch provenance: run id, requested/succeeded counts, success ratio, `data_as_of` |
| `<prefix>_analysis_manifest.json` | Analysis provenance: run id, code revision, thresholds, funnel counts, and the `source_run_id` of the fetch that produced the price file |

Every growth file is written on every run, empty-but-headed when nothing
qualifies, so a later run can never leave an earlier run's results in place to
be republished as current. Windows are published only after all of them have
computed, so a failure part-way cannot produce a mixed-generation output set.
CSVs are written to a process-unique temporary file and renamed into place; the
SQLite database is built in full and then moved over the published one. Growth
tables are created from a declared schema, so their column types do not change
when a screen happens to be empty.

SQLite tables mirror those CSVs, plus `consistent_growth_stocks` — tickers
that qualified in **every** configured window.

## Tests

```bash
uv run pytest          # 121 tests, fully offline
uv run ruff check src tests
```

Provider calls are stubbed throughout, so the suite never depends on Yahoo
being reachable.

## Resources
- [Nasdaq Trader symbol directory](https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs) — the authoritative source `universe.py sync` reads
- [Nasdaq Screener](https://www.nasdaq.com/market-activity/stocks/screener)
