#!/usr/bin/env bash
set -euo pipefail
ROOT=${ROOT:?}; CM=${CM:?}; IMG=${IMG:?}; CHUNK=${1:?chunk}; NS=llmops
JOB="wf196-v3-rescore-c${CHUNK}-$(date -u +%Y%m%d%H%M%S)"
MAN="$ROOT/manifests/${JOB}.yaml"; LOG="$ROOT/logs/chunk_${CHUNK}.log"; FINAL_LOG="$ROOT/logs/chunk_${CHUNK}.final.log"; STATUS="$ROOT/state/chunk_${CHUNK}.status"
echo -e "chunk\t$CHUNK\tSTART\t$(date -u +%FT%TZ)\tjob=$JOB" | tee -a "$ROOT/state/state.tsv"
cat > "$MAN" <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB}
  namespace: ${NS}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 7200
  template:
    metadata:
      annotations:
        sidecar.istio.io/inject: "false"
    spec:
      restartPolicy: Never
      containers:
      - name: rescore
        image: ${IMG}
        imagePullPolicy: IfNotPresent
        resources:
          requests: {cpu: "200m", memory: "512Mi"}
          limits: {cpu: "1", memory: "2Gi"}
        env:
        - {name: DB_HOST, value: llmops-mariadb-service.llmops.svc.cluster.local}
        - {name: DB_PORT, value: "3306"}
        - name: D2_WRITER_USER
          valueFrom:
            secretKeyRef: {name: jw-mart-d2-writer, key: username}
        - name: D2_WRITER_PASSWORD
          valueFrom:
            secretKeyRef: {name: jw-mart-d2-writer, key: password}
        - name: DB_USER
          valueFrom:
            secretKeyRef: {name: jw-mart-d2-writer, key: username}
        - name: DB_PASSWORD
          valueFrom:
            secretKeyRef: {name: jw-mart-d2-writer, key: password}
        - {name: DB_NAME, value: jw_mart_d2_stage_20260630_r2}
        - {name: CHUNK_INDEX, value: "${CHUNK}"}
        - {name: PYTHONUNBUFFERED, value: "1"}
        - {name: MALLOC_ARENA_MAX, value: "2"}
        volumeMounts:
        - {name: scripts, mountPath: /scripts, readOnly: true}
        command: ["/bin/bash", "-lc"]
        args:
        - |
          set -euo pipefail
          mkdir -p /tmp/wf196_v3_chunks
          cp /scripts/catalog_text.txt /tmp/wf196_catalog_text.txt
          python /scripts/rescore_chunk.py
          python /scripts/apply_chunk.py
          rc=1
          for attempt in 1 2 3 4 5 6; do
            echo "VERIFY_ATTEMPT=\${attempt}"
            set +e
            python /scripts/verify_chunk.py
            rc=\$?
            set -e
            if [[ \$rc -eq 0 ]]; then break; fi
            echo "VERIFY_RETRY_AFTER_RC=\${rc} attempt=\${attempt}"
            sleep 20
          done
          exit "\$rc"
      volumes:
      - name: scripts
        configMap: {name: ${CM}}
YAML
kubectl -n "$NS" apply -f "$MAN" | tee -a "$LOG"
POD=""
for i in {1..120}; do POD=$(kubectl -n "$NS" get pod -l job-name="$JOB" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true); [[ -n "$POD" ]] && break; sleep 2; done
echo "POD=$POD" | tee -a "$LOG"
if [[ -z "$POD" ]]; then echo NO_POD | tee "$STATUS"; exit 20; fi
(kubectl -n "$NS" logs -f "$POD" -c rescore >> "$LOG" 2>&1) & LOGPID=$!
WAIT_RC=1; PHASE=""
for i in {1..730}; do
  PHASE=$(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.status.phase}' 2>/dev/null || true)
  [[ "$PHASE" == "Succeeded" || "$PHASE" == "Failed" ]] && { WAIT_RC=0; break; }
  sleep 10
done
sleep 2; kill "$LOGPID" >/dev/null 2>&1 || true
kubectl -n "$NS" logs "$POD" -c rescore > "$FINAL_LOG" 2>&1 || true
kubectl -n "$NS" get job "$JOB" -o yaml > "$ROOT/manifests/${JOB}.status.yaml" 2>&1 || true
LAST_VERIFY=$(grep 'VERIFY_SUMMARY=' "$FINAL_LOG" | tail -n 1 || true); APPLY=$(grep 'APPLY_SUMMARY=' "$FINAL_LOG" | tail -n 1 || true)
if [[ "$LAST_VERIFY" == *'"live_matches_checkpoint": true'* && "$LAST_VERIFY" == *'"backup_intact": true'* && "$LAST_VERIFY" == *'"cross_match_unchanged": true'* && "$LAST_VERIFY" == *'"future_unprocessed_unchanged": true'* ]]; then
  echo -e "chunk\t$CHUNK\tPASS\t$(date -u +%FT%TZ)\tjob=$JOB\twait_rc=$WAIT_RC\tpod_phase=$PHASE\t$APPLY\t$LAST_VERIFY" | tee -a "$ROOT/state/state.tsv"
  echo PASS > "$STATUS"
  kubectl -n "$NS" delete job "$JOB" --ignore-not-found=true >> "$LOG" 2>&1 || true
  exit 0
fi
echo -e "chunk\t$CHUNK\tFAIL\t$(date -u +%FT%TZ)\tjob=$JOB\twait_rc=$WAIT_RC\tpod_phase=$PHASE\t$APPLY\t$LAST_VERIFY" | tee -a "$ROOT/state/state.tsv"
echo FAIL > "$STATUS"; touch "$ROOT/ABORT"; exit 30
