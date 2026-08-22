# Stocks Repository Review — 2026-08-22

**Repository:** `ryandam9/Stocks`  
**Base branch:** `master`  
**Base commit reviewed:** `f4357e6b6493cd6b86272af48402a4cb86f064c4`  
**Review scope:** Python fetch/analysis/config code, shell orchestration, checked-in market universes/config, tests, dependency setup, data lifecycle, and product direction.

## Executive summary

This is a strong first version of a market-data screening pipeline. The code is compact, readable, and already addresses several subtle correctness problems that many stock screeners miss: recent listings being misreported as long-period winners, bad endpoint prints, illiquid securities, adjusted-vs-unadjusted prices, batched downloads, and CSV quoting problems.

The biggest remaining issue is **run-state integrity**. Outputs are not treated as one atomic snapshot. A later run that produces no result for a window can leave an older CSV/table behind, and a stale EOD file can still be analysed as though it were current. Because the application produces investment-research-style outputs, freshness and provenance should be considered correctness requirements, not operational niceties.

I would prioritise the work in this order:

1. Make every run atomic and self-identifying.
2. Reject or clearly flag stale source data.
3. Fix data-completeness checks and adjusted-price provenance.
4. Correct the instrument universe/exchange metadata.
5. Add integration tests around sequential runs and SQLite publication.
6. Then expand the app into a richer research/dashboard experience.

## What is already good

| Area | Observation |
|---|---|
| Growth methodology | Median endpoint prices are much safer than first/last single prints. |
| Listing-age protection | `min_coverage` prevents very new listings from being labelled as long-window growers. |
| Liquidity filtering | Median-volume thresholds are configurable per market rather than globally hard-coded. |
| Price handling | Analysis prefers `adj_close`, which is the right basis for historical return calculations when trustworthy adjusted data is available. |
| Fetch efficiency | Yahoo downloads are batched and failed symbols receive a smaller-batch retry pass. |
| Retry discipline | Permanent errors are not endlessly retried. |
| Configuration | `src/config.py` is a useful single source of truth for paths and thresholds. |
| Portability | `STOCKS_DATA_ROOT` cleanly separates generated data from checked-in inputs. |
| Regression testing | Tests specifically cover previous data-corruption and calculation bugs rather than only happy paths. |
| Logging | Rotating logs and explicit fetch summaries are useful for scheduled execution. |
| Simplicity | The project has few moving parts and is easy to understand. Keep that advantage as features are added. |

---

# Critical findings

## STK-001 — Old growth outputs can be silently reused by a new run

**Files:** `src/analysis.py`, `scripts/run_analysis.sh`, `tests/test_growth_export.py`

`analyze_stocks()` writes a per-window CSV only when the current result is non-empty. `build_combined_growth()` similarly returns without writing anything when no ticker qualifies. Existing files from an earlier run are not removed or replaced.

`run_analysis.sh` then loads a growth table whenever the corresponding CSV exists. Therefore this sequence is possible:

1. Monday: `1_month` has qualifying tickers and writes `..._growth_1_month.csv`.
2. Tuesday: no ticker qualifies for `1_month`; analysis prints `None found` and writes no new file.
3. Tuesday's SQLite load sees Monday's still-existing CSV and loads it as Tuesday's result.

The same problem applies to the combined growth CSV. There is also a related SQLite case: when not every window is available, `consistent_growth_stocks` is skipped rather than always being replaced/dropped, so an old table can survive.

This is the most important issue in the repository because the run can complete successfully while publishing stale results.

**Recommended fix**

Prefer a run-staging design:

- Generate every output under a temporary run directory such as `data/.runs/<run_id>/`.
- Always materialise the expected outputs for the run, including an empty CSV with the correct schema when there are zero matches.
- Build a new SQLite DB in a temporary path.
- Validate row counts/schema/freshness.
- Atomically rename/publish the completed snapshot only after everything succeeds.

A smaller immediate fix is to delete/replace all expected growth CSVs and tables before analysis, but an atomic staged snapshot is much safer.

