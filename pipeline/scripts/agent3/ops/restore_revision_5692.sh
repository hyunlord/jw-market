#!/usr/bin/env bash
set -euo pipefail

cd /app
cp /opt/restore/run_source.py pipeline/scripts/agent3/run_source.py
ACTUAL_CODE_SHA=$(sha256sum pipeline/scripts/agent3/run_source.py | awk '{print $1}')
if [[ "$ACTUAL_CODE_SHA" != "$EXPECTED_CODE_SHA" ]]; then
  echo "code SHA mismatch: actual=$ACTUAL_CODE_SHA expected=$EXPECTED_CODE_SHA" >&2
  exit 20
fi

TARGET=/opt/restore/target_units.tsv
ACTUAL_TARGET_SHA=$(sha256sum "$TARGET" | awk '{print $1}')
TARGET_COUNT=$(wc -l < "$TARGET" | tr -d ' ')
if [[ "$ACTUAL_TARGET_SHA" != "$EXPECTED_TARGET_SHA" || "$TARGET_COUNT" != "1593" ]]; then
  echo "target gate failed: count=$TARGET_COUNT sha=$ACTUAL_TARGET_SHA" >&2
  exit 21
fi

mkdir -p /tmp/agent3-restore/results /tmp/agent3-restore/chunks
echo "[restore-preflight] commit=$CODE_COMMIT code_sha=$ACTUAL_CODE_SHA target_count=$TARGET_COUNT target_sha=$ACTUAL_TARGET_SHA workflow_rev=$AGENT3_WORKFLOW_REV expected_rev=5692"

for source in iqvia ubist; do
  awk -F '\t' -v wanted="$source" '$2 == wanted {print $1}' "$TARGET" \
    | split -l 100 - "/tmp/agent3-restore/chunks/${source}_"
  for chunk in /tmp/agent3-restore/chunks/${source}_*; do
    [[ -s "$chunk" ]] || continue
    chunk_name=$(basename "$chunk")
    brands=$(paste -sd, "$chunk")
    count=$(wc -l < "$chunk" | tr -d ' ')
    output="/tmp/agent3-restore/results/${chunk_name}.json"
    echo "[restore-batch-start] chunk=$chunk_name source=$source units=$count"
    python -m pipeline.scripts.agent3.run_source \
      --brand-source general_all \
      --mode full \
      --source "$source" \
      --brands "$brands" \
      --workflow-rev 5692 \
      --expected-workflow-rev 5692 \
      --reestablish-revision \
      --output "$output"
    python - "$output" <<'PY'
import json
import sys

path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
keys = (
    "workflow_rev",
    "source_units",
    "workflow_calls",
    "workflow_errors",
    "skipped_same_hash",
    "skipped_same_content",
    "canonical_mismatch",
    "revision_reestablished",
    "affected",
    "estimated_cost_krw",
)
print("[restore-batch-result] " + " ".join(f"{key}={data[key]}" for key in keys), flush=True)
PY
  done
done

python - /tmp/agent3-restore/results/*.json <<'PY'
import json
import sys

keys = (
    "source_units",
    "workflow_calls",
    "workflow_errors",
    "skipped_same_hash",
    "skipped_same_content",
    "canonical_mismatch",
    "revision_reestablished",
    "affected",
)
totals = {key: 0 for key in keys}
cost = 0.0
for path in sys.argv[1:]:
    data = json.load(open(path, encoding="utf-8"))
    for key in keys:
        totals[key] += int(data[key])
    cost += float(data["estimated_cost_krw"])
print("[restore-final] workflow_rev=5692 " + " ".join(f"{key}={value}" for key, value in totals.items()) + f" estimated_cost_krw={cost:.2f}", flush=True)
PY
