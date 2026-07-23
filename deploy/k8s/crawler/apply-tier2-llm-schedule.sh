#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/../../.." && pwd)
namespace=${NAMESPACE:-llmops}
configmap=tier2-llm-runner-rev5671
runner="$repo_root/pipeline/scripts/crawler/tier2_full_scoring_runner.py"
backlog_policy="$repo_root/pipeline/scripts/crawler/crawl_backlog_policy.py"
manifest="$repo_root/deploy/k8s/crawler/crawl-tier2-cronjob.yaml"

kubectl -n "$namespace" create configmap "$configmap" \
  --from-file=tier2_full_scoring_runner.py="$runner" \
  --from-file=crawl_backlog_policy.py="$backlog_policy" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$namespace" apply -f "$manifest"