**Test to add**

Create a two-run integration test: first run produces a result, second run produces none, then assert that no first-run ticker exists in second-run CSVs or SQLite tables.

---

## STK-002 — A stale EOD dataset is treated as current

**File:** `src/analysis.py`

Freshness is calculated relative to:

```python
latest_date = df["stock_price_date"].max()
```

and per-ticker staleness is measured relative to that same dataset date. This detects a ticker that stopped trading *within a fresh dataset*, but it does not detect that the whole dataset is old.

Example: if the fetch has not succeeded for 30 days, the newest row in the CSV is still used as `latest_date`; a ticker trading on that date gets `staleness_days == 0` and can be reported as a current growth candidate.

**Recommended fix**

Add dataset-level freshness validation before screening:

- determine the latest completed trading session for the configured market;
- compare it with `latest_date`;
- abort, or require an explicit `--allow-stale`, when it is older than an allowed tolerance;
- store `data_as_of` in outputs and the DB;
- display it prominently in every user-facing result.

At minimum, compare `latest_date` with the current date using a configurable tolerance. A proper exchange calendar is better because weekends/holidays should not be treated as failures.

---

# High-priority findings

## STK-003 — `min_coverage` measures date span, not actual observation coverage

**File:** `src/analysis.py`

Coverage is currently:

```python
(stats["last_date"] - stats["first_date"]).dt.days / window_days
```

This verifies that first and last observations span the window, but not that there are enough observations inside it. A pathological ticker with only two prints, one near each endpoint, can have approximately 100% `coverage` and pass because the only observation-count rule is `observations >= 2`.

For a daily EOD screener, data density matters. Sparse trading, provider gaps, or corrupted histories should not qualify as well-covered data.

**Recommended fix**

Track two concepts separately:

- **span coverage** — current first-to-last date calculation;
- **observation coverage** — observations divided by expected trading sessions.

Require both. If adding a market calendar is too much initially, use business-day count as an approximation and later replace it with exchange-specific calendars.

Also require at least `2 * endpoint_window` non-null observations so endpoint windows cannot overlap on tiny samples.

---

## STK-004 — Missing `Adj Close` is silently converted into apparently adjusted data

**Files:** `src/fetch_prices.py`, `src/analysis.py`

`_normalise()` does this when Yahoo does not supply `Adj Close`:

```python
frame["Adj Close"] = frame["Close"]
```

The nearby comment says this is recorded as unadjusted, but no provenance column is actually recorded. Downstream, `load_price_data()` sees an `adj_close` column and treats it as preferred adjusted history.

For a security with a split or distribution inside the analysis window, this fallback can materially distort calculated growth while looking valid.

**Recommended fix**

Choose one of these behaviours:

1. **Fail closed:** reject the ticker/window when adjusted prices are unavailable.
2. **Explicit fallback:** store fields such as `price_basis = adjusted|raw_fallback` and exclude raw-fallback rows from screens by default.
3. Fetch corporate actions and reconstruct/validate adjusted prices.

Do not make raw close indistinguishable from verified adjusted close.

---

## STK-005 — The `NASDAQ stocks` universe contains other exchanges and non-stock securities

**File:** `config/nasdaq_stocks.csv`

The checked-in file includes examples such as:

- `A` — Agilent Technologies
- `AA` — Alcoa
- `ABBV` — AbbVie

which are not NASDAQ-listed securities. It also includes SPAC units/warrants such as symbols/names ending in `Units`, `Warrant`, etc.

This creates multiple problems:

- the dataset name and DB prefix imply NASDAQ when it is really a broader US universe;
- Google Finance links are generated with the configured exchange for every ticker, so a NYSE security receives a NASDAQ exchange code;
- warrants/units can appear in a screen intended to represent ordinary stocks;
- market-specific analysis or future benchmarking becomes unreliable.

Current Google Finance guidance also expects the correct `TICKER:EXCHANGE` pair, and the current quote URL format is `/finance/quote/TICKER:EXCHANGE`, not the older `/finance/beta/quote/...` pattern used in `analysis.py`.

