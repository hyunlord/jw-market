#!/bin/bash
set -euo pipefail

stage="${1:?stage is required}"
repo_root="${CRAWL_CHAIN_REPO_ROOT:-/app}"
cd "${repo_root}"

preseed_urls() {
  local raw="$1"
  local sites="${2:-}"
  RAW="${raw}" SITES="${sites}" python - <<'PY'
import os
from pathlib import Path

import pymysql

raw = Path(os.environ["RAW"])
sites = os.environ.get("SITES", "").split()
conn = pymysql.connect(
    host=os.environ["DB_HOST"],
    port=int(os.environ.get("DB_PORT", "3306")),
    user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
    database=os.environ.get("DB_NAME", "jw_mart_d2_stage_20260630_r2"),
    charset="utf8mb4",
    cursorclass=pymysql.cursors.Cursor,
)
try:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT article_url FROM news_raw "
            "WHERE article_url IS NOT NULL AND article_url <> ''"
        )
        urls = [row[0] for row in cursor.fetchall() if row and row[0]]
finally:
    conn.close()

if sites:
    for site in sites:
        history = raw / site / f"news_5years_{site}"
        history.mkdir(parents=True, exist_ok=True)
        (history / "scraped_urls.txt").write_text(
            "\n".join(urls) + ("\n" if urls else ""), encoding="utf-8"
        )
else:
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "scraped_urls.txt").write_text(
        "\n".join(urls) + ("\n" if urls else ""), encoding="utf-8"
    )
print(f"PRESEED_URL_COUNT={len(urls)}")
print(f"PRESEED_SITE_COUNT={len(sites)}")
PY
}

prepare_new_candidates() {
  local raw="$1"
  local scored="$2"
  local scored_new="$3"
  local summary_path="$4"
  RAW="${raw}" SCORED="${scored}" SCORED_NEW="${scored_new}" SUMMARY_PATH="${summary_path}" python - <<'PY'
import json
import os
import shutil
import sys
from pathlib import Path

import pymysql

sys.path[:0] = ["/app/crawl/agent1"]
from corpus_loader_v2 import generate_news_id, read_json, resolve_news_path, scored_files

raw = Path(os.environ["RAW"])
scored = Path(os.environ["SCORED"])
scored_new = Path(os.environ["SCORED_NEW"])
items = []
seen = {}
duplicates = []
for scored_path in scored_files(raw, scored):
    scored_json = read_json(scored_path)
    source_path = resolve_news_path(raw, scored_path, scored_json)
    news_id = generate_news_id(read_json(source_path), source_path, scored_json)
    relative = scored_path.relative_to(scored)
    if news_id in seen:
        duplicates.append(
            {"news_id": news_id, "first": str(seen[news_id]), "second": str(relative)}
        )
    else:
        seen[news_id] = relative
    items.append({"news_id": news_id, "relative": str(relative), "path": str(scored_path)})
if duplicates:
    raise SystemExit(
        "ABORT: duplicate candidate IDs within batch: "
        + json.dumps(duplicates[:10], ensure_ascii=False)
    )

existing = set()
conn = pymysql.connect(
    host=os.environ["DB_HOST"],
    port=int(os.environ.get("DB_PORT", "3306")),
    user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
    database=os.environ.get("DB_NAME", "jw_mart_d2_stage_20260630_r2"),
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
)
try:
    with conn.cursor() as cursor:
        for offset in range(0, len(items), 200):
            batch = [item["news_id"] for item in items[offset : offset + 200]]
            if not batch:
                continue
            placeholders = ",".join(["%s"] * len(batch))
            cursor.execute(
                f"SELECT news_id FROM news_raw WHERE news_id IN ({placeholders})", batch
            )
            existing.update(row["news_id"] for row in cursor.fetchall())
finally:
    conn.close()

new_count = 0
for item in items:
    if item["news_id"] in existing:
        continue
    destination = scored_new / item["relative"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(item["path"]), destination)
    new_count += 1
summary = {
    "candidate_count": len(items),
    "candidate_unique_count": len(seen),
    "existing_count": len(existing),
    "new_count": new_count,
    "filtered_scored_dir": str(scored_new),
}
Path(os.environ["SUMMARY_PATH"]).write_text(
    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
)
print("CANDIDATE_GATE=" + json.dumps(summary, ensure_ascii=False, sort_keys=True))
PY
}

