#!/usr/bin/env bash
set -euo pipefail

temporal schedule create \
  --address "${TEMPORAL_ADDRESS:-temporal-frontend.temporal.svc:7233}" \
  --namespace default \
  --schedule-id jw-agent2-agent3-weekly-v1 \
  --cron '30 12 * * Sat' \
  --time-zone Asia/Seoul \
  --task-queue jw-agent-refresh-weekly-v1 \
  --type jw_agent2_agent3_weekly_v1 \
  --overlap-policy Skip \
  --pause-on-failure \
  --catchup-window 1h \
  --execution-timeout 10h \
  --run-timeout 10h \
  --notes 'Global Agent2 staging then Agent3 refresh; guarded against ingest/agent overlap and Galera disk below 20 percent'
