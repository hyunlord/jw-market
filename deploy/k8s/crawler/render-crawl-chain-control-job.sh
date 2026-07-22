#!/bin/bash
set -euo pipefail

action="${1:?action must be resume or status}"
run_id="${2:?run id is required}"
from_stage="${3:-}"
namespace="${NAMESPACE:-llmops}"
job_name="jw-crawl-chain-${action}-$(date -u +%Y%m%d%H%M%S)"

if [[ ! "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}$ ]]; then
  echo "invalid run id" >&2
  exit 64
fi

case "${action}" in
  resume)
    test -n "${from_stage}"
    case "${from_stage}" in
      tier1_collect|tier1_classify_incremental|tier2_collect_exact|tier2_classify_v2_and_refresh) ;;
      *) echo "invalid from-stage" >&2; exit 64 ;;
    esac
    command="exec python /opt/crawl-chain/crawl_chain.py run --run-id '${run_id}' --state-root /var/lib/jw-crawl-chain --stage-script /opt/crawl-chain/crawl_chain_steps.sh --resume --from-stage '${from_stage}'"
    ;;
  status)
    command="exec python /opt/crawl-chain/crawl_chain.py status --run-id '${run_id}' --state-root /var/lib/jw-crawl-chain"
    ;;
  *)
    echo "usage: $0 <resume|status> <run-id> [from-stage]" >&2
    exit 64
    ;;
esac

kubectl -n "${namespace}" create job "${job_name}" \
  --from=cronjob/jw-crawl-chain-daily --dry-run=client -o json | \
  CONTROL_COMMAND="${command}" python -c '
import json, os, sys
payload = json.load(sys.stdin)
payload["spec"]["template"]["spec"]["containers"][0]["args"] = [os.environ["CONTROL_COMMAND"]]
print(json.dumps(payload, sort_keys=True))
'
