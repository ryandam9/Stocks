#!/usr/bin/env bash
# run_analysis.sh
# Usage: run_analysis.sh <EXCHANGE> <INSTRUMENT_TYPE> [--upload] [--allow-stale]
#
#   1. Run analysis.py, which publishes one growth CSV per window (empty but
#      headed when nothing qualifies) plus a combined price-history CSV.
#   2. Build a fresh SQLite DB in a temporary file and publish it atomically.
#   3. Build the consistent_growth_stocks table.
#   4. Optionally upload the DB to S3.
#
# Every expected table is recreated on every run, so a window that matches
# nothing cannot leave the previous run's rows in place.
#
# Set S3_BUCKET (and optionally S3_REGION) to enable the upload.

set -euo pipefail

usage() {
    echo "Usage: $(basename "$0") <EXCHANGE> <INSTRUMENT_TYPE> [--upload] [--allow-stale]" >&2
    echo "  e.g. $(basename "$0") US stocks" >&2
    echo "       S3_BUCKET=s3://my-bucket $(basename "$0") US stocks --upload" >&2
    exit 1
}

[[ $# -lt 2 ]] && usage

EXCHANGE="$1"
INSTRUMENT="$2"
shift 2

UPLOAD=false
ALLOW_STALE=()
while [[ $# -gt 0 ]]; do
    case "$1" in
    --upload) UPLOAD=true ;;
    --allow-stale) ALLOW_STALE=(--allow-stale) ;;
    *)
        echo "Error: unknown option '$1'" >&2
        usage
        ;;
    esac
    shift
done

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
# consistent_growth_stocks spans month-scale windows only; day-scale windows
# are published on their own rather than narrowing that table to "and also
# rose this week".
mapfile -t CONSISTENT_LABELS < <(cfg consistent_growth_labels)
GROWTH_SCHEMA=$(cfg growth_schema_sql)
INCLUDE_HISTORY=$(cfg include_price_history)

if [[ ! -f "$EOD_CSV" ]]; then
    echo "Error: price data not found: $EOD_CSV" >&2
    echo "Run scripts/fetch_prices.sh $EXCHANGE $INSTRUMENT first." >&2
    exit 1
fi

for tool in sqlite3 sqlite-utils; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "Error: required tool '$tool' not found on PATH" >&2
        exit 1
    }
done

# ── 1. Run analysis ───────────────────────────────────────────────────────────
log "==> [1] Running analysis: $EXCHANGE $INSTRUMENT"
uv run src/analysis.py --exchange "$EXCHANGE" --instrument-type "$INSTRUMENT" \
    ${ALLOW_STALE[0]+"${ALLOW_STALE[@]}"}

# ── 2. Build the DB in a temporary file ───────────────────────────────────────
# Publishing into the live DB would leave consumers reading a half-populated
# database if a load failed partway.
log "==> [2] Building DB"
mkdir -p "$(dirname "$DB")"
TMP_DB="${DB}.building.$$"
rm -f "$TMP_DB"
cleanup() { rm -f "$TMP_DB"; }
trap cleanup EXIT

# Loads a CSV into a table, creating an empty typed-as-TEXT table when the CSV
# has only a header. sqlite-utils cannot import a header-only file.
load_table() {
    local csv="$1" table="$2" schema="${3:-}"
    if [[ ! -f "$csv" ]]; then
        log "  MISSING (expected): $(basename "$csv")"
        return 1
    fi

    local rows
    rows=$(($(wc -l <"$csv") - 1))

    # Create the table with its declared schema first, then insert. Letting
    # sqlite-utils infer types from the CSV gave the same logical table
    # REAL/INTEGER columns on a normal run and all-TEXT columns on an empty
    # one, which breaks type-sensitive consumers when a screen empties out.
    if [[ -n "$schema" ]]; then
        sqlite3 "$TMP_DB" \
            "DROP TABLE IF EXISTS \"$table\"; CREATE TABLE \"$table\" ($schema);"
    else
        sqlite3 "$TMP_DB" "DROP TABLE IF EXISTS \"$table\";"
    fi

    if [[ $rows -le 0 ]]; then
        if [[ -z "$schema" ]]; then
            local header cols
            IFS= read -r header <"$csv"
            # shellcheck disable=SC2001  # per-field substitution needs sed
            cols=$(printf '%s' "$header" | sed 's/[^,]*/"&" TEXT/g')
            sqlite3 "$TMP_DB" "CREATE TABLE \"$table\" ($cols);"
        fi
        log "  Loaded 0 rows -> $table (empty, declared schema)"
        return 0
    fi

    sqlite-utils insert "$TMP_DB" "$table" "$csv" --csv --detect-types
    log "  Loaded $rows rows -> $table"
    return 0
}

