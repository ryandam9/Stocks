# Stocks Repository Review — Round 2 — 2026-08-22

**Repository:** `ryandam9/Stocks`  
**Base branch:** `master`  
**Base commit reviewed:** `196bfcc1a9d6998f60013f87c3b3796ecdeff6e1`  
**Previous review baseline:** `f4357e6b6493cd6b86272af48402a4cb86f064c4`  
**Scope:** updated Python pipeline, universe enrichment, shell orchestration, SQLite publication, provenance, configuration, tests/CI, and next product improvements.

---

## Executive summary

Round 1 resulted in a substantial improvement. The repository is no longer a simple stock-growth script with a few guardrails; it is becoming a traceable screening pipeline. The biggest Round 1 correctness problems have been addressed: zero-result runs no longer leave stale growth CSVs behind, dataset-level freshness is checked, observation density is measured, adjusted-price provenance exists, output CSVs and the SQLite DB are published atomically at the individual-file level, universe metadata is structured, error reports are rewritten every run, config validation is stronger, CI exists, dependencies are locked, and the analysis stage emits a run manifest.

The next problems are therefore different. They are mostly **second-order pipeline-integrity issues introduced or exposed by the stronger design**.

The highest-priority theme is now:

> A result should be trusted only when the complete expected universe was fetched, the universe/config used for fetch and analysis are identifiable, and every published artifact belongs to the same successful pipeline snapshot.

At the reviewed commit, those guarantees are not yet complete.

The most important new findings are:

1. A materially **partial fetch can still be published and screened as a successful dataset**.
2. The new universe enrichment can still **misclassify derivatives as common stock**; the checked-in master currently classifies `AACBR` as `common_stock`, although SEC filings identify it as a **Right**.
3. Publication is atomic per file/DB, but **not atomic across the whole run**, so consumers can still observe mixed generations after failure or concurrent execution.
4. `RECOVERABLE_ERRORS` still includes broad built-in `ValueError` and `KeyError`, so some programming/response-shape defects can still be converted into ticker failures.
5. Fetch provenance and analysis provenance are disconnected: EOD rows do not identify the fetch run, there is no fetch manifest, and the analysis manifest cannot prove which universe/config produced its price file.
6. Price-basis aggregation uses lexical `min`, which can incorrectly label a mixed `adjusted` + `raw_fallback` ticker as fully adjusted.
7. The universe `refresh` command enriches current rows but does not actually refresh membership; nevertheless it stamps every row with today's `source_date`.
8. SQLite table types change between empty and non-empty runs because header-only CSVs are recreated as all-`TEXT` tables.
9. A shell preflight path has a concrete typo: `exw do you want me to implement?it 1` instead of `exit 1`.

The project is in much better shape than in Round 1. I would **not** respond to this round by adding a large framework or broad rewrite. The remaining high-impact fixes are relatively local and should preserve the current compact design.

---

# Round 1 status

This table is intentionally conservative: **Closed** means the reviewed implementation now addresses the original defect; **Partial** means the main case is fixed but an adjacent correctness gap remains.

| Previous ID | Status | Round 2 assessment |
|---|---|---|
| STK-001 stale growth outputs | **Closed** | Every growth window and the combined output are rewritten even when empty. |
| STK-002 stale dataset treated as current | **Closed** | Dataset-level freshness guard and `--allow-stale` now exist. |
| STK-003 span mistaken for observation coverage | **Closed** | `min_observation_ratio` and non-overlapping endpoint requirements were added. |
| STK-004 adjusted-price provenance | **Partial** | Row-level `price_basis` exists, but mixed-basis aggregation is unsafe; see R2-006. |
| STK-005 mixed exchange/security universe | **Partial** | Structured metadata and exchange-aware links are major improvements, but derivative classification is still incomplete; see R2-002/R2-007. |
| STK-006 broad exception handling | **Partial** | Unexpected exception types abort, but `ValueError`/`KeyError` remain recoverable; see R2-004. |
| STK-007 atomic publication | **Partial** | Individual CSVs and final DB are atomic, but the complete snapshot is not; see R2-003. |
| STK-008 exclusive fetch end date | **Mostly closed** | Requesting tomorrow fixes the literal exclusive-end bug; exchange-session/timezone handling can still be improved; see R2-009. |
| STK-009 stale error CSV | **Closed** | Error CSV is rewritten on clean runs. |
| STK-010 config semantic validation | **Mostly closed** | Core ranges/labels are checked, but type coercion and `asset_types` validation remain; see R2-010. |
| STK-011 incomplete eligibility diagnostics | **Closed** | A clear staged funnel is now printed and recorded. |
| STK-012 missing integration coverage | **Improved / Partial** | Strong Python integration tests were added; shell/multi-artifact/concurrency failure paths remain uncovered. |
| STK-013 no CI | **Closed** | GitHub Actions now runs Ruff, formatting, pytest, and ShellCheck. |
| STK-014 dependency reproducibility | **Mostly closed** | A compiled lock file is committed and used by CI. |
| STK-015 weak provenance | **Partial** | Analysis manifest/run IDs are useful, but fetch-to-analysis lineage is missing; see R2-005. |
| STK-016 packaging | **Deferred intentionally** | Still reasonable to defer at this repository size. |
| STK-017 shell requirements undocumented | **Closed** | README now states Bash 4.4+, sqlite tools, and direct Python alternatives. |
| STK-018 public-repo housekeeping | **Optional** | No correctness impact. |

