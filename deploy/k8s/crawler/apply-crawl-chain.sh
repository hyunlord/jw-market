#!/bin/bash
set -euo pipefail

namespace="${NAMESPACE:-llmops}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
crawler_dir="${root}/deploy/k8s/crawler"
runner="${root}/pipeline/scripts/crawler/crawl_chain.py"
steps="${root}/pipeline/scripts/crawler/crawl_chain_steps.sh"
mode="${1:---dry-run}"

render_runner_configmap() {
  kubectl -n "${namespace}" create configmap crawl-chain-runner \
    --from-file=crawl_chain.py="${runner}" \
    --from-file=crawl_chain_steps.sh="${steps}" \
    --dry-run=client -o yaml
}

active_legacy_jobs() {
  kubectl -n "${namespace}" get jobs -o json | python -c '
import json, sys
legacy = {"jw-news-crawl-tier1-daily", "jw-news-crawl-tier2-daily-slice"}
payload = json.load(sys.stdin)
for item in payload.get("items", []):
    owners = {owner.get("name") for owner in item.get("metadata", {}).get("ownerReferences", [])}
    if owners & legacy and int(item.get("status", {}).get("active") or 0) > 0:
        print(item["metadata"]["name"])
'
}

case "${mode}" in
  --dry-run)
    render_runner_configmap >/dev/null
    kubectl -n "${namespace}" apply --dry-run=server -f "${crawler_dir}/crawl-chain-cronjob.yaml"
    kubectl -n "${namespace}" apply --dry-run=server -f "${crawler_dir}/crawl-tier1-cronjob.yaml"
    kubectl -n "${namespace}" apply --dry-run=server -f "${crawler_dir}/crawl-tier2-cronjob.yaml"
    echo "CRAWL_CHAIN_CUTOVER_DRY_RUN=PASS"
    ;;
  --execute-cutover)
    active="$(active_legacy_jobs)"
    if [[ -n "${active}" ]]; then
      echo "ABORT: active legacy crawl Jobs must finish before cutover: ${active}" >&2
      exit 20
    fi
    render_runner_configmap | kubectl -n "${namespace}" apply -f -
    # Apply the new object suspended first. Only activate it after both old
    # objects are durably suspended, preventing a double-run window.
    kubectl -n "${namespace}" apply -f "${crawler_dir}/crawl-chain-cronjob.yaml"
    kubectl -n "${namespace}" apply -f "${crawler_dir}/crawl-tier1-cronjob.yaml"
    kubectl -n "${namespace}" apply -f "${crawler_dir}/crawl-tier2-cronjob.yaml"
    kubectl -n "${namespace}" patch cronjob jw-crawl-chain-daily -p '{"spec":{"suspend":false}}'
    kubectl -n "${namespace}" get cronjob \
      jw-crawl-chain-daily jw-news-crawl-tier1-daily jw-news-crawl-tier2-daily-slice \
      -o custom-columns=NAME:.metadata.name,SCHEDULE:.spec.schedule,SUSPEND:.spec.suspend
    ;;
  --rollback)
    kubectl -n "${namespace}" patch cronjob jw-crawl-chain-daily -p '{"spec":{"suspend":true}}'
    kubectl -n "${namespace}" patch cronjob jw-news-crawl-tier1-daily -p '{"spec":{"suspend":false}}'
    kubectl -n "${namespace}" patch cronjob jw-news-crawl-tier2-daily-slice -p '{"spec":{"suspend":false}}'
    ;;
  *)
    echo "usage: $0 [--dry-run|--execute-cutover|--rollback]" >&2
    exit 64
    ;;
esac
