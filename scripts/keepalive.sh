#!/usr/bin/env bash
set -euo pipefail

INTERVAL_SECONDS="${1:-60}"
LOG_FILE="${2:-/tmp/codespace_keepalive.log}"

mkdir -p "$(dirname "$LOG_FILE")"

echo "[keepalive] started interval=${INTERVAL_SECONDS}s log=${LOG_FILE}"

while true; do
  ts="$(date '+%Y-%m-%d %H:%M:%S %Z')"
  line="[keepalive] ${ts} heartbeat"
  echo "${line}" | tee -a "${LOG_FILE}"
  sleep "${INTERVAL_SECONDS}"
done
