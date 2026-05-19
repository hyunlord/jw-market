#!/usr/bin/env bash
set -euo pipefail

CMD="${1:-help}"

mkdir -p /data /output
ln -sfn /data /app/data
ln -sfn /output /app/output

case "$CMD" in
  migrate)
    python /app/pipeline/scripts/run_migration.py "${@:2}"
    ;;
  load-ubist)
    python /app/pipeline/scripts/etl/ubist_parquet_loader.py "${@:2}"
    ;;
  load-iqvia)
    python /app/pipeline/scripts/etl/iqvia_loader.py "${@:2}"
    ;;
  enrich)
    python /app/pipeline/scripts/etl/layer2_enrich.py "${@:2}"
    ;;
  shell)
    /bin/bash
    ;;
  help|*)
    cat <<'EOF'
Usage: <migrate|load-ubist|load-iqvia|enrich|shell> [args...]

Examples:
  migrate status
  migrate apply --all
  load-ubist --all --truncate
  load-iqvia --all
  enrich --all
EOF
    ;;
esac
