#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "usage: $0 GATE_ID CANDIDATES_JSON CENSUS_JSON ENVIRONMENT" >&2
  exit 64
fi

gate_id=$1
candidates=$2
census=$3
environment=$4
root=$(cd "$(dirname "$0")/../../.." && pwd)

case "$gate_id" in
  f062_molecule_parity|f062_corpus_parity) ;;
  *)
    echo "unsupported F-062 gate id: $gate_id" >&2
    exit 64
    ;;
esac

python3 "$root/pipeline/scripts/gates/release_acceptance.py" population \
  --gate-id "$gate_id" \
  --candidates "$candidates" \
  --census "$census" \
  --environment "$environment"
