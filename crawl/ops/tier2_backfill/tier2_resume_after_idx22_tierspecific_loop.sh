#!/usr/bin/env bash
set -euo pipefail
NS=llmops
CHUNKS=(23 24 25 26 27)
RUNNER=/tmp/run_tier2_mod28_chunk_retuned.sh
POSTER=/tmp/post_tier2_chunk.sh
STAMP="$(date +%Y%m%d%H%M%S)"
ROOT="/tmp/tier2_resume_after_idx22_tierspecific_loop_${STAMP}"
LOG="$ROOT/loop.log"
STATE="$ROOT/state.tsv"
ABORT="$ROOT/ABORT"
mkdir -p "$ROOT"
echo "$ROOT" > /tmp/tier2_resume_after_idx22_tierspecific_loop.latest
exec > >(tee -a "$LOG") 2>&1
printf 'START\t%s\n' "$(date -Iseconds)"
printf 'ROOT\t%s\n' "$ROOT"
printf 'CHUNKS\t%s\n' "${CHUNKS[*]}"
ZERO_SAVED_STREAK=0
for IDX in "${CHUNKS[@]}"; do
  printf '\n===== CHUNK %s START %s =====\n' "$IDX" "$(date -Iseconds)"
  before_list="$(mktemp)"
  ls -d /tmp/tier2_mod28_${IDX}_* 2>/dev/null | sort > "$before_list" || true
  set +e
  "$RUNNER" "$IDX" > "$ROOT/chunk_${IDX}_runner.log" 2>&1
  rc=$?
  set -e
  after_list="$(mktemp)"
  ls -d /tmp/tier2_mod28_${IDX}_* 2>/dev/null | sort > "$after_list" || true
  EVID="$(comm -13 "$before_list" "$after_list" | tail -n 1 || true)"
  rm -f "$before_list" "$after_list"
  if [ -z "$EVID" ]; then
    EVID="$(ls -td /tmp/tier2_mod28_${IDX}_* 2>/dev/null | head -n 1 || true)"
  fi
  printf 'CHUNK\t%s\tRUN_RC\t%s\tEVID\t%s\n' "$IDX" "$rc" "$EVID" | tee -a "$STATE"
  if [ "$rc" -ne 0 ]; then
    printf 'ABORT\tchunk=%s\treason=runner_rc_%s\n' "$IDX" "$rc" | tee "$ABORT"
    exit "$rc"
  fi
  if [ -z "$EVID" ] || [ ! -d "$EVID" ]; then
    printf 'ABORT\tchunk=%s\treason=no_evidence_dir\n' "$IDX" | tee "$ABORT"
    exit 11
  fi
  JOB="$(cat "$EVID/job_name.txt" 2>/dev/null || true)"
  if [ ! -f "$EVID/post.tsv" ]; then
    if [ -n "$JOB" ] && kubectl -n "$NS" get job "$JOB" >/dev/null 2>&1; then
      "$POSTER" "$IDX" "$JOB" "$EVID" > "$ROOT/chunk_${IDX}_post.log" 2>&1 || {
        printf 'ABORT\tchunk=%s\treason=post_script_failed\n' "$IDX" | tee "$ABORT"
        exit 12
      }
    else
      printf 'ABORT\tchunk=%s\treason=missing_post_and_job\n' "$IDX" | tee "$ABORT"
      exit 13
    fi
  elif [ -n "$JOB" ] && kubectl -n "$NS" get job "$JOB" >/dev/null 2>&1; then
    kubectl -n "$NS" delete job "$JOB" --ignore-not-found=true > "$EVID/delete.txt" || true
  fi
  python3 - "$IDX" "$EVID" "$STATE" <<'PY'
import json, re, sys
from pathlib import Path
idx, evid, state_path = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
post = (evid / 'post.tsv').read_text(errors='replace')
log = (evid / 'pod_final.log').read_text(errors='replace') if (evid / 'pod_final.log').exists() else (evid / 'pod.log').read_text(errors='replace')
base = (evid / 'baseline.tsv').read_text(errors='replace') if (evid / 'baseline.tsv').exists() else ''
job_yaml = (evid / 'job_final.yaml').read_text(errors='replace') if (evid / 'job_final.yaml').exists() else ''

def counts(text):
    out = {}
    for line in text.splitlines():
        parts = line.split('\t')
        if len(parts) >= 3 and parts[0] == 'count':
            try: out[parts[1]] = int(parts[2])
            except ValueError: pass
    return out
bc, pc = counts(base), counts(post)
run_match = re.search(r'\{"tier": 2.*?\}\nCRAWL_ARTICLE_FILE_COUNT', log, re.S)
if not run_match:
    raise SystemExit(f'ABORT: chunk {idx} missing run summary')
