#!/usr/bin/env bash
set -euo pipefail
IDX="${1:?index required}"
STAMP="$(date +%Y%m%d%H%M%S)"
NS=llmops
DB_NAME="${DB_NAME:-jw_mart_d2_stage_20260630_r2}"
DB_USER="${DB_USER:-jw_mart_d2_writer}"
IMAGE="asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01/jw-market-crawl@sha256:64bb2b9f2ad213a06392d5caf9ea4191615d265ecdcfb52b64bba59ae9171268"
JOB="jw-news-crawl-tier2-backfill-m28-${IDX}-${STAMP}"
EVID="/tmp/tier2_mod28_${IDX}_${STAMP}"
mkdir -p "$EVID"
DB_PASS="$(kubectl -n "$NS" get secret jw-mart-d2-writer -o jsonpath='{.data.password}' | base64 -d)"
cat > "$EVID/baseline.sql" <<'SQL'
SELECT 'count','news_raw',COUNT(*) FROM news_raw;
SELECT 'count','events',COUNT(*) FROM events;
SELECT 'count','event_brand_scores',COUNT(*) FROM event_brand_scores;
SELECT 'count','tier2_news',COUNT(*) FROM news_raw WHERE tier=2;
SELECT 'count','tier2_events',COUNT(*) FROM events WHERE tier=2;
SELECT 'count','tier2_scores',COUNT(*) FROM event_brand_scores WHERE tier=2;
SELECT 'hash','news_raw_old',MD5(CONCAT_WS('|',news_id,COALESCE(title,''),COALESCE(article_url,''),COALESCE(article_text,''),COALESCE(CAST(published_date AS CHAR),''),COALESCE(source_name,''),COALESCE(CAST(tier AS CHAR),''),COALESCE(CAST(collected_at AS CHAR),''),COALESCE(CAST(expire_at AS CHAR),''))) FROM news_raw WHERE tier IS NULL ORDER BY news_id LIMIT 1;
SELECT 'hash','events_old',MD5(CONCAT_WS('|',event_id,COALESCE(news_id,''),COALESCE(category,''),COALESCE(category_label,''),COALESCE(summary,''),COALESCE(body_full,''),COALESCE(processed_by,''),COALESCE(CAST(tier AS CHAR),''),COALESCE(CAST(collected_at AS CHAR),''),COALESCE(CAST(expire_at AS CHAR),''))) FROM events WHERE tier IS NULL ORDER BY event_id LIMIT 1;
SELECT 'hash','scores_old',MD5(CONCAT_WS('|',COALESCE(event_id,''),COALESCE(news_id,''),COALESCE(brand_canonical,''),COALESCE(derivation,''),COALESCE(source_processor,''),COALESCE(CAST(score AS CHAR),''),COALESCE(CAST(tier AS CHAR),''),COALESCE(CAST(collected_at AS CHAR),''),COALESCE(CAST(expire_at AS CHAR),''))) FROM event_brand_scores WHERE tier IS NULL ORDER BY event_id, news_id, brand_canonical LIMIT 1;
SQL
kubectl -n "$NS" cp "$EVID/baseline.sql" galera-mariadb-galera-0:/tmp/tier2_baseline_${IDX}.sql -c mariadb-galera >/dev/null
kubectl -n "$NS" exec galera-mariadb-galera-0 -c mariadb-galera -- sh -lc "MYSQL_PWD=\"$DB_PASS\" /opt/bitnami/mariadb/bin/mariadb -u "$DB_USER" -N -B "$DB_NAME" < /tmp/tier2_baseline_${IDX}.sql" > "$EVID/baseline.tsv"
cat > "$EVID/job.yaml" <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB}
  namespace: ${NS}
  labels:
    app: jw-news-crawl
    tier: tier2
    backfill: mod28
    chunk-index: "${IDX}"
