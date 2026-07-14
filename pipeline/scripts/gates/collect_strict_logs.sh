#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 5 ]]; then
  echo "usage: $0 NAMESPACE DEPLOYMENT SINCE OUTPUT_DIR ENVIRONMENT" >&2
  exit 64
fi

namespace=$1
deployment=$2
since=$3
output_dir=$4
environment=$5
root=$(cd "$(dirname "$0")/../../.." && pwd)
runner="$root/pipeline/scripts/gates/release_acceptance.py"

mkdir -p "$output_dir"
selector=$(kubectl -n "$namespace" get deployment "$deployment" -o json | python3 -c '
import json
import sys

document = json.load(sys.stdin)
labels = document["spec"]["selector"]["matchLabels"]
print(",".join(f"{key}={value}" for key, value in sorted(labels.items())))
')
pods=()
while IFS= read -r pod; do
  pods+=("$pod")
done < <(kubectl -n "$namespace" get pods -l "$selector" -o json | python3 -c '
import json
import sys

document = json.load(sys.stdin)
for item in document["items"]:
    if not item["metadata"].get("deletionTimestamp"):
        print(item["metadata"]["name"])
')

args=(strict-logs --environment "$environment")
for pod in "${pods[@]}"; do
  log_path="$output_dir/$pod.log"
  kubectl -n "$namespace" logs "$pod" --all-containers=true --since="$since" >"$log_path"
  args+=(--expected-pod "$pod" --pod-log "$pod=$log_path")
done

python3 "$runner" "${args[@]}"