run = json.loads(run_match.group(0).split('\nCRAWL')[0])
gate_match = re.search(r'CANDIDATE_GATE=(\{.*?\})', log)
if not gate_match:
    raise SystemExit(f'ABORT: chunk {idx} missing candidate gate')
gate = json.loads(gate_match.group(1))
load_matches = re.findall(r'\{\n  "started_at".*?"verdict": "passed"\n\}', log, re.S)
if gate.get('new_count', 0) > 0 and not load_matches:
    raise SystemExit(f'ABORT: chunk {idx} missing loader summary')
load = json.loads(load_matches[-1]) if load_matches else {}
errors = []
if run.get('llm_calls') != 0: errors.append(f'llm_calls={run.get("llm_calls")}')
for key in ('bad_expire_news','bad_expire_events','bad_expire_scores','tier2_proc_bad'):
    if pc.get(key, 0) != 0: errors.append(f'{key}={pc.get(key)}')
if 'DeadlineExceeded' in job_yaml: errors.append('DeadlineExceeded')
if gate.get('new_count', 0) > 0:
    expected_news = bc.get('tier2_news', 0) + int(load.get('news_raw_inserted', -999999))
    expected_events = bc.get('tier2_events', 0) + int(load.get('events_inserted', -999999))
    expected_scores = bc.get('tier2_scores', 0) + int(load.get('event_brand_scores_llm_direct', -999999))
    if pc.get('tier2_news') != expected_news: errors.append(f'tier2_news_delta={pc.get("tier2_news")}-{bc.get("tier2_news")} expected {load.get("news_raw_inserted")}')
    if pc.get('tier2_events') != expected_events: errors.append(f'tier2_events_delta={pc.get("tier2_events")}-{bc.get("tier2_events")} expected {load.get("events_inserted")}')
    if pc.get('tier2_scores') != expected_scores: errors.append(f'tier2_scores_delta={pc.get("tier2_scores")}-{bc.get("tier2_scores")} expected {load.get("event_brand_scores_llm_direct")}')
if errors:
    raise SystemExit('ABORT: chunk %s gates failed: %s' % (idx, ', '.join(errors)))
mem_peak = None
mem_path = evid / 'memory.log'
if mem_path.exists():
    vals=[]
    for line in mem_path.read_text(errors='replace').splitlines():
        for part in line.split():
            if part.endswith('Mi'):
                try: vals.append(float(part[:-2])); break
                except Exception: pass
    mem_peak = max(vals) if vals else None
wait = (evid / 'wait_seconds.txt').read_text().strip() if (evid / 'wait_seconds.txt').exists() else ''
line = '\t'.join(map(str, [
    'PASS', idx, 'brands', run.get('brand_count'), 'raw', run.get('saved_articles'), 'matched', (run.get('score_summary') or {}).get('matched_json'),
    'candidate', gate.get('candidate_count'), 'existing', gate.get('existing_count'), 'new', gate.get('new_count'),
    'news_inserted', load.get('news_raw_inserted', 0), 'scores_inserted', load.get('event_brand_scores_llm_direct', 0),
    'wait_seconds', wait, 'mem_peak_mi', mem_peak, 'evid', str(evid)
]))
print(line)
with state_path.open('a', encoding='utf-8') as f:
    f.write(line + '\n')
PY
  saved=$(python3 - <<PY
import json,re
from pathlib import Path
log=(Path('$EVID')/'pod_final.log').read_text(errors='replace') if (Path('$EVID')/'pod_final.log').exists() else (Path('$EVID')/'pod.log').read_text(errors='replace')
m=re.search(r'\{"tier": 2.*?\}\nCRAWL_ARTICLE_FILE_COUNT', log, re.S)
print(json.loads(m.group(0).split('\nCRAWL')[0]).get('saved_articles',0) if m else 0)
PY
)
  if [ "${saved:-0}" -le 0 ]; then
    ZERO_SAVED_STREAK=$((ZERO_SAVED_STREAK + 1))
  else
    ZERO_SAVED_STREAK=0
  fi
  if [ "$ZERO_SAVED_STREAK" -ge 2 ]; then
    printf 'ABORT\tchunk=%s\treason=two_consecutive_zero_saved\n' "$IDX" | tee "$ABORT"
    exit 14
  fi
  printf '===== CHUNK %s PASS %s =====\n' "$IDX" "$(date -Iseconds)"
done
printf 'DONE\t%s\n' "$(date -Iseconds)" | tee -a "$STATE"