**Recommended fix**

Replace the `ticker~name` universe with structured instrument metadata, for example:

```text
ticker,exchange,name,asset_type,currency,active,source_date
A,NYSE,Agilent Technologies Inc.,common_stock,USD,true,2026-08-22
AAPL,NASDAQ,Apple Inc.,common_stock,USD,true,2026-08-22
```

Filter the universe to the requested asset type and use each row's true exchange for links and market-specific logic.

If the intended product is "US stocks" rather than specifically NASDAQ, rename the config/data set accordingly.

---

## STK-006 — Broad exception handling can hide programming/schema defects as ticker failures

**File:** `src/fetch_prices.py`

`_fetch_all()` catches `Exception` around an entire batch and converts every symbol in the batch into a failed ticker.

That is helpful for network resilience, but it also catches programming errors, unexpected pandas/yfinance response-shape changes, and bugs in normalisation. The process may continue and publish a partially successful data file instead of failing loudly.

**Recommended fix**

Only recover from known provider/data exceptions. Unexpected exceptions should terminate the run with a stack trace/non-zero exit.

Also record failure reason/category per ticker:

```text
ticker,name,reason,error_type,attempts,run_id
```

This makes provider failures distinguishable from bad symbols and internal defects.

---

## STK-007 — CSV and SQLite publication is not atomic

**Files:** `src/fetch_prices.py`, `src/analysis.py`, `scripts/run_analysis.sh`

CSV files are written directly to their final paths. SQLite tables are dropped before new rows are inserted. If a process is interrupted or `sqlite-utils insert` fails, consumers can observe truncated CSVs, missing tables, or a DB containing tables from different run states.

**Recommended fix**

- write CSVs to a temporary file and `os.replace()` after success;
- build a complete temporary DB, run validation, then atomically replace the published DB;
- alternatively, wrap same-DB table replacement in explicit transactions and staging tables;
- never drop the known-good published table before a replacement is ready.

A run-level `run_id` should be present in all generated records/metadata.

---

## STK-008 — Fetch end date is exclusive, so the intended latest session can be omitted

**File:** `src/fetch_prices.py`

The fetcher passes today's date as `end`. `yfinance.download()` documents `end` as **exclusive**. Therefore a completed session whose date equals `end_date` is not returned.

This is especially easy to get wrong when the machine timezone differs from the exchange timezone.

**Recommended fix**

Calculate the last completed exchange session and pass an exclusive end boundary after it. Avoid naive `datetime.now()` for exchange-sensitive boundaries; use timezone-aware market/session logic.

For a simple interim fix, fetch through `today + 1 day`, then discard any incomplete/current session according to exchange time.

---

# Medium-priority findings

## STK-009 — `_error.csv` can remain stale after a later fully successful fetch

**File:** `src/fetch_prices.py`

The error file is written when there are failures, but a later run with zero failures does not remove or replace it. Someone inspecting the output directory can therefore see an old error list and assume those failures belong to the latest run.

**Fix:** always write the current run's failure report, even when it contains zero rows, or make outputs run-scoped.

---

## STK-010 — Config values are structurally checked but not semantically validated

**File:** `src/config.py`

Useful key validation exists, but values such as these are not constrained:

- `min_coverage` should be in `(0, 1]`;
- `endpoint_window` should be `>= 1`;
- `months` should be positive;
- thresholds and liquidity/price floors should have sane ranges;
- labels should be unique and safe for filenames/table identifiers.

Bad values can produce silent empty results or awkward downstream names rather than failing at startup.

**Fix:** add dataclass/Pydantic-style validation or explicit checks in `load_config()`.

---

## STK-011 — Eligibility diagnostics do not report every rule described to the user

**File:** `src/analysis.py`

`_log_exclusions()` reports short history, inactivity, and illiquidity. It does not report exclusions caused by the price floor, insufficient observations, invalid endpoint price, or threshold itself.

The README says each rule's exclusions are printed, so diagnostics and documentation are slightly out of sync.

