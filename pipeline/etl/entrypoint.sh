#!/usr/bin/env bash
set -euo pipefail

CMD="${1:-help}"
case "$CMD" in
  all)          python -m pipeline.etl.run --all "${@:2}" ;;
  period)       python -m pipeline.etl.run --period "${@:2}" ;;
  apply-change) python -m pipeline.etl.run --apply-change "${@:2}" ;;
  stage)        python -m pipeline.etl.run --stage "${@:2}" ;;
  shell)        /bin/bash ;;
  help|*)       python -m pipeline.etl.run --help ;;
esac

