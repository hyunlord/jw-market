#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 3 ]]; then
  echo "usage: $0 AUDIT_ROOT [EXPECTED_COUNT] [ENVIRONMENT]" >&2
  exit 64
fi

audit_root=$1
expected_count=${2:-5}
environment=${3:-production}
root=$(cd "$(dirname "$0")/../../.." && pwd)

python3 "$root/pipeline/scripts/gates/release_acceptance.py" golden-tsv \
  --observations "$audit_root/raw/production_goldens.tsv" \
  --expected-count "$expected_count" \
  --gate-id all_four_goldens \
  --environment "$environment"