tier1_collect() {
  local output="${CHAIN_STAGE_OUTPUT_DIR}"
  local raw="${output}/raw"
  local profiles="${output}/drug_profiles"
  local sites="바이오스펙테이터 히트뉴스 약업신문 데일리팜 메디칼타임즈 팜뉴스 의학신문 한경바이오인사이트 메디칼업저버 약사공론 의약뉴스 메디파나뉴스"
  mkdir -p "${raw}" "${profiles}"
  python -c 'import sys,zipfile; from pathlib import Path; target=Path(sys.argv[1]); target.mkdir(parents=True, exist_ok=True); zipfile.ZipFile("crawl/config/drug_profiles.zip").extractall(target)' "${profiles}"
  preseed_urls "${raw}" "${sites}"
  python crawl/crawler/crawl_2tier.py \
    --tier 1 --run-crawl --months 1 --concurrent-sites 4 --delay-sec 5 \
    --output-dir "${raw}" --drug-profile-dir "${profiles}/drug_profiles"
  RAW="${raw}" OUTPUT="${output}" python - <<'PY'
import json
import os
from pathlib import Path

raw = Path(os.environ["RAW"])
report_path = raw / "orchestrator_report.json"
total_news = 0
if report_path.exists():
    report = json.loads(report_path.read_text(encoding="utf-8"))
    total_news = int(report.get("total_news") or (report.get("summary") or {}).get("total_news") or 0)
articles = []
for path in raw.rglob("*.json"):
    if path.name in {"orchestrator_report.json", "crawl_summary.json", "score_run_log.json"}:
        continue
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        continue
    if isinstance(payload, dict) and (payload.get("url") or payload.get("article_url")) and (
        payload.get("title") or payload.get("body") or payload.get("content")
    ):
        articles.append(str(path.relative_to(raw)))
summary = {"article_file_count": len(articles), "report_total_news": total_news}
Path(os.environ["OUTPUT"], "collect_summary.json").write_text(
    json.dumps(summary, sort_keys=True, indent=2), encoding="utf-8"
)
if not articles and total_news <= 0:
    Path(os.environ["OUTPUT"], "NO_NEW_RAW").write_text("1\n", encoding="utf-8")
print(f"CRAWL_ARTICLE_FILE_COUNT={len(articles)}")
print(f"CRAWL_REPORT_TOTAL_NEWS={total_news}")
PY
}

tier1_classify() {
  local collect="${CHAIN_RUN_ROOT}/outputs/tier1_collect"
  local output="${CHAIN_STAGE_OUTPUT_DIR}"
  local raw="${collect}/raw"
  local scored="${output}/scored"
  local scored_new="${output}/scored_new"
  mkdir -p "${scored}" "${scored_new}"
  if [[ -f "${collect}/NO_NEW_RAW" ]]; then
    printf '{"status":"noop","reason":"no_new_raw"}\n' > "${output}/load_summary.json"
    return
  fi
  python crawl/agent1/score_v2.py \
    "${raw}" --catalog crawl/config/_catalog.json --output-root "${scored}" \
    --limit 2000 --run-log "${output}/score_run_log.json"
  prepare_new_candidates "${raw}" "${scored}" "${scored_new}" "${output}/candidate_gate.json"
  local new_count
  new_count="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["new_count"])' "${output}/candidate_gate.json")"
  if [[ "${new_count}" -eq 0 ]]; then
    printf '{"status":"noop","reason":"no_new_candidates"}\n' > "${output}/load_summary.json"
    return
  fi
  python crawl/agent1/corpus_loader_v2.py \
    --batch-dir "${raw}" --scored-dir "${scored_new}" \
    --catalog crawl/config/_catalog.json --output "${output}/load_summary.json" \
    --db-name "${DB_NAME}" --tier 1 --processed-by workflow_196_rev5674
}

