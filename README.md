# Stock Growth Screener

Screens US common stock and ASX ETFs for sustained growth. Keeps each
instrument universe current against its exchange, fetches a year of end-of-day
prices from Yahoo Finance, measures returns over five trailing windows, and
publishes the results as SQLite databases.

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

Generated CSVs and databases are written under `<repo>/data` by default. To put
them elsewhere, create a `.env` in the repo root (copy `.env.example`):

```bash
cp .env.example .env
# then edit:
STOCKS_DATA_ROOT=/home/you/market-data
```

`.env` is untracked and read automatically by every command, so nothing needs
exporting. An explicitly exported `STOCKS_DATA_ROOT` still overrides it for a
one-off run.

Check where any command will read and write:

```bash
uv run src/config.py US stocks db_path
uv run src/config.py US stocks eod_csv
```

Universe files in `config/` are repo inputs and are always read from the repo,
regardless of that variable.

## End to end

Two universes ship ready to run: **US stocks** and **ASX ETFs**. Nothing needs
exporting — `.env` supplies the data location (see [Where data
goes](#where-data-goes)).

### US stocks — full run

```bash
cd /path/to/stocks

# 1. Sync the instrument universe from the exchange symbol directory.
#    Adds new listings, drops delisted ones, and classifies every security.
#    Optional: skip it to keep the committed snapshot exactly as-is.
uv run src/universe.py sync US stocks

# 2. Fetch ~1 year of end-of-day prices. Only common stock is requested.
./scripts/fetch_prices.sh US stocks 365

# 3. Screen for growth and publish the SQLite database.
./scripts/run_analysis.sh US stocks
```

What you should see:

```
# step 1 — nothing changed since the last sync in this example
Downloading symbol directory for US...
  13135 symbols (+0 added, -0 removed, 13135 retained)
Asset types:
  common_stock 5750 | etf 5670 | preferred 487 | warrant 473 | unit 295 | ...

# step 2
  Universe: 5750 of 13135 instruments match ['common_stock'] (7385 excluded)
  Batch 58/58 (50 tickers)
  FETCH SUMMARY: 5749/5750 tickers fetched successfully
  Data saved to: .../us/stocks/us_stocks_eod.csv

# step 3
  Data as of 2026-08-21 (1 day(s) old)
  --- Tickers with >25.0% growth over the last 1_year ---
      Universe in window             5,749
      Enough span                    5,372
      Enough observations            5,372
      Still trading                  5,372
      Adjusted prices                5,372
      Liquid enough                  4,200
      Above price floor              2,761
      Valid baseline                 2,761
      Return above 25.0%             1,171
  Loaded 1171 rows -> us_stocks_growth_1_year
  consistent_growth_stocks: 152 rows
  DB published: .../us.db
```

Step 2 takes about 4 minutes; step 3 about 40 seconds.

### ASX ETFs — full run

```bash
cd /path/to/stocks

# 1. Refresh metadata for the existing universe, dropping delisted funds.
#    ASX has no bulk symbol directory, so this uses per-ticker lookups and
#    "sync" does not apply. --prune removes funds the provider no longer
#    lists, which otherwise fail every fetch forever.
uv run src/universe.py enrich ASX etf --prune

# 2. Fetch ~1 year of end-of-day prices.
./scripts/fetch_prices.sh ASX etf 365

# 3. Screen for growth and publish the SQLite database.
./scripts/run_analysis.sh ASX etf
```

What you should see:

```
# step 1
  Pruned 75 delisted instrument(s)
  etf 402 | unit 1

# step 2
  Universe: 402 of 403 instruments match ['etf'] (1 excluded)
  FETCH SUMMARY: 402/402 tickers fetched successfully

# step 3
  Data as of 2026-08-21 (1 day(s) old)
  Loaded 35 rows -> asx_etf_growth_1_year
  DB published: .../asx.db
```

The whole ASX run takes under a minute.

### Inspecting the results

```bash
# Where everything lives
uv run src/config.py US stocks db_path        # /.../us.db
uv run src/config.py ASX etf  db_path         # /.../asx.db
uv run src/config.py US stocks eod_csv

# Top US movers over one year
sqlite3 -header /path/to/data/us.db "
  SELECT ticker, exchange, pct_change, ROUND(latest_price,2) AS price, google_finance
  FROM us_stocks_growth_1_year ORDER BY pct_change DESC LIMIT 10;"

# Tickers that grew in every window
sqlite3 -header /path/to/data/us.db "SELECT * FROM consistent_growth_stocks;"

# ASX equivalents
sqlite3 -header /path/to/data/asx.db "
  SELECT ticker, pct_change FROM asx_etf_growth_1_year ORDER BY pct_change DESC LIMIT 10;"

# What produced this data
cat /path/to/data/us/stocks/us_stocks_fetch_manifest.json
cat /path/to/data/us/stocks/us_stocks_analysis_manifest.json
```

### If a step refuses to run

Both stages fail loudly rather than publishing a doubtful result. Each message
says what to do:

| Message | Meaning | Fix |
|---|---|---|
| `Only 416/477 tickers (87.2%) returned data, below the required 95.0%` | Too much of the universe failed to fetch; the previous price file was left untouched | Usually delisted members: `universe.py enrich <EX> <TYPE> --prune`, then re-fetch. Or `--allow-partial` to publish anyway |
| `Price data is 81 days old (newest row ..., limit 5 days)` | The price file is stale, so the screen would not reflect the market | Re-run the fetch, or `run_analysis.sh ... --allow-stale` |
| `No config file for ASX/stocks` | That universe does not exist | Only `US stocks` and `ASX etf` ship; see [Supported universes](#supported-universes) |
| `Not pruning: N lookups were rate limited` | The provider throttled, so a dead listing cannot be told from a failed lookup | Wait a few minutes and re-run |
| `required tool 'sqlite-utils' not found on PATH` | Missing CLI dependency | `uv pip install sqlite-utils` |

### Scheduling

Both universes, refreshed daily, with logs kept:

```bash
# crontab -e  — runs after the US close (ASX times differ; adjust to taste)
30 22 * * 1-5  cd /path/to/stocks && ./scripts/fetch_prices.sh US stocks 365 && ./scripts/run_analysis.sh US stocks
0  9  * * 1-5  cd /path/to/stocks && ./scripts/fetch_prices.sh ASX etf 365 && ./scripts/run_analysis.sh ASX etf
```

Both scripts exit non-zero on failure, so cron will report a bad run rather
than silently publishing one. Fetch logs rotate under `logs/`.

## Docker

The image runs any pipeline stage; all state lives on a mounted volume, so the
container itself is disposable.

### Full pipeline (compose)

Compose is the supported entry point: it is the only thing that reads `.env`,
forwards AWS credentials, and pins the data volume. Use it unless you have a
reason not to.

```bash
cd /path/to/stocks

# Once per shell, and only if you want the S3 upload. Skip it and the run
# still publishes locally, logging "Skipping S3 upload (...)".
eval "$(aws configure export-credentials --format env)"

docker compose build          # only after editing src/ or config/
docker compose run --rm asx   # ASX ETFs:   sync -> fetch -> analyse -> publish -> upload
docker compose run --rm us    # US stocks:  same
```

**Pass no arguments.** Each service already carries its full command
(`all --exchange ASX --instrument-type etf --period 365`). Anything you append
*replaces* that command rather than adding to it, so `docker compose run --rm us all`
fails with a usage error — `--exchange` and `--instrument-type` go missing. To
run one stage, give the whole invocation:

```bash
docker compose run --rm asx publish --exchange ASX --instrument-type etf
docker compose run --rm us analyze --exchange US --instrument-type stocks --allow-stale
docker compose --profile tools run --rm sqlite   # shell to inspect /data
```

Compose prints `volume "stocks-data" already exists but was not created by
Docker Compose` if the volume predates the pinned name. It is a warning only;
the correct volume is still attached.

### Full pipeline (plain docker)

Equivalent, but you supply by hand everything compose would have supplied —
`.env` is not read by your shell, and the image does not contain it:

```bash
docker build -t stocks:dev .

set -a; source .env; set +a
eval "$(aws configure export-credentials --format env)"

docker run --rm -v stocks-data:/data -e TZ=UTC \
  -e S3_BUCKET -e S3_REGION -e S3_AUTO_UPLOAD \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
  stocks:dev all --exchange ASX --instrument-type etf --period 365

# Individual stages
docker run --rm -v stocks-data:/data stocks:dev fetch   --exchange US --instrument-type stocks
docker run --rm -v stocks-data:/data stocks:dev analyze --exchange US --instrument-type stocks --allow-stale
```

Omitting `-v stocks-data:/data` is the trap: docker creates a fresh anonymous
volume instead of failing, and the run silently operates on empty data.

**Rebuild after editing source.** `COPY src/ ./src/` bakes the code into the
image. A stale image gives no warning — it just runs the old code.

### Copying the databases out of the volume

Container runs publish to the `stocks-data` volume, whose host path
(`/var/lib/docker/volumes/stocks-data/_data`) needs root to read. Copy the
files out through a container instead — either form works:

```bash
# compose
docker compose --profile tools run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD":/out sqlite -c 'cp /data/*.db /out/'

# plain docker
docker run --rm --user "$(id -u):$(id -g)" \
  -v stocks-data:/data -v "$PWD":/out --entrypoint sh stocks:dev \
  -c 'cp /data/*.db /out/'
```

`--user` is not optional. The image runs as uid 10001, so without it the copy
fails with `Permission denied` on any directory you own. The volume's files are
mode 644, so reading them as your own uid is fine.

Add `-p` to `cp` to keep the original modification times, which record when the
run happened; busybox `cp` in the sqlite image then warns that it cannot
preserve ownership, which is harmless.

**Universe seeding.** The committed universes ship inside the image and are
copied to `/data/universe/` on first run. `sync` and `enrich` then rewrite the
copy on the volume, never the image layer, so membership survives across
containers and the repository stays read-only.

**Exit codes** are stable so a scheduler can branch without parsing logs:

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Error |
| 2 | Price data too stale to screen |
| 3 | Fetch too incomplete to publish |

**Image notes.** `python:3.12-slim`, multi-stage with `uv` installing from
`requirements.lock`, runs as uid 10001, ~340 MB. `tzdata` is installed
deliberately: the fetch boundary resolves the exchange's local date through
`zoneinfo`, and without it the boundary silently falls back to UTC — a full
day out for ASX. Outbound network is needed to
`query1/query2.finance.yahoo.com` and `www.nasdaqtrader.com`.

The container never invokes the shell scripts. `src/run.py` drives everything
and `src/pipeline.py` builds the database with the `sqlite3` standard library,
so the image needs no `sqlite3` CLI, no `sqlite-utils` and no Bash. The shell
scripts remain for host use and produce byte-identical output.

## Common recipes

**Screen US common stock only** — this is the shipped default; no change needed:

```bash
uv run src/universe.py sync US stocks           # refresh membership + classification
./scripts/fetch_prices.sh US stocks 365         # fetches only common stock
./scripts/run_analysis.sh US stocks
```

`config/us_stocks_config.yaml` sets `asset_types: [common_stock]`, and **both**
stages honour it, so ETFs, warrants, units, rights, preferred lines and notes
are dropped *before the fetch* rather than after. Each stage confirms it:

```
Universe: 5750 of 13135 instruments match ['common_stock'] (7385 excluded)
```

That is ~7,400 provider requests not spent on instruments you would discard.

**Screen ASX ETFs:**

```bash
./scripts/fetch_prices.sh ASX etf 365
./scripts/run_analysis.sh ASX etf
```

**Preview a universe before fetching** — it is plain CSV:

```bash
awk -F',' '$4=="common_stock"' config/us_stocks.csv | wc -l
cut -d',' -f4 config/us_stocks.csv | sort | uniq -c | sort -rn
```

**Restrict to a single venue** — copy the US config to
`config/nasdaq_stocks_config.yaml`, point `data_dir`/`db_path` somewhere new,
then sync: `universe.py sync NASDAQ stocks` restricts membership to the Nasdaq
venue rather than all US exchanges.

**Include instrument types that are normally excluded** — screening warrants or
unclassified instruments is legitimate, just explicit:

```yaml
asset_types: [common_stock, unknown]    # also accept unclassified instruments
asset_types: [common_stock, etf]        # stocks and funds together
```

**Skip the universe sync** to keep the committed snapshot exactly as-is —
`sync` replaces membership from the live directory, adding new listings and
dropping delisted ones. Fetch and analysis work fine without it.

**Screen stale data deliberately** (e.g. the fetch is failing but you want to
look anyway):

```bash
./scripts/run_analysis.sh US stocks --allow-stale
```

## Supported universes

| `--exchange` | Covers | Shipped config |
|---|---|---|
| `US` | The whole US listed universe: Nasdaq, NYSE, NYSE American, NYSE Arca, Cboe BZX, IEX | ✅ `config/us_stocks_config.yaml` |
| `ASX` | Australian Securities Exchange — **ETFs only** today | ✅ `config/asx_etf_config.yaml` (`etf`) |
| `NASDAQ` | Nasdaq-listed only | — add a config to use |
| `NYSE` | NYSE, NYSE American, NYSE Arca | — add a config to use |
| `NSE` / `BSE` | India | — add a config to use |

`US` is the shipped US universe because the exchange symbol directory covers
every US venue and links are built per ticker. `NASDAQ` and `NYSE` remain
available if you want a venue-restricted universe; create
`config/<exchange>_<type>_config.yaml` and run `universe.py sync` against it.

### ASX coverage

The ASX universe is **exchange-traded funds** (477 of them), by design — ASX
common stock is deliberately out of scope. `--exchange ASX --instrument-type
stocks` therefore fails with a clear "no config file" message rather than
screening the wrong thing.

Two differences from the US universe are worth knowing:

- **`sync` does not apply to ASX.** It reads the US exchange symbol directory,
  so it only covers `US`, `NASDAQ` and `NYSE`. For ASX, use
  `uv run src/universe.py enrich ASX etf`, which fills metadata for the tickers
  already in the file via per-ticker provider lookups. That path is slower and
  the provider rate-limits it, so the command retries with backoff and reports
  anything it could not resolve.
- **Thresholds are tuned per market.** ASX ETFs turn over roughly 8k shares a
  day against ~188k for US equities, and market-maker creation/redemption means
  screen volume understates their real liquidity. `config/asx_etf_config.yaml`
  therefore uses `min_median_volume: 1000` and `min_price: 2.0` (AUD). Applying
  the US floors here would exclude about 82% of the universe.

## Command reference

Every stage is also a Python entry point, useful when you want options the
shell wrappers do not expose:

```bash
uv run src/fetch_prices.py  --exchange US --instrument-type stocks \
    --period 365 --batch-size 100 --min-success-ratio 0.95 --log-file logs/us.log
uv run src/analysis.py      --exchange US --instrument-type stocks [--allow-stale]
uv run src/universe.py      sync US stocks          # membership + class (US only)
uv run src/universe.py      enrich ASX etf [--prune]  # metadata, any market
uv run src/config.py        US stocks db_path       # resolve any config value
```

Useful flags:

| Flag | Stage | Effect |
|---|---|---|
| `--period N` | fetch | Days of history to request (default 365) |
| `--batch-size N` | fetch | Symbols per provider request (default 100) |
| `--min-success-ratio F` | fetch | Completeness gate (default 0.95) |
| `--allow-partial` | fetch | Publish even below that ratio |
| `--allow-stale` | analysis | Screen price data older than `max_data_age_days` |
| `--prune` | universe enrich | Drop instruments the provider no longer lists |
| `--upload` | run_analysis.sh | Upload the DB to S3 (needs `S3_BUCKET`) |

### Publishing to S3

Set the bucket in `.env` and the database is uploaded after every successful
build:

```bash
S3_BUCKET=s3://your-bucket
S3_REGION=ap-southeast-2
S3_AUTO_UPLOAD=true
```

Both settings are required: `S3_BUCKET` alone does nothing, so data is never
sent off the machine by default. Without `S3_AUTO_UPLOAD`, pass `--upload` for
a one-off:

```bash
./scripts/run_analysis.sh US stocks --upload
uv run src/run.py analyze --exchange US --instrument-type stocks --upload
```

The key is the database filename (`us.db`, `asx.db`) at the bucket root. A
prefix works too — `S3_BUCKET=s3://your-bucket/daily` writes `daily/us.db`.

Uploads use `boto3`, not the `aws` CLI, so the container image stays small. In
AWS, give the task an IAM role rather than credentials in the environment.

**In containers, credentials come from the environment.** `.env` is excluded
from the image, and the container runs as uid 10001 so it cannot read a `0600`
`~/.aws/credentials` even if that directory were mounted. `docker-compose.yml`
therefore forwards `AWS_*` from the calling shell:

```bash
eval "$(aws configure export-credentials --format env)"
docker compose run --rm asx
```

The variables are listed by name only in the compose file, so compose omits
each one when it is unset on the host instead of passing an empty string —
an empty `AWS_ACCESS_KEY_ID` would short-circuit botocore's credential chain
rather than falling through to the next provider. On ECS none of them are set
and the task role supplies credentials.

Every run states which path it took, so the log is never ambiguous:

```
INFO -   Uploading 0.05 MB -> s3://your-bucket/asx.db
INFO - Uploaded to s3://your-bucket/asx.db
```

```
INFO - Skipping S3 upload (S3_AUTO_UPLOAD is not set; pass --upload to force)
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
      - {days: 7, label: 7_days, threshold: 10.0, endpoint_window: 2, min_coverage: 0.5}
```

### Windows

Each window gives its length as either `months` or `days` (exactly one), a
`label` used for its filename and SQLite table, and a `threshold` percentage.

Any eligibility setting may be overridden per window — `endpoint_window`,
`min_coverage`, `min_observation_ratio`, `min_price`, `min_median_volume` —
which short windows need. A 7-day window holds only 5–6 trading sessions, so
the default 3-day endpoint median would leave no room between the two ends and
a holiday-shortened week would exclude everything. `endpoint_window: 2` still
medians each end while tolerating a 5-session week.

Values are validated at load: ranges are checked, and window labels must be
unique and safe as filenames and SQLite identifiers.

## Outputs

| File | Contents |
|---|---|
| `<prefix>_eod.csv` | Full price history, one row per ticker per day |
| `<prefix>_eod_growth_<label>.csv` | Qualifying tickers for one window, with diagnostics |
| `<prefix>_eod_growth.csv` | Daily price history for every ticker that grew in any window, with `growth_count` and `growth_periods` |
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
that qualified in every **month-scale** window. Day-scale windows such as
`7_days` are deliberately excluded from it: that table answers "grew
consistently across timeframes", and folding a one-week window in would
silently narrow it to "and also rose this week", a far more selective and
quite different signal. The short window is published as its own table, so
both questions can be asked separately:

```sql
-- sustained growers
SELECT * FROM consistent_growth_stocks;

-- sustained growers that are also moving right now
SELECT c.* FROM consistent_growth_stocks c
JOIN us_stocks_growth_7_days w USING (ticker);
```

### Database size

A published database holds the screen results only, so it is small — under
1 MB for the whole US universe:

```
us_stocks_growth_1_year       1,171 rows
us_stocks_growth_6_months       749 rows
us_stocks_growth_3_months       521 rows
us_stocks_growth_1_month        818 rows
consistent_growth_stocks        152 rows
                              --------
us.db                          0.83 MB
```

Daily price history for matched tickers is **not published by default**. It is
useful only for charting and was ~99% of the file: 438,643 rows against ~3,000
rows of actual results. Enable it per config if you want it:

```yaml
analysis:
  include_price_history: true    # adds <prefix>_growth, ~33 MB for US stocks
```

Turning it back off removes the table and its CSV on the next run, so a stale
copy is never served alongside fresh results.

Two further size measures apply when it *is* enabled: the history table stores
only per-row facts (values constant for a run — `fetch_time`, `price_basis`,
`fetch_run_id`, `run_id` — live in the manifest, and `name` is joinable from
any per-window table, rather than being repeated on every daily row), and the
database is `VACUUM`ed before publication, since bulk inserts otherwise leave
roughly half the file as free pages.

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