---

# High-priority findings

## R2-001 — A partial fetch can still become the official dataset

**Severity:** High  
**Files:** `src/fetch_prices.py`, `src/analysis.py`

The fetch stage now records failed tickers well, but it does not enforce a minimum success ratio.

`fetch_historical_data()` returns all successful frames after its retry pass. If at least one ticker succeeds, the CLI saves the resulting EOD CSV and exits successfully. This is true even if a large fraction of the requested universe failed.

The analysis stage subsequently screens that partial EOD file. It knows how many instruments are in the configured universe (`manifest.universe_screened`) but does not compare that number with the number of tickers actually present in the price dataset and does not require a fetch-quality threshold.

That produces a dangerous failure mode:

```text
Universe expected:             5,000
Provider succeeded:            2,800
Provider failed:               2,200
Fetch command exit status:         0
Analysis command:            success
Published screen:          incomplete
```

The screen may look perfectly plausible while silently omitting thousands of candidates.

This is more important than an individual failed ticker. Screening is a **cross-sectional** operation: completeness of the population is part of the result's correctness.

### Recommended fix

Add an explicit fetch quality gate.

A minimal implementation could add configuration such as:

```yaml
fetch:
  min_success_ratio: 0.98
```

and refuse to publish a new EOD snapshot when:

```text
successful_tickers / requested_tickers < min_success_ratio
```

For exceptional use, support a deliberate override such as `--allow-partial`, and stamp that state prominently into provenance.

Better still, add a fetch manifest containing:

```text
fetch_run_id
started_at
finished_at
requested_tickers
successful_tickers
failed_tickers
success_ratio
provider
requested_start
requested_end
data_as_of
universe_hash
config_hash
eod_sha256
status
```

Then make analysis validate the manifest before screening.

### Tests to add

- 100 symbols requested, 1 fails: behaviour according to configured tolerance.
- 100 symbols requested, 30 fail: fetch must not publish a new official snapshot by default.
- Previous known-good EOD exists, current fetch is below quality threshold: old file remains intact and the failed fetch is recorded separately.
- Analysis receives an EOD file whose manifest says `partial`: reject unless explicitly allowed.

---

## R2-002 — Universe enrichment still misclassifies real derivatives as common stock

**Severity:** High  
**Files:** `src/universe.py`, `config/nasdaq_stocks.csv`

The structured universe is a major improvement over `ticker~name`, but current classification still has a correctness hole.

At the reviewed master commit:

```csv
ticker,name,exchange,asset_type,...
AACBR,Artius II Acquisition Inc.,NASDAQ,common_stock,...
```

However, Artius II Acquisition Inc.'s SEC filing identifies:

- `AACB` — Class A ordinary shares
- `AACBR` — **Rights**, each right entitling the holder to receive one tenth of one Class A ordinary share

So `AACBR` is currently eligible for the configured `[common_stock]` NASDAQ screen even though it is a derivative security.

The reason is structural:

- name-based classification only works when the security name contains a recognisable suffix such as `Rights`, `Warrant`, `Units`, etc.;
- the current source name for `AACBR` is just `Artius II Acquisition Inc.`;
- provider metadata can report derivative lines as `EQUITY`;
- `_asset_type()` falls back to `default_asset_type`, which for the stocks universe is `common_stock`.