spec:
  activeDeadlineSeconds: 14400
  backoffLimit: 0
  template:
    metadata:
      annotations:
        sidecar.istio.io/inject: "false"
      labels:
        app: jw-news-crawl
        tier: tier2
    spec:
      restartPolicy: Never
      nodeSelector:
        genos: "enabled"
      containers:
        - name: jw-news-crawl
          image: ${IMAGE}
          imagePullPolicy: IfNotPresent
          resources:
            requests:
              cpu: "500m"
              memory: "1Gi"
            limits:
              cpu: "1"
              memory: "2Gi"
          env:
            - name: DB_HOST
              value: llmops-mariadb-service.llmops.svc.cluster.local
            - name: DB_PORT
              value: "3306"
            - name: D2_WRITER_USER
              valueFrom:
                secretKeyRef:
                  name: jw-mart-d2-writer
                  key: username
            - name: D2_WRITER_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: jw-mart-d2-writer
                  key: password
            - name: DB_USER
              valueFrom:
                secretKeyRef:
                  name: jw-mart-d2-writer
                  key: username
            - name: DB_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: jw-mart-d2-writer
                  key: password
            - name: DB_NAME
              value: ${DB_NAME}
          command: ["/bin/sh", "-lc"]
          args:
            - |
              set -euo pipefail
              WORK=/tmp/jw-news-crawl/tier2/backfill-m28-${IDX}-$(date +%Y%m%d%H%M%S)
              RAW="\$WORK/raw"
              SCORED="\$WORK/processed"
              SCORED_NEW="\$WORK/processed_new"
              export WORK RAW SCORED SCORED_NEW
              mkdir -p "\$RAW" "\$SCORED" "\$SCORED_NEW"
              python - <<'PY_PRESEED'
              import os
              from pathlib import Path
              import pymysql
              raw = Path(os.environ["RAW"])
              conn = pymysql.connect(
                  host=os.environ["DB_HOST"],
                  port=int(os.environ.get("DB_PORT", "3306")),
                  user=os.environ["DB_USER"],
                  password=os.environ["DB_PASSWORD"],
                  database=os.environ.get("DB_NAME", "jw_mart_d2_stage_20260630_r2"),
                  charset="utf8mb4",
                  cursorclass=pymysql.cursors.Cursor,
              )
              try:
                  with conn.cursor() as cur:
                      cur.execute("SELECT DISTINCT article_url FROM news_raw WHERE article_url IS NOT NULL AND article_url <> ''")
                      urls = [row[0] for row in cur.fetchall() if row and row[0]]
              finally:
                  conn.close()
              (raw / "scraped_urls.txt").write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
              print(f"PRESEED_URL_COUNT={len(urls)}")
              PY_PRESEED
              python crawl/crawler/crawl_2tier.py \
                --tier 2 \
                --run-crawl \
                --score \
                --weekday-slice ${IDX} \
                --slice-mod 28 \
                --days 365 \
                --tier2-concurrent-sites 11 \
                --max-pages-per-site 3 \
                --max-links-per-page 80 \
                --delay-sec 5 \
                --output-dir "\$RAW" \
                --processed-dir "\$SCORED" \
                --brand-plan-output "\$WORK/tier2_brand_plan.json"
              python - <<'PY_RAW_COUNT'
              import json
              import os
              from pathlib import Path
              raw = Path(os.environ["RAW"])
              article_files = [
                  path
                  for path in raw.rglob("*.json")
                  if path.name not in {"crawl_report.json", "tier2_brand_plan.json", "tier2_site_report.json"}
                  and not path.name.endswith("_report.json")
              ]
              site_report_path = raw / "tier2_site_report.json"
              total_news = -1
              if site_report_path.exists():
                  total_news = int(json.loads(site_report_path.read_text(encoding="utf-8")).get("total_news", -1))
              print(f"CRAWL_ARTICLE_FILE_COUNT={len(article_files)}")
              print(f"TIER2_SITE_REPORT_TOTAL_NEWS={total_news}")
              if len(article_files) <= 0 and total_news <= 0:
                  raise SystemExit("ABORT: crawl produced zero raw articles")
              PY_RAW_COUNT
              python - <<'PY_DUP_GATE'
              import json
              import os
              import shutil
              import sys
              from pathlib import Path
              import pymysql
              sys.path[:0] = ["/app/crawl/agent1"]
              from corpus_loader_v2 import generate_news_id, read_json, resolve_news_path, scored_files
              raw = Path(os.environ["RAW"])
              scored = Path(os.environ["SCORED"])
              scored_new = Path(os.environ["SCORED_NEW"])
              files = scored_files(raw, scored)
              items = []
              seen = {}
              duplicate_within = []
              for scored_path in files:
                  scored_json = read_json(scored_path)
                  source_path = resolve_news_path(raw, scored_path, scored_json)
                  news = read_json(source_path)
                  news_id = generate_news_id(news, source_path, scored_json)
                  rel = scored_path.relative_to(scored)
                  if news_id in seen:
                      duplicate_within.append({"news_id": news_id, "first": str(seen[news_id]), "second": str(rel)})
                  else:
                      seen[news_id] = rel
                  items.append({"news_id": news_id, "rel": str(rel), "scored_path": str(scored_path)})
              if duplicate_within:
                  raise SystemExit("ABORT: duplicate candidate IDs within batch: " + json.dumps(duplicate_within[:10], ensure_ascii=False))
              existing = set()
              conn = pymysql.connect(
                  host=os.environ["DB_HOST"],
                  port=int(os.environ.get("DB_PORT", "3306")),
                  user=os.environ["DB_USER"],
                  password=os.environ["DB_PASSWORD"],
                  database=os.environ.get("DB_NAME", "jw_mart_d2_stage_20260630_r2"),
                  charset="utf8mb4",
                  cursorclass=pymysql.cursors.DictCursor,
              )
              try:
                  with conn.cursor() as cur:
                      for i in range(0, len(items), 200):
                          batch = [item["news_id"] for item in items[i:i + 200]]
                          if not batch:
                              continue
                          placeholders = ",".join(["%s"] * len(batch))
                          cur.execute(f"SELECT news_id FROM news_raw WHERE news_id IN ({placeholders})", batch)
                          existing.update(row["news_id"] for row in cur.fetchall())
              finally:
                  conn.close()
              new_count = 0
              for item in items:
                  if item["news_id"] in existing:
                      continue
                  src = Path(item["scored_path"])
                  dst = scored_new / item["rel"]
                  dst.parent.mkdir(parents=True, exist_ok=True)
                  shutil.copy2(src, dst)
                  new_count += 1
              summary = {
                  "candidate_count": len(items),
                  "candidate_unique_count": len(seen),
                  "existing_count": len(existing),
                  "new_count": new_count,
                  "filtered_scored_dir": str(scored_new),
              }
              Path(os.environ["WORK"], "candidate_gate.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
              print("CANDIDATE_GATE=" + json.dumps(summary, ensure_ascii=False, sort_keys=True))
              if new_count <= 0:
                  print("NO_NEW_CANDIDATES_AFTER_GATE=1")
                  Path(os.environ["WORK"], "NO_NEW_CANDIDATES_AFTER_GATE").write_text("1\n", encoding="utf-8")
                  raise SystemExit(0)
              PY_DUP_GATE
              if [ -f "\$WORK/NO_NEW_CANDIDATES_AFTER_GATE" ]; then
                echo "TIER2_BACKFILL_NOOP=no new candidates after duplicate gate"
                exit 0
              fi
              python crawl/agent1/corpus_loader_v2.py \
                --batch-dir "\$RAW" \
                --scored-dir "\$SCORED_NEW" \
                --catalog crawl/config/_catalog.json \
                --output "\$WORK/load_summary.json" \
                --db-name "$DB_NAME" \
                --tier 2 \
                --processed-by tier2_exact_rule_v1
              cat "\$WORK/candidate_gate.json"
              cat "\$WORK/load_summary.json"
YAML
kubectl -n "$NS" apply -f "$EVID/job.yaml" | tee "$EVID/apply.txt"
for _ in $(seq 1 120); do
  POD="$(kubectl -n "$NS" get pod -l job-name="$JOB" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [ -n "$POD" ]; then break; fi
  sleep 2
done
if [ -z "${POD:-}" ]; then echo "pod not found" >&2; exit 1; fi
echo "$JOB" > "$EVID/job_name.txt"
echo "$POD" > "$EVID/pod_name.txt"
nohup kubectl -n "$NS" logs --timestamps -f "$POD" > "$EVID/pod.log" 2>&1 & echo $! > "$EVID/log_pid.txt"
nohup sh -c "while kubectl -n $NS get pod $POD >/dev/null 2>&1; do date -Iseconds; kubectl -n $NS top pod $POD --no-headers 2>/dev/null || true; phase=\$(kubectl -n $NS get pod $POD -o jsonpath='{.status.phase}' 2>/dev/null || true); echo phase=\$phase; [ \"\$phase\" = Succeeded ] && break; [ \"\$phase\" = Failed ] && break; sleep 15; done" > "$EVID/memory.log" 2>&1 & echo $! > "$EVID/mem_pid.txt"
START_EPOCH="$(date +%s)"
while true; do
  STATUS="$(kubectl -n "$NS" get job "$JOB" -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}' 2>/dev/null || true)"
  NOW="$(date -Iseconds)"
  echo "$NOW status=${STATUS:-Running}" | tee -a "$EVID/watch.log"
  case "$STATUS" in
    *"Complete=True"*|*"Failed=True"*) break ;;
  esac
  sleep 60