**Fix:** calculate a funnel with mutually understandable counts, for example:

```text
Universe                       5,124
Enough history                 4,900
Data complete                  4,720
Fresh                          4,701
Liquid                         3,860
Above price floor              3,225
Return above threshold           142
```

This would also make the application much easier to trust and tune.

---

## STK-012 — Unit tests are solid, but the risky orchestration layer lacks integration tests

**Files:** `tests/*`, `scripts/run_analysis.sh`

The Python-level tests are a good foundation. The most dangerous defects now live at the boundaries between files, sequential runs, shell orchestration, CSVs, and SQLite.

Add tests that execute the complete pipeline in a temp data root with stub price data and a temporary DB.

Priority integration scenarios:

1. results on run 1 -> no results on run 2;
2. stale EOD input;
3. SQLite import failure midway through publication;
4. empty growth window;
5. no failures after a prior failure file exists;
6. mixed exchanges/security types;
7. sparse price history;
8. missing adjusted-close data.

---

## STK-013 — No CI is present

There is no checked-in GitHub Actions workflow in the reviewed tree.

For this repository, a small CI job would provide a lot of value:

- install via `uv`;
- run `pytest`;
- run Ruff/format checks;
- run shellcheck on `scripts/*.sh`;
- optionally run a no-network integration test against synthetic CSV input.

Because `yfinance` response behaviour changes over time, a separate scheduled compatibility job can be useful, but network/provider tests should not block every normal PR.

---

## STK-014 — Dependency reproducibility can be stronger

**File:** `requirements.txt`

Pinning yfinance and core Python libraries is sensible, especially because the code depends on response shape. However transitive dependencies are not locked, while `sqlite-utils` and `pytest` use lower bounds.

**Recommended fix:** retain a human-readable dependency declaration but commit a `uv.lock` (or equivalent lock file). Use automated dependency PRs only when CI validates the fetch/normalisation contract.

Do not automatically upgrade yfinance just because a newer version exists; test the response shape first.

---

## STK-015 — Data provenance is too implicit

The outputs have a row-level `fetch_time`, but there is no clear run manifest telling a consumer:

- run ID;
- code commit;
- config hash;
- universe source/version/date;
- provider;
- data-as-of date;
- successful/failed ticker counts;
- analysis thresholds;
- start/end timestamps;
- publish status.

This information is extremely valuable when a result looks surprising weeks later.

**Fix:** create a `runs` table and a JSON run manifest.

---

# Low-priority / maintainability observations

## STK-016 — `sys.path` mutation works, but packaging the source would be cleaner

Both entry points manipulate `sys.path` to import sibling modules. This is fine for a small script repo, but once the app grows, use a package layout and console entry points, e.g. `stocks fetch`, `stocks analyse`, `stocks build-db`.

Do this only when it reduces friction; do not over-engineer the current small codebase.

## STK-017 — Shell portability should be documented

The README contains a Windows activation example, but the primary orchestration relies on Bash, `mapfile`, `sqlite3`, and `sqlite-utils`. Clarify supported operating systems/shell versions and provide direct Python equivalents where appropriate.

## STK-018 — Public-repo housekeeping

Consider adding:

- `LICENSE` if the repository is intended to be reusable;
- `CONTRIBUTING.md` only if outside contributions are expected;
- a short architecture/data-flow diagram;
- a sample output screenshot/table in the README once a dashboard exists.

---

# Recommended architecture evolution

The current table-per-window approach is easy to inspect but becomes awkward as more windows/metrics are added. A small long-form data model would make the app much more flexible.

## Suggested core tables

### `runs`

```text
run_id
started_at
completed_at
status
code_commit
exchange_or_universe
universe_version
data_provider
data_as_of
requested_period_days
fetch_success_count
fetch_failure_count
config_hash
```

### `instruments`

```text
instrument_id
ticker
exchange
name
asset_type
currency
sector
industry
active
source
source_date
```

### `prices`

```text
run_id
instrument_id
price_date
open
high
low
close
adjusted_close
volume
price_basis
```

### `growth_metrics`

