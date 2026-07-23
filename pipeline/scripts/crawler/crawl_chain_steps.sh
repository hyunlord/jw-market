#!/bin/bash
set -euo pipefail

stage="${1:?stage is required}"
repo_root="${CRAWL_CHAIN_REPO_ROOT:-/app}"
cd "${repo_root}"

summary_failure_count() {
  local summary_path="$1"
  python - "${summary_path}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit(f"missing required summary: {path}")
payload = json.loads(path.read_text(encoding="utf-8"))
values = []
for field in ("failures", "error_count"):
    value = payload.get(field, 0)
    if isinstance(value, list):
        value = len(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SystemExit(f"invalid {field} value in {path}: {value!r}")
    values.append(value)
errors = payload.get("errors") or []
if not isinstance(errors, list):
    raise SystemExit(f"invalid errors value in {path}: {errors!r}")
values.append(len(errors))
if payload.get("status") == "partial" and not any(values):
    values.append(1)
print(max(values))
PY
}

write_stage_gate() {
  local failures="$1"
  local events_raw_gap="$2"
  local pending_gap="$3"
  STAGE="${stage}" OUTPUT="${CHAIN_STAGE_OUTPUT_DIR}" \
    FAILURES="${failures}" EVENTS_RAW_GAP="${events_raw_gap}" \
    PENDING_GAP="${pending_gap}" python - <<'PY'
import json
import os
from pathlib import Path

payload = {
    "schema": "crawl-stage-gate/v1",
    "stage": os.environ["STAGE"],
    "exit_code": 0,
    "failures": int(os.environ["FAILURES"]),
    "events_raw_gap": int(os.environ["EVENTS_RAW_GAP"]),
    "pending_gap": int(os.environ["PENDING_GAP"]),
}
Path(os.environ["OUTPUT"], "stage_gate.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print("STAGE_GATE=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
PY
}

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
  RAW="${raw}" SCORED="${scored}" SCORED_NEW="${scored_new}" SUMMARY_PATH="${summary_path}" \
    REPO_ROOT="${repo_root}" python - <<'PY'
import json
import os
import shutil
import sys
from pathlib import Path

import pymysql

sys.path[:0] = [str(Path(os.environ["REPO_ROOT"]) / "crawl" / "agent1")]
from corpus_loader_v2 import news_id

raw = Path(os.environ["RAW"])
scored = Path(os.environ["SCORED"])
scored_new = Path(os.environ["SCORED_NEW"])
items = []
seen = {}
duplicates = []
for scored_path in sorted(scored.rglob("*.json")):
    if "report" in scored_path.name.lower() or scored_path.name == "tier2_brand_plan.json":
        continue
    source_relative = scored_path.relative_to(scored)
    if len(source_relative.parts) > 1 and source_relative.parent.name.startswith(
        "news_5years_"
    ):
        relative = Path(source_relative.parent.name + "_processed", source_relative.name)
    else:
        relative = Path(source_relative.name)
    candidate_path = scored_new / relative
    candidate_news_id = news_id(candidate_path)
    if candidate_news_id in seen:
        duplicates.append(
            {
                "news_id": candidate_news_id,
                "first": str(seen[candidate_news_id]),
                "second": str(relative),
            }
        )
    else:
        seen[candidate_news_id] = relative
    items.append(
        {
            "news_id": candidate_news_id,
            "relative": str(relative),
            "path": str(scored_path),
        }
    )
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

prepare_shadow_tier1_profile() {
  local profile_dir="$1"
  local keyword="$2"
  PROFILE_DIR="${profile_dir}" KEYWORD="${keyword}" python - <<'PY'
import json
import os
from pathlib import Path

profile_dir = Path(os.environ["PROFILE_DIR"])
keyword = os.environ["KEYWORD"].strip()
if not keyword:
    raise SystemExit("CRAWL_CHAIN_SHADOW_KEYWORD must not be blank")
profile_dir.mkdir(parents=True, exist_ok=True)
for path in profile_dir.glob("drug_profile_*.json"):
    path.unlink()
payload = {
    "약 한글명": keyword,
    "약 영문명": "",
    "질환명": [],
    "경쟁사 약 한글명": [],
    "경쟁사 약 영문명": [],
    "성분명 한글": "",
    "성분명 영문": "",
}
(profile_dir / "drug_profile_shadow.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(f"SHADOW_TIER1_KEYWORD_COUNT=1")
PY
}

prepare_shadow_tier2_brand_file() {
  local brand_file="$1"
  local keyword="$2"
  BRAND_FILE="${brand_file}" KEYWORD="${keyword}" python - <<'PY'
import hashlib
import json
import os
from pathlib import Path

keyword = os.environ["KEYWORD"].strip()
if not keyword:
    raise SystemExit("CRAWL_CHAIN_SHADOW_KEYWORD must not be blank")
brand_key = "temporal-shadow-" + hashlib.sha256(keyword.encode("utf-8")).hexdigest()[:16]
payload = [{"brand_name": keyword, "brand_key": brand_key, "source": "temporal_shadow"}]
path = Path(os.environ["BRAND_FILE"])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
weekday = int(hashlib.sha256(brand_key.encode("utf-8")).hexdigest()[:8], 16) % 7
print(weekday)
PY
}

tier1_collect() {
  local output="${CHAIN_STAGE_OUTPUT_DIR}"
  local raw="${output}/raw"
  local profiles="${output}/drug_profiles"
  local all_sites="바이오스펙테이터 히트뉴스 약업신문 데일리팜 메디칼타임즈 팜뉴스 의학신문 한경바이오인사이트 메디칼업저버 약사공론 의약뉴스 메디파나뉴스"
  local selected_sites="${CRAWL_CHAIN_TIER1_SITES:-${all_sites// /,}}"
  local preseed_sites="${selected_sites//,/ }"
  local months="${CRAWL_CHAIN_TIER1_MONTHS:-1}"
  local max_articles="${CRAWL_CHAIN_TIER1_MAX_ARTICLES:-0}"
  local delay_seconds="${CRAWL_CHAIN_DELAY_SECONDS:-5}"
  local shadow_keyword="${CRAWL_CHAIN_SHADOW_KEYWORD:-}"
  local -a crawl_command=(
    python crawl/crawler/crawl_2tier.py
    --tier 1 --run-crawl --months "${months}" --concurrent-sites 4
    --delay-sec "${delay_seconds}" --output-dir "${raw}"
    --drug-profile-dir "${profiles}/drug_profiles"
  )
  crawl_command+=(--sites "${selected_sites}")
  if [[ "${max_articles}" != "0" ]]; then
    crawl_command+=(--max-articles "${max_articles}")
  fi
  mkdir -p "${raw}" "${profiles}"
  python -c 'import sys,zipfile; from pathlib import Path; target=Path(sys.argv[1]); target.mkdir(parents=True, exist_ok=True); zipfile.ZipFile("crawl/config/drug_profiles.zip").extractall(target)' "${profiles}"
  if [[ -n "${shadow_keyword}" ]]; then
    prepare_shadow_tier1_profile "${profiles}/drug_profiles" "${shadow_keyword}"
  fi
  preseed_urls "${raw}" "${preseed_sites}"
  "${crawl_command[@]}"
  RAW="${raw}" OUTPUT="${output}" python - <<'PY'
import json
import os
from pathlib import Path

from pipeline.scripts.crawler.crawl_temporal_contract import orchestrator_failure_count

raw = Path(os.environ["RAW"])
report_path = raw / "orchestrator_report.json"
if not report_path.is_file():
    raise SystemExit(f"missing required orchestrator report: {report_path}")
report = json.loads(report_path.read_text(encoding="utf-8"))
total_news = int(report.get("total_news") or (report.get("summary") or {}).get("total_news") or 0)
failures = orchestrator_failure_count(report)
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
summary = {
    "article_file_count": len(articles),
    "report_total_news": total_news,
    "failures": failures,
}
Path(os.environ["OUTPUT"], "collect_summary.json").write_text(
    json.dumps(summary, sort_keys=True, indent=2), encoding="utf-8"
)
if not articles and total_news <= 0:
    Path(os.environ["OUTPUT"], "NO_NEW_RAW").write_text("1\n", encoding="utf-8")
print(f"CRAWL_ARTICLE_FILE_COUNT={len(articles)}")
print(f"CRAWL_REPORT_TOTAL_NEWS={total_news}")
print(f"CRAWL_REPORTED_FAILURES={failures}")
PY
  local failures
  failures="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["failures"])' "${output}/collect_summary.json")"
  write_stage_gate "${failures}" 0 0
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
    write_stage_gate 0 0 0
    return
  fi
  python crawl/agent1/score_v2.py \
    "${raw}" --catalog crawl/config/_catalog.json --output-root "${scored}" \
    --limit 2000 --run-log "${output}/score_run_log.json" \
    --direct-run-url "${WF196_DIRECT_RUN_URL}"
  prepare_new_candidates "${raw}" "${scored}" "${scored_new}" "${output}/candidate_gate.json"
  local new_count
  new_count="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["new_count"])' "${output}/candidate_gate.json")"
  if [[ "${new_count}" -eq 0 ]]; then
    printf '{"status":"noop","reason":"no_new_candidates"}\n' > "${output}/load_summary.json"
    write_stage_gate 0 0 0
    return
  fi
      python crawl/agent1/corpus_loader_v2.py \
        --corpus "${scored_new}" --catalog crawl/config/_catalog.json \
        --output "${output}/load_summary.json" \
        --db-name "${DB_NAME}" --tier 1 --processed-by workflow_196_rev5674
  write_stage_gate "$(summary_failure_count "${output}/load_summary.json")" 0 0
}

tier2_collect() {
  local output="${CHAIN_STAGE_OUTPUT_DIR}"
  local raw="${output}/raw"
  local processed="${output}/processed"
  local weekday
  local selected_sites="${CRAWL_CHAIN_TIER2_SITES:-}"
  local days="${CRAWL_CHAIN_TIER2_DAYS:-7}"
  local max_pages="${CRAWL_CHAIN_TIER2_MAX_PAGES_PER_SITE:-3}"
  local max_links="${CRAWL_CHAIN_TIER2_MAX_LINKS_PER_PAGE:-80}"
  local max_articles="${CRAWL_CHAIN_TIER2_MAX_ARTICLES:-0}"
  local limit_brands="${CRAWL_CHAIN_TIER2_LIMIT_BRANDS:-0}"
  local delay_seconds="${CRAWL_CHAIN_DELAY_SECONDS:-5}"
  local shadow_keyword="${CRAWL_CHAIN_SHADOW_KEYWORD:-}"
  local shadow_brand_file="${output}/shadow_brand.json"
  local -a crawl_command=(
    python crawl/crawler/crawl_2tier.py
    --tier 2 --run-crawl --score --days "${days}"
    --max-pages-per-site "${max_pages}" --max-links-per-page "${max_links}"
    --max-articles "${max_articles}" --limit-brands "${limit_brands}"
    --delay-sec "${delay_seconds}" --output-dir "${raw}"
    --processed-dir "${processed}" --brand-plan-output "${output}/tier2_brand_plan.json"
  )
  if [[ -n "${shadow_keyword}" ]]; then
    weekday="$(prepare_shadow_tier2_brand_file "${shadow_brand_file}" "${shadow_keyword}")"
    crawl_command+=(--brand-file "${shadow_brand_file}" --weekday-slice "${weekday}")
  else
    weekday="$(python -c 'from datetime import datetime; print(datetime.now().weekday())')"
    crawl_command+=(--weekday-slice "${weekday}")
  fi
  if [[ -n "${selected_sites}" ]]; then
    crawl_command+=(--sites "${selected_sites}")
  fi
  mkdir -p "${raw}" "${processed}"
  preseed_urls "${raw}"
  "${crawl_command[@]}"
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
  write_stage_gate 0 0 0
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
  else
    prepare_new_candidates "${raw}" "${processed}" "${processed_new}" "${output}/candidate_gate.json"
    local new_count
    new_count="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["new_count"])' "${output}/candidate_gate.json")"
    if [[ "${new_count}" -gt 0 ]]; then
      python crawl/agent1/corpus_loader_v2.py \
        --corpus "${processed_new}" --catalog crawl/config/_catalog.json \
        --output "${output}/load_summary.json" \
        --db-name "${DB_NAME}" --tier 2 --processed-by tier2_exact_rule_v1
    else
      printf '{"status":"noop","reason":"no_new_candidates"}\n' > "${output}/load_summary.json"
    fi
  fi
  python /opt/tier2/tier2_full_scoring_runner.py sync-events-raw --retries 1 \
    > "${output}/sync_summary.json"
  local llm_call_limit
  llm_call_limit="${CRAWL_CHAIN_LLM_CALL_LIMIT:-60}"
  if [[ ! "${llm_call_limit}" =~ ^[0-9]+$ ]]; then
    echo "invalid CRAWL_CHAIN_LLM_CALL_LIMIT=${llm_call_limit}" >&2
    return 64
  fi
  python /opt/tier2/tier2_full_scoring_runner.py append-live \
    --source-processor tier2_exact_rule_v1 \
    --target-processor tier2_llm_v2_rev5671 \
    --workflow-url "${WF337_URL}" --daily-call-limit "${llm_call_limit}" --max-cost-krw 203.40 \
    > "${output}/append_summary.json"
  # This required final step closes the historical category omission (audit dd41f8aa).
  python /opt/tier2/tier2_full_scoring_runner.py refresh-live-categories \
    > "${output}/refresh_summary.json"
  python /opt/tier2/tier2_full_scoring_runner.py gate-status \
    --source-processor tier2_exact_rule_v1 \
    --target-processor tier2_llm_v2_rev5671 \
    > "${output}/gate_status.json"
  local load_failures append_failures failures events_gap pending_gap
  local selected_pending_gap global_pending_gap pending_scope workflow_calls
  load_failures="$(summary_failure_count "${output}/load_summary.json")"
  append_failures="$(summary_failure_count "${output}/append_summary.json")"
  failures="$((load_failures + append_failures))"
  events_gap="$(python -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["events_raw_gap"]))' "${output}/gate_status.json")"
  selected_pending_gap="$(python -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["pending_gap"]))' "${output}/append_summary.json")"
  global_pending_gap="$(python -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["pending_gap"]))' "${output}/gate_status.json")"
  workflow_calls="$(python -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["workflow_calls"]))' "${output}/append_summary.json")"
  pending_scope="global"
  pending_gap="${global_pending_gap}"
  if [[ -n "${CRAWL_CHAIN_SHADOW_KEYWORD:-}" && "${llm_call_limit}" -eq 0 ]]; then
    pending_scope="selected_no_llm_shadow"
    pending_gap="${selected_pending_gap}"
  fi
  python - "${output}/classification_summary.json" "${pending_scope}" \
    "${selected_pending_gap}" "${global_pending_gap}" "${workflow_calls}" <<'PY'
import json
import sys
from pathlib import Path

output, scope, selected, global_gap, workflow_calls = sys.argv[1:]
payload = {
    "status": "complete",
    "category_refresh": True,
    "pending_scope": scope,
    "pending_selected_gap": int(selected),
    "pending_global_gap": int(global_gap),
    "workflow_calls": int(workflow_calls),
}
Path(output).write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
PY
  write_stage_gate "${failures}" "${events_gap}" "${pending_gap}"
}

case "${stage}" in
  tier1_collect) tier1_collect ;;
  tier1_classify_incremental) tier1_classify ;;
  tier2_collect_exact) tier2_collect ;;
  tier2_classify_v2_and_refresh) tier2_classify ;;
  *) echo "unsupported stage: ${stage}" >&2; exit 64 ;;
esac