There is a second subtle issue in `_asset_type()`: the comment says a provider `EQUITY` result is not trustworthy enough to override the universe's declared type, but the implementation returns `default_asset_type` rather than preserving the existing `row["asset_type"]`.

### Recommended fix

Do not make per-ticker Yahoo metadata the authoritative source of US security class.

For US listings, prefer a bulk authoritative symbol directory whose security name/type is designed for listing metadata. Nasdaq Trader publishes `nasdaqlisted.txt` and `otherlisted.txt`; its symbol-directory documentation states that the files are updated periodically throughout the day, and `otherlisted.txt` security names can include security type/class information.

Use a layered approach:

1. authoritative/bulk exchange directory for symbol + venue + security description;
2. explicit security-type mapping;
3. name heuristics only as a fallback;
4. provider metadata as enrichment, not the sole classifier;
5. unknown/ambiguous instruments should default to `unknown`, not `common_stock`.

For high-integrity screening, **fail closed on unknown type** unless the user explicitly opts in.

### Verification source

SEC filing for Artius II Acquisition Inc. (2026):  
`https://www.sec.gov/Archives/edgar/data/2034334/000114036126011804/ef20069206_8k.htm`

Nasdaq Trader Symbol Directory definitions:  
`https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs`

### Tests to add

Add real regression fixtures for ambiguous names where the ticker's security class is not visible in the stored name, including `AACBR`.

Assert that a known `right`, `unit`, `warrant`, preferred line, and common stock cannot switch into the wrong class during refresh.

---

## R2-003 — Publication is atomic per artifact, not atomic per pipeline snapshot

**Severity:** High  
**Files:** `src/runmeta.py`, `src/analysis.py`, `scripts/run_analysis.sh`

Round 1's file-truncation problem is substantially improved:

- CSVs are written to temp files and `os.replace()`d;
- SQLite is built in `${DB}.building.$$` and moved into place only after completion.

However, the **set of artifacts is still published incrementally**.

The analysis loop publishes each window CSV immediately:

```text
1_year.csv     -> new run
6_months.csv   -> new run
3_months.csv   -> process crashes
1_month.csv    -> previous run
combined.csv   -> previous run
manifest.json  -> previous run
DB             -> previous run
```

At that instant, a consumer reading the data directory sees a mixed-generation snapshot.

A second mismatch can occur when analysis succeeds but DB construction/import fails: the CSVs and manifest represent the new analysis run, while the published SQLite DB remains the previous run.

Concurrency adds another problem. `atomic_write_csv()` and `atomic_write_text()` use a fixed temp name:

```text
.<filename>.tmp
```

Two concurrent executions targeting the same output can overwrite/remove each other's temporary files. The DB temp path is process-specific, but the CSV/manifest temp paths are not. Two successful runs can also finish out of order and let an older run overwrite a newer run.

### Recommended fix

Move from file-level atomicity to **run-level staging**.

For example:

```text
data/
  .runs/
    <run_id>/
      growth_1_year.csv
      growth_6_months.csv
      growth_3_months.csv
      growth_1_month.csv
      combined.csv
      stocks.db
      manifest.json
  current -> .runs/<run_id>
```

Workflow:

1. Acquire a lock for the exchange/instrument pipeline.
2. Generate all artifacts inside a run-specific directory.
3. Validate required files, schemas, run IDs and row counts.
4. Build SQLite from the staged files.
5. Write final manifest with `status=success`.
6. Atomically switch one `current` pointer (or rename one staging directory) to publish the complete snapshot.

If keeping today's flat output paths, at least:

- use unique temp names containing `run_id`/PID/UUID;
- hold an inter-process lock;
- validate every output's run ID before DB publication;
- publish manifest last and have consumers treat it as the commit marker.

### Tests to add

- Crash after the second of four window CSVs: consumers must still see the previous complete snapshot.
- Force `sqlite-utils` failure after CSV generation: previous published snapshot remains internally consistent.
- Start two runs concurrently: either one is rejected by the lock or both complete without temp-file collision, and newest/selected run wins deterministically.

---

## R2-004 — `ValueError` and `KeyError` are still treated as provider failures