```text
run_id
instrument_id
window
first_price
last_price
return_pct
benchmark_return_pct
relative_return_pct
observations
observation_coverage
median_volume
qualified
```

This removes the need to create a new database table every time a window or metric is added and makes historical comparisons straightforward.

---

# App improvement ideas

The existing project is currently more of a robust CLI/data pipeline than a user-facing app. I would evolve it into a **personal market research dashboard** rather than adding dozens of indicators to CSV output.

## 1. A useful dashboard without a heavy backend

Generate a local/static HTML dashboard from SQLite after each run. It can remain simple and portable.

Recommended home view:

- **Data as of** timestamp and freshness status at the top;
- fetch success/failure counts;
- number of instruments screened;
- number qualifying in 1M / 3M / 6M / 1Y;
- "consistent growers" count;
- top new entrants since the previous run;
- tickers that dropped out since the previous run;
- searchable/sortable results table.

The freshness banner should be impossible to miss.

## 2. Per-ticker research page

For each instrument show:

- price chart with 1M / 3M / 6M / 1Y views;
- benchmark comparison;
- return for each configured window;
- volume trend;
- drawdown from recent high;
- volatility;
- when/why it qualified;
- data completeness and last trading date;
- direct Yahoo/Google/official exchange links.

## 3. Explain *why* a ticker is ranked

Instead of only reporting a percentage, provide a compact explanation:

```text
AAPL
+31.2% 1Y | +18.4% 6M | +11.0% 3M
Qualified 3/4 windows
+8.7% vs benchmark over 6M
98% observation coverage
Median volume 52.1M
Max drawdown -9.4%
```

This is more useful than an opaque score alone.

## 4. Add benchmark-relative performance

Absolute growth is useful, but a stock rising 20% while its benchmark rises 25% is different from one rising 20% while the market is flat.

Add configurable benchmarks, for example:

- US broad market / NASDAQ-style universe: SPY, QQQ or a chosen index;
- ASX: an ASX 200 benchmark ETF/index;
- ETF screens: category-specific benchmarks when available.

Store both absolute and excess return.

## 5. Add risk context, not just more momentum indicators

Useful additions:

- annualised volatility;
- maximum drawdown;
- downside deviation;
- return/volatility ratio;
- distance from 50/200-day moving averages;
- percentage of positive weeks/months;
- volume confirmation;
- recent gap/spike detection.

Avoid turning the project into an indicator zoo. Prefer a small number of interpretable metrics.

## 6. Track screen membership over time

This can become one of the most valuable features.

Persist whether each ticker qualified on each run. Then show:

- **new entrants**;
- **still qualifying**;
- **dropped out**;
- number of consecutive runs qualified;
- first qualified date;
- performance since first qualification.

That makes the screener useful every day rather than producing isolated snapshots.

## 7. Watchlists and notes

Add a lightweight `watchlists` / `watchlist_members` model:

- starred tickers;
- user tags such as `research`, `owned`, `avoid`, `earnings-watch`;
- short notes;
- optional target/alert levels.

Keep this local in SQLite unless multi-device sync is actually needed.

## 8. Alerts

Useful event-driven alerts include:

- ticker enters the 4/4 consistent-growth set;
- ticker enters/exits a selected screen;
- new 52-week high;
- abnormal volume;
- data fetch stale/failing;
- adjusted-price/provider anomaly.

Start with console/email/file notifications only if needed; do not add infrastructure before the screen-history model exists.

## 9. Better universe management

The current checked-in symbol lists should become generated, dated inputs rather than anonymous static files.

Maintain:

- source URL/provider;
- retrieved date;
- exchange;
- security type;
- active/delisted state;
- currency;
- optional sector/industry.

A universe refresh command could generate a diff before replacing the current list:

```text
+ 42 newly listed
- 17 delisted/removed
~ 3 symbol/name changes
```

This protects against accidental universe drift.

## 10. Provider resilience

`BaseDataCollector` already hints at provider abstraction. Use it only when needed, but a second source can eventually provide:

- validation of suspicious adjusted prices;
- fallback when Yahoo fails;
- official exchange metadata.

Avoid silently mixing providers inside one time series. Record provider provenance per run/series.

## 11. Export a clean research snapshot

Besides SQLite/CSV, generate a single self-contained HTML report that can be opened on desktop/mobile and archived by date. A report could include:

- run summary;
- top screens;
- new entrants/dropouts;
- top 20 ranked candidates;
- charts;
- data-quality warnings;
- exact methodology/config used.

This gives the project a polished "app" experience without immediately building a web service.

---

# Suggested ranking model

After data-integrity fixes, consider ranking rather than only threshold filtering.

A simple interpretable score can combine:

```text
30% multi-window return consistency
25% benchmark-relative return
15% trend quality
10% liquidity
10% volatility penalty
10% drawdown penalty
```

Keep every component visible in the output so the score never becomes a black box.

For ETFs, use a separate model because turnover, liquidity, distributions and benchmark behaviour differ from individual equities.

---

# Testing roadmap

## Highest-value tests to add next

| ID | Test | Why it matters |
|---|---|---|
| T-01 | Two sequential runs: result -> no result | Prevent stale growth CSV/table reuse. |
| T-02 | Dataset latest date is stale | Prevent old data being presented as current. |
| T-03 | Two sparse observations spanning a year | Ensure span is not mistaken for completeness. |
| T-04 | Missing Adj Close with split-like raw prices | Prevent false adjusted returns. |
| T-05 | Mixed NYSE/NASDAQ universe | Verify correct instrument exchange/link metadata. |
| T-06 | Unexpected exception in batch normalisation | Ensure programming bugs fail the run. |
| T-07 | SQLite import failure | Ensure previous known-good DB remains intact. |
| T-08 | Prior error CSV followed by clean run | Ensure error report reflects latest run. |
| T-09 | Invalid config ranges/duplicate labels | Fail early with actionable messages. |
| T-10 | Fetch boundary around last completed session | Protect against yfinance's exclusive end date. |

---

# Proposed implementation order

## Phase 0 — Correctness and publication safety

1. STK-001 stale output lifecycle.
2. STK-002 dataset freshness guard.
3. STK-003 observation coverage.
4. STK-004 adjusted-price provenance.
5. STK-007 atomic publication.
6. Add T-01 through T-04 and T-07.

## Phase 1 — Universe/data quality

1. Replace ticker-only files with structured instrument metadata.
2. Separate exchange from broad universe name.
3. Filter warrants/units/other unwanted security types.
4. Correct external finance links.
5. Add universe refresh provenance/diffs.

## Phase 2 — Developer quality

1. CI with pytest + Ruff + shellcheck.
2. Lock dependencies with `uv.lock`.
3. Add run manifest / `runs` table.
4. Narrow exception handling and improve failure reasons.
5. Package CLI only if it improves usability.

## Phase 3 — Product value

1. Screen-history/new-entrant/dropout tracking.
2. Benchmark-relative returns.
3. Risk metrics.
4. Static/self-contained HTML dashboard/report.
5. Watchlists and alerts.

---

# Final assessment

The repository is better than a typical initial stock-screener implementation because the author has already thought about data correctness rather than only fetching prices and calculating percentage change. The code is small enough that the remaining reliability problems can be fixed without a large redesign.

I would **not** prioritise a large UI or more indicators yet. First make one run a trustworthy, immutable snapshot with clear `data_as_of`, run provenance, data-completeness guarantees and atomic publication. Once that foundation is in place, the best product improvement is screen-history + benchmark context + a polished HTML dashboard. Those features would make the project meaningfully more useful without making it bulky.

## External references checked during review

- yfinance `download()` documentation: `end` is exclusive — https://ranaroussi.github.io/yfinance/reference/yfinance.functions.html
- Google Finance ticker guidance: use the correct exchange+ticker pair — https://support.google.com/docs/answer/3093281
- Google Finance quote URL format documented in Google help/community examples as `/finance/quote/TICKER:EXCHANGE`.
