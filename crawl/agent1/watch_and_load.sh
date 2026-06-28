#!/usr/bin/env bash
set -u

GCP_TARGET="${GCP_TARGET:-GCP@34.47.113.232}"
REMOTE_ROOT="${REMOTE_ROOT:-~/phase_beta_delta}"
LOCAL_TMP_BASE="${LOCAL_TMP_BASE:-/tmp}"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-3308}"
DB_USER="${DB_USER:-root}"
DB_NAME="${DB_NAME:-jw_mart}"
SLEEP_SECONDS="${SLEEP_SECONDS:-300}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
OUTPUT_DIR="${REPO_ROOT}/outputs"

mkdir -p "${OUTPUT_DIR}"

while true; do
  ssh "${GCP_TARGET}" "ls ${REMOTE_ROOT}/outputs/stats_*.json 2>/dev/null" \
    | sed 's|.*/stats_||;s|.json||' > "${LOCAL_TMP_BASE}/completed_batches.txt"

  while IFS= read -r ym; do
    [ -n "${ym}" ] || continue
    flag="${OUTPUT_DIR}/loaded_${ym}.flag"
    [ ! -f "${flag}" ] || continue

    local_batch="${LOCAL_TMP_BASE}/news_${ym}"
    rm -rf "${local_batch}"
    scp -r "${GCP_TARGET}:${REMOTE_ROOT}/news_${ym}" "${LOCAL_TMP_BASE}/"

    python3 "${SCRIPT_DIR}/corpus_loader_v2.py" \
      --batch-dir "${local_batch}" \
      --catalog "${local_batch}/_catalog.json" \
      --db-host "${DB_HOST}" \
      --db-port "${DB_PORT}" \
      --db-user "${DB_USER}" \
      --db-name "${DB_NAME}" \
      --workflow-id 196 \
      --output "${OUTPUT_DIR}/load_${ym}.json" \
      >> "${OUTPUT_DIR}/load_all.log" 2>&1

    python3 "${SCRIPT_DIR}/cross_match_adapter.py" \
      --batch "${ym}" \
      --db-host "${DB_HOST}" \
      --db-port "${DB_PORT}" \
      --db-user "${DB_USER}" \
      --db-name "${DB_NAME}" \
      --catalog "${local_batch}/_catalog.json" \
      --output "${OUTPUT_DIR}/cross_match_${ym}.json" \
      >> "${OUTPUT_DIR}/cross_match_all.log" 2>&1

    touch "${flag}"
    rm -rf "${local_batch}"
  done < "${LOCAL_TMP_BASE}/completed_batches.txt"

  sleep "${SLEEP_SECONDS}"
done