**Severity:** High  
**File:** `src/fetch_prices.py`

The Round 1 broad `except Exception` was narrowed, which is good. But the current recoverable set is:

```python
RECOVERABLE_ERRORS = TRANSIENT_ERRORS + (YFException, ValueError, KeyError)
```

`ValueError` and `KeyError` are generic built-in exceptions used extensively by Python/pandas/application code. They can represent:

- a changed yfinance response shape;
- an unexpected missing column;
- a normalization bug;
- invalid data coercion;
- an internal programming mistake.

When one of these escapes `_fetch_batch`, `_fetch_all()` records the entire batch as failed tickers and continues. That recreates part of the exact failure mode STK-006 was intended to eliminate.

The current regression test proves that `AttributeError` aborts, but not that accidental `ValueError`/`KeyError` defects abort.

### Recommended fix

Remove generic `ValueError` and `KeyError` from the batch-level recoverable set.

Catch them only at a **specific narrow provider boundary** where the exact error condition is known and intentionally translated into a provider-specific exception.

Conceptually:

```python
RECOVERABLE_ERRORS = (
    YFRateLimitError,
    YFPricesMissingError,
    ConnectionError,
    TimeoutError,
    ...specific transport/provider exceptions...
)
```

Then let schema/normalization errors fail the run loudly.

### Test to add

Have `_normalise()` or `_extract()` raise a synthetic `KeyError` and `ValueError` that represents a schema defect and assert that the fetch command aborts rather than publishing a partial EOD file.

---

## R2-005 — Fetch provenance and analysis provenance are not connected

**Severity:** High  
**Files:** `src/fetch_prices.py`, `src/analysis.py`, `src/runmeta.py`, README

The analysis manifest is useful, but it identifies the **analysis invocation**, not the complete lineage of the price data.

Current state:

- the fetcher creates `self.run_id`;
- `_error.csv` rows carry that run ID;
- EOD price rows do **not** carry the fetch run ID;
- there is no fetch manifest;
- analysis generates a new unrelated run ID;
- the analysis manifest stores the universe file path, but not a content hash;
- config thresholds are recorded, but there is no fetch-time config hash;
- there is no EOD file checksum;
- the manifest is overwritten at a fixed `*_analysis_manifest.json` path rather than retained historically;
- a failed analysis may leave the previous successful manifest in place.

This matters because the universe/config can change between fetch and analysis.

Example:

1. Fetch prices using universe version A.
2. Refresh/edit universe to version B.
3. Run analysis against the old EOD file but new universe metadata/filtering.
4. The manifest records the new universe path but cannot prove which content was used by the fetch.

