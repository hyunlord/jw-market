#!/usr/bin/env bash
#
# 260518 correctness rebuild Stage 2 방식 (2) 배포 보조 스크립트.
#
# 무엇을 하는가:
#   검증 완료된 로컬 staging schema에서 cache 3종만 logical dump로 뽑아
#   GCP Galera staging table에 적재하고, 승인 후 blue-green RENAME을 수행한다.
#
# 왜 logical dump인가:
#   Galera는 큰 CTAS/단일 transaction이 writeset 한계에 걸릴 수 있다.
#   따라서 이미 검증된 로컬 cache 결과를 INSERT dump로 옮기고, 운영에서는
#   staging table load와 atomic RENAME만 수행한다.
#
# 도메인 제약:
#   이번 배포 대상은 cache_cause/cache_market_status/cache_brands 세 테이블뿐이다.
#   Agent2 소유 cache_deep_analysis_ai_analysis 및 로컬 cache_deep_analysis는
#   모델 산출물 lifecycle이 달라 절대 전송하거나 swap하지 않는다.
#
# 기각한 대안:
#   - 운영에서 full rebuild: GCP 직접 재현(B4)이 최종 목표지만, 이번은 검증된
#     로컬 cache를 빠르게 반영하는 방식 (2)이므로 제외했다.
#   - CTAS: Galera writeset 리스크 때문에 제외했다.
#   - cache_deep_analysis 포함: Agent2 소유 25행 보호 원칙 때문에 제외했다.
#
# 사용 예:
#   LOCAL_DB_PASS=... REMOTE_SQL='mysql ...' ./pipeline/scripts/deploy_cache_stage2_way2.sh dump-local
#   ./pipeline/scripts/deploy_cache_stage2_way2.sh print-rollback
#
# 실제 SSH/운영 실행은 PL 승인 게이트 뒤에서만 수행한다. 이 스크립트는
# 실행 전에 환경변수가 명시되어 있지 않으면 실패하도록 구성했다.

set -euo pipefail

MODE="${1:-help}"
TS="${DEPLOY_TS:-$(date +%Y%m%d_%H%M%S)}"

LOCAL_HOST="${LOCAL_HOST:-127.0.0.1}"
LOCAL_PORT="${LOCAL_PORT:-3308}"
LOCAL_USER="${LOCAL_USER:-jwapp}"
LOCAL_DB="${LOCAL_DB:-jw_mart_stage1_20260611_015318}"
LOCAL_DB_PASS="${LOCAL_DB_PASS:-}"

REMOTE_DB="${REMOTE_DB:-jw_mart}"
REMOTE_SQL="${REMOTE_SQL:-}"
DUMP_DIR="${DUMP_DIR:-/tmp/jw_stage2_cache_${TS}}"
BATCH_SIZE="${BATCH_SIZE:-200}"

TABLES=(cache_cause cache_market_status cache_brands)
FORBIDDEN_TABLES=(cache_deep_analysis cache_deep_analysis_ai_analysis)

usage() {
  cat <<'EOF'
Usage:
  deploy_cache_stage2_way2.sh dump-local
  deploy_cache_stage2_way2.sh emit-remote-load-sql
  deploy_cache_stage2_way2.sh emit-swap-sql
  deploy_cache_stage2_way2.sh print-rollback

Required environment:
  LOCAL_DB_PASS      Local MariaDB password.

Optional environment:
  LOCAL_DB           Source schema. Default: jw_mart_stage1_20260611_015318
  REMOTE_DB          GCP schema. Default: jw_mart
  DUMP_DIR           Dump output directory.
  DEPLOY_TS          Shared timestamp for old table names.

This script intentionally never references cache_deep_analysis*.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_local_password() {
  [[ -n "$LOCAL_DB_PASS" ]] || fail "LOCAL_DB_PASS is required"
}

assert_safe_tables() {
  local table forbidden
  for table in "${TABLES[@]}"; do
    for forbidden in "${FORBIDDEN_TABLES[@]}"; do
      [[ "$table" != "$forbidden" ]] || fail "forbidden table selected: $table"
    done
  done
}

local_mysql() {
  require_local_password
  mysql -h "$LOCAL_HOST" -P "$LOCAL_PORT" -u "$LOCAL_USER" "-p${LOCAL_DB_PASS}" "$LOCAL_DB" "$@"
}

dump_local() {
  assert_safe_tables
  mkdir -p "$DUMP_DIR"
  for table in "${TABLES[@]}"; do
    printf '[dump] %s.%s -> %s/%s.sql.gz\n' "$LOCAL_DB" "$table" "$DUMP_DIR" "$table"
    mysqldump \
      -h "$LOCAL_HOST" -P "$LOCAL_PORT" -u "$LOCAL_USER" "-p${LOCAL_DB_PASS}" \
      --single-transaction --quick --skip-lock-tables --default-character-set=utf8mb4 \
      "$LOCAL_DB" "$table" \
      | gzip -c > "${DUMP_DIR}/${table}.sql.gz"
  done
}

emit_remote_load_sql() {
  assert_safe_tables
  cat <<SQL
-- Stage 2 방식 (2) remote staging load 준비 SQL.
-- 실제 data load는 dump 안의 INSERT를 staging table명으로 치환해 배치 적용한다.
USE \`${REMOTE_DB}\`;
SQL
  for table in "${TABLES[@]}"; do
    cat <<SQL
DROP TABLE IF EXISTS \`${table}_staging\`;
CREATE TABLE \`${table}_staging\` LIKE \`${table}\`;
SQL
  done
}

emit_swap_sql() {
  assert_safe_tables
  cat <<SQL
-- PL 승인 후에만 실행한다.
-- live -> old, staging -> live atomic RENAME.
USE \`${REMOTE_DB}\`;
SQL
  for table in "${TABLES[@]}"; do
    cat <<SQL
RENAME TABLE \`${table}\` TO \`${table}_old_${TS}\`, \`${table}_staging\` TO \`${table}\`;
SQL
  done
}

print_rollback() {
  assert_safe_tables
  cat <<SQL
-- 문제가 있으면 backend 재배포 없이 이 reverse RENAME으로 복구한다.
USE \`${REMOTE_DB}\`;
SQL
  for table in "${TABLES[@]}"; do
    cat <<SQL
RENAME TABLE \`${table}\` TO \`${table}_failed_${TS}\`, \`${table}_old_${TS}\` TO \`${table}\`;
SQL
  done
}

case "$MODE" in
  dump-local)
    dump_local
    ;;
  emit-remote-load-sql)
    emit_remote_load_sql
    ;;
  emit-swap-sql)
    emit_swap_sql
    ;;
  print-rollback)
    print_rollback
    ;;
  help|--help|-h)
    usage
    ;;
  *)
    usage >&2
    fail "unknown mode: $MODE"
    ;;
esac
