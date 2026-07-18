#!/usr/bin/env bash
set -euo pipefail
IDX="${1:?idx}"
JOB="${2:?job}"
EVID="${3:?evid}"
NS=llmops
DB_NAME="${DB_NAME:-jw_mart_d2_stage_20260630_r2}"
DB_USER="${DB_USER:-jw_mart_d2_writer}"
POD=$(kubectl -n "$NS" get pod -l job-name="$JOB" -o jsonpath='{.items[0].metadata.name}')
DB_PASS=$(kubectl -n "$NS" get secret jw-mart-d2-writer -o jsonpath='{.data.password}' | base64 -d)
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
kubectl -n "$NS" get job "$JOB" -o wide > "$EVID/job_status.txt" || true
kubectl -n "$NS" delete job "$JOB" --ignore-not-found=true > "$EVID/delete.txt"
echo "EVID=$EVID"
cat "$EVID/job_status.txt"
echo --- baseline ---
cat "$EVID/baseline.tsv"
echo --- post ---
sed -n '1,40p' "$EVID/post.tsv"
echo --- load tail ---
tail -n 80 "$EVID/pod_final.log"