The README currently says each run is a self-contained, self-identifying snapshot and that every row is stamped with `run_id` and `data_as_of`. That is true for the per-window growth outputs, but not for every pipeline output (notably EOD rows and the combined history's `data_as_of`).

### Recommended fix

Introduce fetch-to-analysis lineage explicitly.

EOD rows should carry at least:

```text
fetch_run_id
```

A fetch manifest should record hashes and counts:

```text
fetch_run_id
universe_sha256
config_sha256
eod_sha256
requested_count
success_count
failure_count
data_as_of
provider/version
```

Analysis manifest should include:

```text
analysis_run_id
source_fetch_run_id
source_eod_sha256
source_universe_sha256
source_config_sha256
```

Retain manifests by run ID and optionally maintain a small `latest.json` pointer.

This also gives the future dashboard a clean `runs` history without reverse-engineering files.

---

# Medium-priority findings

## R2-006 — Mixed `price_basis` history can be incorrectly marked fully adjusted

**Severity:** Medium  
**File:** `src/analysis.py`

`_window_stats()` currently aggregates price basis using:

```python
price_basis=("price_basis", "min")
```

For strings, `min` is lexical. With the current values:

```text
adjusted
raw_fallback
unknown
```

lexical minimum of a mix containing `adjusted` and `raw_fallback` is `adjusted`.

So a ticker whose window contains both verified adjusted rows and raw-fallback rows can be classified as:

```text
price_basis = adjusted
```

and pass the `Adjusted prices` eligibility stage.

Today's fetcher normally assigns one basis to the full downloaded symbol frame, so the issue may not appear in a single clean fetch. But it becomes real as soon as data is appended incrementally, backfilled from different runs/providers, or manually combined — exactly the likely future direction of this app.

### Recommended fix

Treat price basis as a conservative quality state, not a lexical aggregate.

For example:

```python
if any(raw_fallback): raw_fallback
elif any(unknown):    unknown
else:                 adjusted
```

Or require:

```python
(stats_group["price_basis"] == "adjusted").all()
```

when strict adjusted-price screening is desired.

Also consider recording `raw_fallback_rows` and `unknown_basis_rows` in the funnel/manifest.

### Test to add

Construct one ticker with adjusted rows at the beginning/end and one `raw_fallback` row inside the window; assert that it does not pass as fully adjusted.

---

## R2-007 — `universe.py refresh` refreshes metadata, not universe membership

**Severity:** Medium  
**File:** `src/universe.py`

The command name and `source_date` imply that the universe itself is current. But `refresh_universe()` starts by loading the existing file and queries metadata only for those existing tickers.

It does not discover:

- newly listed securities;
- symbols added to the source universe;
- delisted/removed securities;
- symbol changes;
- changed listing status.

After enrichment, it sets:

```python
df["source_date"] = today
```

for every row. Therefore a universe whose membership is months old can be stamped with today's source date after metadata enrichment.

### Recommended fix

Split the concepts:

```text
stocks universe sync     # fetch authoritative membership, diff adds/removes
stocks universe enrich   # fill metadata for existing rows
```

or make `refresh` truly perform both.

Record separate fields such as:

```text
membership_source
membership_as_of
metadata_source
metadata_refreshed_at
```

Before replacing membership, print/store a diff:

```text
+ 42 additions
- 17 removals
~ 3 symbol/name changes
```

For the US universe, a bulk exchange directory is preferable to thousands of individual metadata requests.

---

## R2-008 — SQLite schema changes depending on whether a screen is empty

**Severity:** Medium  
**File:** `scripts/run_analysis.sh`

For a non-empty CSV, the script imports with:

```bash
sqlite-utils insert ... --detect-types
```

For a header-only CSV, it manually creates every field as:

```text
"column" TEXT
```

The log says `schema preserved`, but only **column names** are preserved. Column types are not.

So the same logical table can change between runs:

```text
Non-empty run:
  pct_change       FLOAT
  observations     INTEGER
  median_volume    INTEGER/FLOAT

Empty run:
  pct_change       TEXT
  observations     TEXT
  median_volume    TEXT
```

This can break dashboard queries, BI tools, type-sensitive client code, or indexes when a screen moves between empty/non-empty states.

`--detect-types` also means inferred types can vary with actual values.

### Recommended fix

Define the SQLite schema explicitly and keep it stable.

Options:

1. create tables using a canonical DDL, then import rows;
2. maintain schema metadata in Python and use it for both CSV and SQLite;
3. move the SQLite build into Python so DataFrame/output schemas have one owner.

Add a `PRAGMA table_info(...)` integration assertion comparing empty and non-empty runs.

---

## R2-009 — Fetch boundary is still based on local wall-clock date rather than a completed exchange session

**Severity:** Medium  
**File:** `src/fetch_prices.py`

Changing yfinance's exclusive end from `today` to `tomorrow` fixes the literal omitted-end-date bug.

The remaining issue is that:

```python
now = datetime.now()
end_date = now + 1 day
```

uses the machine's local timezone/date and does not establish whether the current exchange session is complete.

This can matter when:

- the job runs while the US market is still open but the host is already on the next calendar date;
- the job runs around DST transitions;
- different exchanges are screened from the same host timezone;
- the provider exposes a current/incomplete daily bar.

The analysis layer drops rows whose adjusted price is null, which provides some protection, but the fetcher's contract should still be “completed EOD sessions,” not “whatever Yahoo returned before tomorrow.”

### Recommended fix

Long term, calculate the last completed session using exchange timezone/calendar metadata and fetch through the exclusive boundary after that session.

A lighter solution is to:

- over-fetch through tomorrow;
- validate the final daily bar;
- discard a session known to be in progress;
- record `last_completed_session` in the fetch manifest.

Do not make a network market-calendar dependency mandatory if the project can keep this deterministic and simple.

---

## R2-010 — Config validation still silently coerces some invalid values

**Severity:** Medium  
**File:** `src/config.py`

The new range validation is valuable. A few semantic gaps remain.

### 1. Integer truncation

Values are validated/coerced with `int(...)`, e.g.:

```python
endpoint_window=int(...)
months=int(...)
max_data_age_days=int(...)
```

A YAML value such as `1.9` can become `1` rather than being rejected.

### 2. `asset_types` is not validated

This expression:

```python
list(analysis_raw.get("asset_types", ["common_stock", "etf"]))
```

turns a scalar string into characters:

```yaml
asset_types: common_stock
```

becomes conceptually:

```python
["c", "o", "m", "m", "o", "n", ...]
```

The result is then a confusing empty screen rather than a clear config error.

Unknown asset types are also accepted silently.

### 3. Non-finite thresholds

Consider rejecting `NaN`/infinite numeric values explicitly.

### Recommended fix

Validate exact types and allowed enums before coercion:

```text
endpoint_window: positive integer
max_data_age_days: positive integer
months: positive integer
asset_types: non-empty list of known strings
threshold: finite number
```

This does not require Pydantic; a few focused helper functions are enough.

---

# Low-priority / maintainability findings

## R2-011 — Shell dependency check contains a concrete command typo

**Severity:** Low for data correctness, Medium for operability  
**File:** `scripts/run_analysis.sh`

The missing-tool path contains:

```bash
exw do you want me to implement?it 1
```

where the intended command is clearly:

```bash
exit 1
```

Because this is still syntactically valid shell, linting can miss it as an arbitrary external command. A machine missing `sqlite3` or `sqlite-utils` gets a `command not found` failure instead of the intended clean diagnostic path.

### Fix

Replace with `exit 1` and add a small behavioral shell smoke test that executes the dependency-check branch with a controlled `PATH`.

This is also evidence that ShellCheck should complement, not replace, executable tests of the shell wrappers.

---

## R2-012 — Legacy ETF universe compatibility is misleading/broken in pipeline callers

**Severity:** Low/Medium  
**Files:** `src/fetch_prices.py`, `src/analysis.py`, README

`load_universe()` accepts a `default_asset_type`, but fetch/analysis call it without providing the instrument-specific default.

Its default is `common_stock`.

Therefore a legacy `TICKER~Name` ETF universe whose names do not contain a derivative suffix is loaded as `common_stock`, then filtered by an ETF config requesting `[etf]`, which can remove the entire intended universe.

`universe.py refresh` correctly chooses an ETF default for refresh, but the ordinary fetch/analysis callers do not.

The README says legacy `TICKER~Name` files still load. That should either be made instrument-aware everywhere or documented as a one-time upgrade path rather than general compatibility.

### Fix

Use:

```python
default_type = ETF if instrument_type == "etf" else COMMON_STOCK
load_universe(path, default_asset_type=default_type)
```

consistently in fetch and analysis, or remove legacy support once all shipped files are structured.

---

# Tests and CI — next round

The new test suite is much stronger. The highest-value additions are no longer more isolated unit tests; they are **pipeline contract tests**.

| ID | Test | Expected guarantee |
|---|---|---|
| R2-T01 | 30% of provider universe fails | No successful official publish below configured coverage. |
| R2-T02 | Schema bug raises `KeyError`/`ValueError` | Run aborts; defect is not converted to ticker failures. |
| R2-T03 | Mixed adjusted/raw price basis | Ticker is not labelled fully adjusted. |
| R2-T04 | AACBR-like ambiguous security name | Authoritative security class wins over common-stock default. |
| R2-T05 | Crash after second window output | Previous complete snapshot remains visible. |
| R2-T06 | SQLite import failure after new analysis | CSV/manifest/DB visible to consumer remain one generation. |
| R2-T07 | Two concurrent runs | Lock or deterministic publication prevents collisions/out-of-order overwrite. |
| R2-T08 | Empty then non-empty SQLite table | `PRAGMA table_info` is identical across runs. |
| R2-T09 | Scalar/unknown `asset_types` config | Config fails with actionable error. |
| R2-T10 | Legacy ETF universe | ETF entries remain screenable or compatibility is explicitly rejected. |
| R2-T11 | Missing `sqlite3`/`sqlite-utils` | Wrapper prints expected error and exits with intended status. |
| R2-T12 | Fetch with universe A, analysis after universe B | Provenance mismatch is detected. |

### CI improvement

Keep the current offline pytest job; it is exactly the right default.

Add a small shell **behavior** job in addition to ShellCheck. It can use stubs/mocked commands and should not call Yahoo.

A separate scheduled compatibility job may occasionally test the pinned yfinance contract against live provider responses, but live-provider failure should not make every normal PR flaky.

---

# Architecture recommendations after Round 2

## 1. Introduce one pipeline snapshot identity

The most useful structural change is not packaging or a web framework. It is a single identity that follows the data:

```text
fetch_run_id
      |
      v
EOD snapshot
      |
      v
analysis_run_id + source_fetch_run_id
      |
      v
CSV + SQLite + report
```

The analysis can have its own run ID, but it must point to the exact fetch snapshot it consumed.

## 2. Use a manifest as the commit contract

Treat `manifest.status == success` and all expected artifact hashes/run IDs matching the manifest as the definition of a published run.

Consumers should never infer “latest successful” from file modification time.

## 3. Prefer staged directories over many coordinated flat files

A run directory gives the project atomic snapshots, easy history, reproducibility, and simple cleanup without adding a database server.

For example:

```text
data/nasdaq/stocks/runs/<run_id>/...
data/nasdaq/stocks/current -> runs/<run_id>
```

This would solve several findings at once: R2-003, R2-005, concurrent temp collisions, historical manifests, and future dashboard history.

## 4. Make universe membership a first-class dataset

A universe should have its own version/hash and provenance, independent of enrichment fields.

That makes it possible to answer:

```text
Why was ticker X absent from yesterday's screen?
Was it filtered, failed to fetch, or not in that universe version?
```

---

# App improvement suggestions — revised priority

Round 1 proposed a dashboard, benchmark-relative performance, risk context, screen history, watchlists and alerts. Those still make sense. After reviewing the improved code, I would order them more specifically.

## Product Priority 1 — Screen history: new / retained / dropped

This is still the single highest-value user feature after pipeline integrity.

Persist one row per ticker/window/run:

```text
run_id
ticker
window
qualified
return_pct
```

Then the app can immediately answer:

- What entered the screen today?
- What dropped out?
- What has qualified for 5 consecutive runs?
- What was the return after first qualification?

This turns isolated CSV snapshots into a daily research workflow.

## Product Priority 2 — Benchmark-relative return

Absolute return alone lacks context.

Store:

```text
asset_return_pct
benchmark_return_pct
excess_return_pct
```

For example, +20% is much more interesting when the benchmark is +3% than when the benchmark is +28%.

Do not hard-code one benchmark globally. Configure it per universe/strategy.

## Product Priority 3 — Use dollar liquidity, not only share volume

Current liquidity uses median share volume. Share count is not directly comparable across securities with very different prices.

Add median **dollar turnover**:

```text
median(adj_price * volume)
```

and consider making it the primary stock-liquidity filter.

For ETFs, liquidity is more nuanced: spread, AUM and market-maker creation/redemption matter, so keep ETF thresholds separate rather than pretending the stock model transfers perfectly.

## Product Priority 4 — Clarify “price growth” vs total return

The analysis uses adjusted close, which includes the effect of splits and usually cash distributions/dividends. For income-oriented stocks and ETFs, that is closer to a **total-return** series than pure market-price appreciation.

Make the semantic choice explicit:

```yaml
return_basis: adjusted_total_return
```

or provide both:

```text
price_return_pct
total_return_pct
```

The UI/README should avoid calling adjusted-close performance simply “price growth” if distributions are part of the number.

## Product Priority 5 — Risk context

Add a small, interpretable set rather than an indicator zoo:

- maximum drawdown;
- annualised volatility;
- positive-week/month consistency;
- distance from 50/200-day moving averages;
- volume/dollar-turnover confirmation.

A candidate card might read:

```text
AAPL
1Y total return      +31.2%
6M excess return     +12.4%
Max drawdown          -9.4%
Volatility             24%
Median dollar volume  $8.1B
Qualified             3/4 windows
Consecutive runs       6
```

## Product Priority 6 — Static self-contained HTML dashboard

The pipeline is now mature enough to justify this once R2 integrity issues are closed.

Keep it lightweight: generate a static HTML report after each successful published snapshot rather than immediately introducing a server/backend.

Suggested home page:

- run/data-as-of banner;
- fetch coverage and failures;
- universe version;
- counts by 1M/3M/6M/1Y;
- new entrants/dropouts;
- consistent growers;
- sortable/filterable table;
- per-ticker detail/chart links.

Because the dashboard is generated from a specific run directory, it becomes an immutable archive as well as the current UI.

## Product Priority 7 — Watchlists and alerts

After membership history exists, alerts become straightforward:

- enters/exits a chosen screen;
- reaches 4/4 windows;
- new 52-week high;
- abnormal dollar volume;
- fetch coverage below threshold;
- stale or mixed-provenance data.

Operational/data-quality alerts are as important as investment-event alerts.

---

# Performance improvements

## Incremental price fetching

Downloading the full configured period for every ticker each run is simple and safe but increasingly expensive.

Once provenance and run staging are solid, consider incremental updates:

1. load the last stored date;
2. fetch a small overlap window, not just the missing day;
3. replace overlapping rows;
4. periodically perform a deeper/full backfill.

The overlap matters because adjusted historical values can change after corporate actions/distributions.

Do not implement append-only incremental fetching without that correction strategy.

## Bulk universe sources

The current threaded per-ticker metadata enrichment is much faster than the original sequential implementation, but bulk symbol directories are still more efficient and less rate-limit-sensitive for membership/exchange/security-class metadata.

Use yfinance per-ticker metadata only for fields that a bulk listing source does not provide.

---

# Suggested implementation order

## Phase R2-A — Trust the population

1. R2-001 fetch success/coverage gate.
2. R2-004 remove broad built-in exceptions from recoverable batch failures.
3. Add fetch manifest and fetch run ID to EOD data.
4. Add R2-T01/R2-T02.

## Phase R2-B — Trust the universe

1. R2-002 authoritative security-class source / fail closed on ambiguous type.
2. Fix the known `AACBR` classification regression.
3. R2-007 separate membership sync from metadata enrichment.
4. Version/hash universe content.
5. Add membership diff output.

## Phase R2-C — Trust the snapshot

1. R2-003 run-level staging + lock + one atomic publish marker/pointer.
2. R2-005 connect fetch and analysis lineage.
3. R2-006 conservative price-basis aggregation.
4. R2-008 stable SQLite schemas.
5. Add failure/concurrency integration tests.

## Phase R2-D — Tighten edges

1. R2-009 completed-session handling.
2. R2-010 strict config types/enums.
3. R2-011 shell typo + wrapper smoke tests.
4. R2-012 instrument-aware legacy handling or remove legacy mode.

## Phase R2-E — User value

1. Screen history/new entrants/dropouts.
2. Benchmark/excess return.
3. Dollar-volume liquidity + risk metrics.
4. Static HTML dashboard.
5. Watchlists/alerts.

---

# Final assessment

The response to Round 1 was strong. Several non-trivial issues were not merely patched superficially: the code now has a proper eligibility funnel, stale-data guard, structured universe metadata, price provenance, integration tests, CI, dependency locking, and much safer individual artifact publication.

That changes the nature of the review. I no longer see “basic stock screener correctness” as the principal risk. The remaining risk is **whether all stages describe the same complete dataset**.

The next milestone I would aim for is:

> One successful run = one immutable, complete snapshot containing a versioned universe, fetch manifest, EOD data, analysis results, SQLite DB and report, all linked by run IDs/hashes and published together only after validation.

You can reach that without turning the repository into a large application. A run directory, a small lock, explicit manifests, a fetch-quality gate, and authoritative universe membership/classification will resolve most of Round 2's high-priority findings while retaining the project's current simplicity.

Once that foundation is in place, **screen history + benchmark-relative performance + a static HTML dashboard** would provide much more user value than adding many technical indicators.

---

# External references checked for Round 2

1. SEC filing confirming Artius II Acquisition Inc. `AACBR` is a **Right** rather than common stock:  
   `https://www.sec.gov/Archives/edgar/data/2034334/000114036126011804/ef20069206_8k.htm`

2. Nasdaq Trader Symbol Directory field definitions, including frequently updated Nasdaq-listed and other-exchange-listed symbol files:  
   `https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs`

3. Nasdaq Trader Symbol Lookup / downloadable directory entry point:  
   `https://www.nasdaqtrader.com/Trader.aspx?id=symbollookup`