ALL_LOADED=true
for label in "${LABELS[@]}"; do
    load_table "${EOD_CSV%.csv}_growth_${label}.csv" "${PREFIX}_growth_${label}" \
        "$GROWTH_SCHEMA" || ALL_LOADED=false
done
if [[ "$INCLUDE_HISTORY" == true ]]; then
    load_table "$COMBINED_CSV" "${PREFIX}_growth" || ALL_LOADED=false
else
    # Drop any history table left by an earlier run that had it enabled, so a
    # stale copy is never served alongside fresh screen results.
    sqlite3 "$TMP_DB" "DROP TABLE IF EXISTS \"${PREFIX}_growth\";"
    log "  Skipping price history (include_price_history: false)"
fi

if [[ "$ALL_LOADED" != true ]]; then
    echo "Error: expected outputs were missing; refusing to publish a partial DB" >&2
    exit 1
fi

# ── 3. Build consistent growth table ──────────────────────────────────────────
# Tickers that qualified in *every* configured window.
log "==> [3] Building consistent_growth_stocks"
if [[ ${#CONSISTENT_LABELS[@]} -eq 0 ]]; then
    sqlite3 "$TMP_DB" "DROP TABLE IF EXISTS consistent_growth_stocks;"
    log "  Skipping: no month-scale windows are configured"
else
    BASE_TABLE="${PREFIX}_growth_${CONSISTENT_LABELS[0]}"
    INTERSECT_SQL="SELECT ticker FROM \"$BASE_TABLE\""
    for label in "${CONSISTENT_LABELS[@]:1}"; do
        INTERSECT_SQL+=" INTERSECT SELECT ticker FROM \"${PREFIX}_growth_${label}\""
    done

    sqlite3 "$TMP_DB" <<SQL
DROP TABLE IF EXISTS consistent_growth_stocks;
CREATE TABLE consistent_growth_stocks AS
  SELECT ticker, name, exchange, pct_change AS pct_change_shortest_window,
         data_as_of, run_id
  FROM "${PREFIX}_growth_${CONSISTENT_LABELS[-1]}"
  WHERE ticker IN ($INTERSECT_SQL)
  ORDER BY ticker;
SQL
    log "  consistent_growth_stocks: $(sqlite3 "$TMP_DB" 'SELECT COUNT(*) FROM consistent_growth_stocks;') rows (windows: ${CONSISTENT_LABELS[*]})"
fi

# sqlite-utils leaves a large freelist behind after bulk inserts -- about half
# the file. Reclaim it before publishing; this takes well under a second.
log "  Compacting"
sqlite3 "$TMP_DB" "VACUUM;"

# Publish atomically: readers see either the previous DB or the complete new one.
mv -f "$TMP_DB" "$DB"
trap - EXIT
log "  DB published: $DB"

# ── 4. Upload DB to S3 (opt-in) ───────────────────────────────────────────────
# S3_AUTO_UPLOAD makes publication routine without passing --upload each time.
case "${S3_AUTO_UPLOAD:-}" in
    1 | true | yes | on) [[ -n "${S3_BUCKET:-}" ]] && UPLOAD=true ;;
esac

if [[ "$UPLOAD" == true ]]; then
    [[ -n "${S3_BUCKET:-}" ]] || {
        echo "Error: --upload given but S3_BUCKET is not set" >&2
        exit 1
    }
    command -v aws >/dev/null 2>&1 || {
        echo "Error: 'aws' is not on PATH" >&2
        exit 1
    }
    S3_KEY="$(basename "$DB")"
    log "==> [4] Uploading DB -> ${S3_BUCKET}/${S3_KEY}"
    aws s3 cp "$DB" "${S3_BUCKET}/${S3_KEY}" ${S3_REGION:+--region "$S3_REGION"}
else
    log "==> [4] Skipping S3 upload (pass --upload with S3_BUCKET set to enable)"
fi

log "==> Done: $EXCHANGE $INSTRUMENT"
