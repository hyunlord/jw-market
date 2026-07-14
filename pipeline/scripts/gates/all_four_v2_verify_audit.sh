#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 BASE_URL [ENVIRONMENT]" >&2
  exit 64
fi

base_url=$1
environment=${2:-production}
root=$(cd "$(dirname "$0")/../../.." && pwd)

python3 "$root/pipeline/scripts/gates/release_acceptance.py" live-goldens \
  --repo-root "$root" \
  --contracts "$root/tests/gates/chat_backend_live_goldens.json" \
  --base-url "$base_url" \
  --environment "$environment"
