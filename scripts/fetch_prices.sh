#!/usr/bin/env bash
# fetch_prices.sh
# Usage: fetch_prices.sh <EXCHANGE> <INSTRUMENT_TYPE> [PERIOD_DAYS]
#
# Fetches historical EOD price data from Yahoo Finance via src/fetch_prices.py.
#   EXCHANGE        Exchange code: US, ASX, NSE, BSE, NYSE, NASDAQ
#   INSTRUMENT_TYPE Instrument type: stocks, etf
#   PERIOD_DAYS     Days of history to fetch (default: 400)
#
# Logs go to logs/fetch_prices_<exchange>_<instrument>.log (rotated) as well as
# stdout, so no shell redirect is needed.

set -euo pipefail

usage() {
    echo "Usage: $(basename "$0") <EXCHANGE> <INSTRUMENT_TYPE> [PERIOD_DAYS]" >&2
    echo "  e.g. $(basename "$0") US stocks 400" >&2
    exit 1
}

if [[ $# -lt 2 || $# -gt 3 ]]; then
    usage
fi

EXCHANGE="${1^^}"
INSTRUMENT="${2,,}"
PERIOD="${3:-400}"

if ! [[ "$PERIOD" =~ ^[1-9][0-9]*$ ]]; then
    echo "Error: PERIOD_DAYS must be a positive integer, got '$PERIOD'" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Exchange and instrument validity live in src/config.py; let it be the judge
# rather than duplicating the lists here.
if ! uv run src/config.py "$EXCHANGE" "$INSTRUMENT" ticker_file >/dev/null; then
    exit 1
fi

LOG_FILE="$PROJECT_ROOT/logs/fetch_prices_${EXCHANGE,,}_${INSTRUMENT}.log"
mkdir -p "$(dirname "$LOG_FILE")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "==> Fetching prices: exchange=$EXCHANGE instrument=$INSTRUMENT period=${PERIOD}d"
uv run src/fetch_prices.py \
    --exchange "$EXCHANGE" \
    --instrument-type "$INSTRUMENT" \
    --period "$PERIOD" \
    --log-file "$LOG_FILE"
log "==> Done (log: $LOG_FILE)"