done
END_EPOCH="$(date +%s)"
echo $((END_EPOCH - START_EPOCH)) > "$EVID/wait_seconds.txt"
sleep 3
kubectl -n "$NS" get job "$JOB" -o yaml > "$EVID/job_final.yaml" || true
kubectl -n "$NS" get pod "$POD" -o yaml > "$EVID/pod_final.yaml" || true
kubectl -n "$NS" describe pod "$POD" > "$EVID/pod_describe.txt" || true
kubectl -n "$NS" logs "$POD" > "$EVID/pod_final.log" 2>&1 || true
cat > "$EVID/post.sql" <<'SQL'
SELECT 'count','news_raw',COUNT(*) FROM news_raw;
SELECT 'count','events',COUNT(*) FROM events;
SELECT 'count','event_brand_scores',COUNT(*) FROM event_brand_scores;
SELECT 'count','tier2_news',COUNT(*) FROM news_raw WHERE tier=2;
SELECT 'count','tier2_events',COUNT(*) FROM events WHERE tier=2;
SELECT 'count','tier2_scores',COUNT(*) FROM event_brand_scores WHERE tier=2;
SELECT 'count','tier2_proc_ok',COUNT(*) FROM event_brand_scores WHERE tier=2 AND source_processor='tier2_exact_rule_v1';
SELECT 'count','tier2_proc_bad',COUNT(*) FROM event_brand_scores WHERE tier=2 AND source_processor <> 'tier2_exact_rule_v1';
SELECT 'count','bad_expire_news',COUNT(*) FROM news_raw WHERE tier=2 AND expire_at <> DATE_ADD(collected_at, INTERVAL 1 YEAR);
SELECT 'count','bad_expire_events',COUNT(*) FROM events WHERE tier=2 AND expire_at <> DATE_ADD(collected_at, INTERVAL 1 YEAR);
SELECT 'count','bad_expire_scores',COUNT(*) FROM event_brand_scores WHERE tier=2 AND expire_at <> DATE_ADD(collected_at, INTERVAL 1 YEAR);
SELECT 'hash','news_raw_old',MD5(CONCAT_WS('|',news_id,COALESCE(title,''),COALESCE(article_url,''),COALESCE(article_text,''),COALESCE(CAST(published_date AS CHAR),''),COALESCE(source_name,''),COALESCE(CAST(tier AS CHAR),''),COALESCE(CAST(collected_at AS CHAR),''),COALESCE(CAST(expire_at AS CHAR),''))) FROM news_raw WHERE tier IS NULL ORDER BY news_id LIMIT 1;
SELECT 'hash','events_old',MD5(CONCAT_WS('|',event_id,COALESCE(news_id,''),COALESCE(category,''),COALESCE(category_label,''),COALESCE(summary,''),COALESCE(body_full,''),COALESCE(processed_by,''),COALESCE(CAST(tier AS CHAR),''),COALESCE(CAST(collected_at AS CHAR),''),COALESCE(CAST(expire_at AS CHAR),''))) FROM events WHERE tier IS NULL ORDER BY event_id LIMIT 1;
SELECT 'hash','scores_old',MD5(CONCAT_WS('|',COALESCE(event_id,''),COALESCE(news_id,''),COALESCE(brand_canonical,''),COALESCE(derivation,''),COALESCE(source_processor,''),COALESCE(CAST(score AS CHAR),''),COALESCE(CAST(tier AS CHAR),''),COALESCE(CAST(collected_at AS CHAR),''),COALESCE(CAST(expire_at AS CHAR),''))) FROM event_brand_scores WHERE tier IS NULL ORDER BY event_id, news_id, brand_canonical LIMIT 1;
SELECT 'sample',n.news_id,n.source_name,LEFT(n.title,80),n.collected_at,n.expire_at,s.brand_canonical,s.source_processor,s.derivation,s.score FROM news_raw n JOIN event_brand_scores s ON s.news_id=n.news_id WHERE n.tier=2 ORDER BY n.collected_at DESC LIMIT 10;
SQL
kubectl -n "$NS" cp "$EVID/post.sql" galera-mariadb-galera-0:/tmp/tier2_post_${IDX}.sql -c mariadb-galera >/dev/null
kubectl -n "$NS" exec galera-mariadb-galera-0 -c mariadb-galera -- sh -lc "MYSQL_PWD=\"$DB_PASS\" /opt/bitnami/mariadb/bin/mariadb -u "$DB_USER" -N -B "$DB_NAME" < /tmp/tier2_post_${IDX}.sql" > "$EVID/post.tsv"
if [ "${STATUS}" = Complete ]; then
  kubectl -n "$NS" delete job "$JOB" --ignore-not-found=true > "$EVID/delete.txt"
fi
echo "EVID=$EVID"
echo "JOB=$JOB"
echo "POD=$POD"
echo "STATUS=$STATUS"