tier2_collect() {
  local output="${CHAIN_STAGE_OUTPUT_DIR}"
  local raw="${output}/raw"
  local processed="${output}/processed"
  local weekday
  weekday="$(python -c 'from datetime import datetime; print(datetime.now().weekday())')"
  mkdir -p "${raw}" "${processed}"
  preseed_urls "${raw}"
  python crawl/crawler/crawl_2tier.py \
    --tier 2 --run-crawl --score --weekday-slice "${weekday}" --days 7 \
    --tier2-concurrent-sites 4 --max-pages-per-site 3 --max-links-per-page 80 \
    --delay-sec 5 --output-dir "${raw}" --processed-dir "${processed}" \
    --brand-plan-output "${output}/tier2_brand_plan.json"
  RAW="${raw}" OUTPUT="${output}" python - <<'PY'
import json
import os
from pathlib import Path

raw = Path(os.environ["RAW"])
articles = [
    path
    for path in raw.rglob("*.json")
    if path.name not in {"crawl_report.json", "tier2_brand_plan.json", "tier2_site_report.json"}
    and not path.name.endswith("_report.json")
]
summary = {"article_file_count": len(articles)}
Path(os.environ["OUTPUT"], "collect_summary.json").write_text(
    json.dumps(summary, sort_keys=True, indent=2), encoding="utf-8"
)
if not articles:
    Path(os.environ["OUTPUT"], "NO_NEW_RAW").write_text("1\n", encoding="utf-8")
print(f"CRAWL_ARTICLE_FILE_COUNT={len(articles)}")
PY
}

tier2_classify() {
  local collect="${CHAIN_RUN_ROOT}/outputs/tier2_collect_exact"
  local output="${CHAIN_STAGE_OUTPUT_DIR}"
  local raw="${collect}/raw"
  local processed="${collect}/processed"
  local processed_new="${output}/processed_new"
  mkdir -p "${processed_new}"
  if [[ -f "${collect}/NO_NEW_RAW" ]]; then
    printf '{"status":"noop","reason":"no_new_raw"}\n' > "${output}/load_summary.json"
    return
  fi
  prepare_new_candidates "${raw}" "${processed}" "${processed_new}" "${output}/candidate_gate.json"
  local new_count
  new_count="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["new_count"])' "${output}/candidate_gate.json")"
  if [[ "${new_count}" -gt 0 ]]; then
    python crawl/agent1/corpus_loader_v2.py \
      --batch-dir "${raw}" --scored-dir "${processed_new}" \
      --catalog crawl/config/_catalog.json --output "${output}/load_summary.json" \
      --db-name "${DB_NAME}" --tier 2 --processed-by tier2_exact_rule_v1
  else
    printf '{"status":"noop","reason":"no_new_candidates"}\n' > "${output}/load_summary.json"
  fi
  python /opt/tier2/tier2_full_scoring_runner.py sync-events-raw --retries 1
  python /opt/tier2/tier2_full_scoring_runner.py append-live \
    --source-processor tier2_exact_rule_v1 \
    --target-processor tier2_llm_v2_rev5671 \
    --workflow-url "${WF337_URL}" --daily-call-limit 60 --max-cost-krw 203.40
  # This required final step closes the historical category omission (audit dd41f8aa).
  python /opt/tier2/tier2_full_scoring_runner.py refresh-live-categories
  printf '{"status":"complete","category_refresh":true}\n' > "${output}/classification_summary.json"
}

case "${stage}" in
  tier1_collect) tier1_collect ;;
  tier1_classify_incremental) tier1_classify ;;
  tier2_collect_exact) tier2_collect ;;
  tier2_classify_v2_and_refresh) tier2_classify ;;
  *) echo "unsupported stage: ${stage}" >&2; exit 64 ;;
esac
