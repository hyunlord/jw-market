#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/../.." && pwd)
cd "${REPO_ROOT}"

OUTPUT_BASE="output/news_v2_full_mac"
mkdir -p "${OUTPUT_BASE}"

MAC_SITES="바이오스펙테이터,메디칼업저버,약사공론,의약뉴스"

python3 -u crawl/crawler/crawl_news_full_orchestrator.py \
  --crawler crawl/crawler/crawl_news_v2.py \
  --drug-profile-dir crawl/config/drug_profiles \
  --sites "${MAC_SITES}" \
  --output-base "${OUTPUT_BASE}" \
  --concurrent-sites 4 \
  --months 60 \
  --max-pages 10 \
  --delay 5.0 \
  --batch-by-month \
  2>&1 | tee "${OUTPUT_BASE}/orchestrator.log"
