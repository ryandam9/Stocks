#!/usr/bin/env bash
# run_analysis.sh
# Usage: run_analysis.sh <EXCHANGE> <INSTRUMENT_TYPE> [--upload]
#
#   1. Run analysis.py, which writes one growth CSV per window plus a combined
#      price-history CSV for every ticker that grew in any window.
#   2. Reload those CSVs into SQLite (drop + re-insert per table).
#   3. Build the consistent_growth_stocks table.
#   4. Optionally upload the DB to S3.
#
# Set S3_BUCKET (and optionally S3_REGION) to enable the upload; --upload then
# performs it. Without both, the upload step is skipped.

set -euo pipefail

usage() {
    echo "Usage: $(basename "$0") <EXCHANGE> <INSTRUMENT_TYPE> [--upload]" >&2
    echo "  e.g. $(basename "$0") NASDAQ stocks" >&2
    echo "       S3_BUCKET=s3://my-bucket $(basename "$0") NASDAQ stocks --upload" >&2
    exit 1
}

if [[ $# -lt 2 || $# -gt 3 ]]; then
    usage
fi

EXCHANGE="$1"
INSTRUMENT="$2"
UPLOAD=false
if [[ $# -eq 3 ]]; then
    if [[ "$3" == "--upload" ]]; then
        UPLOAD=true
    else
        echo "Error: unknown option '$3'" >&2
        usage
    fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# Read resolved config from the Python module rather than parsing YAML here,
# so there is exactly one implementation of the path rules.
cfg() { uv run src/config.py "$EXCHANGE" "$INSTRUMENT" "$1"; }

if ! DB=$(cfg db_path); then
    echo "Error: could not load config for $EXCHANGE/$INSTRUMENT" >&2
    exit 1
fi
PREFIX=$(cfg prefix)
EOD_CSV=$(cfg eod_csv)
COMBINED_CSV=$(cfg combined_growth_csv)
mapfile -t LABELS < <(cfg growth_labels)

if [[ ! -f "$EOD_CSV" ]]; then
    echo "Error: price data not found: $EOD_CSV" >&2
    echo "Run scripts/fetch_prices.sh $EXCHANGE $INSTRUMENT first." >&2
    exit 1
fi

for tool in sqlite3 sqlite-utils; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Error: required tool '$tool' not found on PATH" >&2
        exit 1
    fi
done

# ── 1. Run analysis ───────────────────────────────────────────────────────────
log "==> [1] Running analysis: $EXCHANGE $INSTRUMENT"
uv run src/analysis.py --exchange "$EXCHANGE" --instrument-type "$INSTRUMENT"

# ── 2. Reload CSV files into DB ───────────────────────────────────────────────
log "==> [2] Reloading CSVs into DB: $DB"
mkdir -p "$(dirname "$DB")"

load_table() {
    local csv="$1" table="$2"
    if [[ ! -f "$csv" ]]; then
        log "  Skipping (no results): $(basename "$csv")"
        return 1
    fi
    log "  Loading $(basename "$csv") -> $table"
    sqlite3 "$DB" "DROP TABLE IF EXISTS \"$table\";"
    sqlite-utils insert "$DB" "$table" "$csv" --csv --detect-types
    return 0
}

LOADED_LABELS=()
for label in "${LABELS[@]}"; do
    if load_table "${EOD_CSV%.csv}_growth_${label}.csv" "${PREFIX}_growth_${label}"; then
        LOADED_LABELS+=("$label")
    fi
done
load_table "$COMBINED_CSV" "${PREFIX}_growth" || true

log "  DB ready: $DB"

# ── 3. Build consistent growth table ──────────────────────────────────────────
# Tickers that qualified in *every* configured window. Built from the tables
# that actually loaded, so a window with no results does not break the query.
log "==> [3] Building consistent_growth_stocks"
if [[ ${#LOADED_LABELS[@]} -eq ${#LABELS[@]} && ${#LABELS[@]} -gt 0 ]]; then
    BASE_TABLE="${PREFIX}_growth_${LABELS[0]}"
    INTERSECT_SQL="SELECT ticker FROM \"$BASE_TABLE\""
    for label in "${LABELS[@]:1}"; do
        INTERSECT_SQL+=" INTERSECT SELECT ticker FROM \"${PREFIX}_growth_${label}\""
    done

    sqlite3 "$DB" <<SQL
DROP TABLE IF EXISTS consistent_growth_stocks;
CREATE TABLE consistent_growth_stocks AS
  SELECT ticker, name, pct_change AS pct_change_shortest_window
  FROM "${PREFIX}_growth_${LABELS[-1]}"
  WHERE ticker IN ($INTERSECT_SQL)
  ORDER BY ticker;
SQL
    ROW_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM consistent_growth_stocks;")
    log "  consistent_growth_stocks: $ROW_COUNT rows"
else
    log "  Skipping: not every window produced results"
fi

# ── 4. Upload DB to S3 (opt-in) ───────────────────────────────────────────────
if [[ "$UPLOAD" == true ]]; then
    if [[ -z "${S3_BUCKET:-}" ]]; then
        echo "Error: --upload given but S3_BUCKET is not set" >&2
        exit 1
    fi
    if ! command -v aws >/dev/null 2>&1; then
        echo "Error: --upload given but 'aws' is not on PATH" >&2
        exit 1
    fi
    S3_KEY="$(basename "$DB")"
    log "==> [4] Uploading DB -> ${S3_BUCKET}/${S3_KEY}"
    aws s3 cp "$DB" "${S3_BUCKET}/${S3_KEY}" ${S3_REGION:+--region "$S3_REGION"}
else
    log "==> [4] Skipping S3 upload (pass --upload with S3_BUCKET set to enable)"
fi

log "==> Done: $EXCHANGE $INSTRUMENT"
