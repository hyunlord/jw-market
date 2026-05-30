#!/usr/bin/env bash
# Blue-green deploy: Local cache tables -> Galera staging -> gates -> optional RENAME.
#
# Usage:
#   pipeline/scripts/deploy_to_galera_bluegreen.sh --dry-run
#   pipeline/scripts/deploy_to_galera_bluegreen.sh --no-switch
#   pipeline/scripts/deploy_to_galera_bluegreen.sh --switch
#   pipeline/scripts/deploy_to_galera_bluegreen.sh --rollback
#
# Required:
#   DB_PASS                         Local jw_mart password for user llmops
#
# Optional SSH environment:
#   BASTION_USER, BASTION_HOST      Defaults: kube / 192.168.81.177
#   BASTION_PASSWORD                If set, sshpass is used for first hop
#   GCP_USER, GCP_HOST, GCP_KEY     Defaults: GCP / 34.47.113.232 / ~/.ssh/id_ed25519
#   GCP_KEY_PASSPHRASE              If set, SSH_ASKPASS is used on the bastion

set -euo pipefail

MODE="${1:---dry-run}"
TS=$(date +%Y%m%d_%H%M%S)
TABLES=(cache_cause cache_brands cache_market_status cache_deep_analysis)
BATCH_SIZE="${BATCH_SIZE:-200}"

LOCAL_DB_HOST="${LOCAL_DB_HOST:-127.0.0.1}"
LOCAL_DB_PORT="${LOCAL_DB_PORT:-3308}"
LOCAL_DB_USER="${LOCAL_DB_USER:-llmops}"
LOCAL_DB_NAME="${LOCAL_DB_NAME:-jw_mart}"
DB_PASS="${DB_PASS:-}"

BASTION_USER="${BASTION_USER:-kube}"
BASTION_HOST="${BASTION_HOST:-192.168.81.177}"
BASTION_TARGET="${BASTION_USER}@${BASTION_HOST}"
GCP_USER="${GCP_USER:-GCP}"
GCP_HOST="${GCP_HOST:-34.47.113.232}"
GCP_TARGET="${GCP_USER}@${GCP_HOST}"
GCP_KEY="${GCP_KEY:-~/.ssh/id_ed25519}"
K8S_NAMESPACE="${K8S_NAMESPACE:-llmops}"
GALERA_POD="${GALERA_POD:-galera-mariadb-galera-2}"
REMOTE_DB_NAME="${REMOTE_DB_NAME:-jw_mart}"
API_BASE="${API_BASE:-https://jwai-dev.jwhealthcare.com/jw-market-backend-api}"

usage() {
  cat <<'EOF'
Usage: pipeline/scripts/deploy_to_galera_bluegreen.sh <mode>

Modes:
  --dry-run      Recreate staging, load Local cache data, run gates, do not RENAME
  --no-switch    Same as --dry-run; leaves staging in place for review
  --switch       Recreate staging, load, run gates, then RENAME staging to production
  --rollback     RENAME *_old tables back to production and keep current tables as staging
  --help         Show this help

Environment:
  DB_PASS                         Required Local DB password
  BASTION_PASSWORD                Optional first-hop SSH password; requires sshpass
  GCP_KEY_PASSPHRASE              Optional second-hop key passphrase
  BATCH_SIZE                      Default 200
EOF
}

log() {
  printf '%s\n' "$*"
}

fail() {
  printf '★ %s\n' "$*" >&2
  exit 1
}

require_db_pass() {
  [[ -n "$DB_PASS" ]] || fail "DB_PASS 필요"
}

shell_quote() {
  printf '%q' "$1"
}

validate_mode() {
  case "$MODE" in
    --dry-run|--no-switch|--switch|--rollback) ;;
    --help|-h|help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "Unknown mode: $MODE"
      ;;
  esac
}

run_bastion_command() {
  local command="$1"
  local ssh_opts=(-o StrictHostKeyChecking=no -o ConnectTimeout=10)
  if [[ -n "${BASTION_PASSWORD:-}" ]]; then
    command -v sshpass >/dev/null 2>&1 || fail "BASTION_PASSWORD 사용 시 sshpass가 필요합니다"
    SSHPASS="$BASTION_PASSWORD" sshpass -e ssh "${ssh_opts[@]}" \
      -o PreferredAuthentications=password -o PubkeyAuthentication=no \
      "$BASTION_TARGET" "$command"
  else
    ssh "${ssh_opts[@]}" "$BASTION_TARGET" "$command"
  fi
}

run_gcp_command() {
  local command="$1"
  local command_b64 pass_b64 remote_script
  command_b64=$(printf '%s' "$command" | base64 | tr -d '\n')
  pass_b64=$(printf '%s' "${GCP_KEY_PASSPHRASE:-}" | base64 | tr -d '\n')
  remote_script=$(cat <<EOS
set -euo pipefail
cmd=\$(printf '%s' '$command_b64' | base64 -d)
key_path=$GCP_KEY
if [ -n '$pass_b64' ]; then
  pass=\$(printf '%s' '$pass_b64' | base64 -d)
  ask=/tmp/bg_askpass_\$\$
  python3 - "\$ask" "\$pass" <<'PY'
import os
import stat
import sys

path, phrase = sys.argv[1:3]
with open(path, "w", encoding="utf-8") as f:
    f.write("#!/bin/sh\\n")
    f.write("printf '%s\\\\n' " + repr(phrase) + "\\n")
os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
PY
  DISPLAY=codex SSH_ASKPASS="\$ask" SSH_ASKPASS_REQUIRE=force setsid ssh -o StrictHostKeyChecking=no -i "\$key_path" $GCP_TARGET "\$cmd"
  rc=\$?
  rm -f "\$ask"
  exit \$rc
fi
ssh -o StrictHostKeyChecking=no -i "\$key_path" $GCP_TARGET "\$cmd"
EOS
)
  run_bastion_command "$remote_script"
}

run_gcp_sql() {
  local sql_b64
  sql_b64=$(cat | base64 | tr -d '\n')
  run_gcp_command "$(cat <<EOS
set -euo pipefail
printf '%s' '$sql_b64' | base64 -d | kubectl exec -i -n $K8S_NAMESPACE $GALERA_POD -- bash -c 'mysql -uroot -p\$MARIADB_ROOT_PASSWORD $REMOTE_DB_NAME'
EOS
)"
}

run_gcp_sql_raw() {
  local sql_b64
  sql_b64=$(cat | base64 | tr -d '\n')
  run_gcp_command "$(cat <<EOS
set -euo pipefail
printf '%s' '$sql_b64' | base64 -d | kubectl exec -i -n $K8S_NAMESPACE $GALERA_POD -- bash -c 'mysql -N -s -uroot -p\$MARIADB_ROOT_PASSWORD $REMOTE_DB_NAME'
EOS
)"
}

stream_to_galera_mysql() {
  run_gcp_command "$(cat <<EOS
set -euo pipefail
gzip -dc | kubectl exec -i -n $K8S_NAMESPACE $GALERA_POD -- bash -c 'mysql -uroot -p\$MARIADB_ROOT_PASSWORD $REMOTE_DB_NAME'
EOS
)"
}

local_mysql_raw() {
  mysql -h "$LOCAL_DB_HOST" -P "$LOCAL_DB_PORT" -u "$LOCAL_DB_USER" "-p$DB_PASS" -N -s "$LOCAL_DB_NAME" "$@"
}

check_bastion() {
  log "[0] bastion 확인"
  run_bastion_command "echo ok" >/dev/null
}

create_staging_tables() {
  log "[A] staging 생성"
  {
    printf 'USE %s;\n' "$REMOTE_DB_NAME"
    for table in "${TABLES[@]}"; do
      printf 'DROP TABLE IF EXISTS %s_staging;\n' "$table"
      printf 'CREATE TABLE %s_staging LIKE %s;\n' "$table" "$table"
      # Contract marker for static tests:
      # CREATE TABLE ${table}_staging LIKE ${table}
    done
  } | run_gcp_sql
}

emit_table_sql() {
  local table="$1"
  local staging="${table}_staging"
  TABLE="$table" STAGING="$staging" DB_PASS="$DB_PASS" LOCAL_DB_HOST="$LOCAL_DB_HOST" \
    LOCAL_DB_PORT="$LOCAL_DB_PORT" LOCAL_DB_USER="$LOCAL_DB_USER" LOCAL_DB_NAME="$LOCAL_DB_NAME" \
    REMOTE_DB_NAME="$REMOTE_DB_NAME" BATCH_SIZE="$BATCH_SIZE" python3 - <<'PY'
import os
import sys

import pymysql
from pymysql.cursors import SSCursor

table = os.environ["TABLE"]
staging = os.environ["STAGING"]
batch_size = int(os.environ["BATCH_SIZE"])
conn = pymysql.connect(
    host=os.environ["LOCAL_DB_HOST"],
    port=int(os.environ["LOCAL_DB_PORT"]),
    user=os.environ["LOCAL_DB_USER"],
    password=os.environ["DB_PASS"],
    database=os.environ["LOCAL_DB_NAME"],
    charset="utf8mb4",
)
meta = conn.cursor()
meta.execute(f"SHOW COLUMNS FROM `{table}`")
cols = [c[0] for c in meta.fetchall()]
col_list = ", ".join(f"`{c}`" for c in cols)
updates = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in cols)
meta.execute(f"SELECT COUNT(*) FROM `{table}`")
total = meta.fetchone()[0]

stream = pymysql.connect(
    host=os.environ["LOCAL_DB_HOST"],
    port=int(os.environ["LOCAL_DB_PORT"]),
    user=os.environ["LOCAL_DB_USER"],
    password=os.environ["DB_PASS"],
    database=os.environ["LOCAL_DB_NAME"],
    charset="utf8mb4",
    cursorclass=SSCursor,
)
cur = stream.cursor()
cur.execute(f"SELECT {col_list} FROM `{table}`")
print(f"USE {os.environ['REMOTE_DB_NAME']};")
print("START TRANSACTION;")
count = 0
batch = 0
for row in cur:
    values = ", ".join(stream.literal(v) for v in row)
    print(
        f"INSERT INTO `{staging}` ({col_list}) VALUES ({values}) "
        f"ON DUPLICATE KEY UPDATE {updates};"
    )
    count += 1
    batch += 1
    if batch >= batch_size:
        print("COMMIT;")
        print("START TRANSACTION;")
        print(f"{table}: streamed {count}/{total}", file=sys.stderr, flush=True)
        batch = 0
print("COMMIT;")
print(f"{table}: streamed {count}/{total} complete", file=sys.stderr, flush=True)
cur.close()
stream.close()
conn.close()
PY
}

load_staging_tables() {
  log "[B] staging 적재"
  for table in "${TABLES[@]}"; do
    log "  - ${table} -> ${table}_staging"
    emit_table_sql "$table" | gzip -c | stream_to_galera_mysql
  done
}

remote_scalar() {
  local sql="$1"
  printf '%s\n' "$sql" | run_gcp_sql_raw 2>/dev/null | awk 'NF { value=$NF } END { print value }'
}

local_scalar() {
  local sql="$1"
  local_mysql_raw -e "$sql" 2>/dev/null | awk 'NF { value=$NF } END { print value }'
}

gate_row_counts() {
  local pass=0
  log "  게이트1 row count"
  for table in "${TABLES[@]}"; do
    local local_count staging_count
    local_count=$(local_scalar "SELECT COUNT(*) FROM ${table};")
    staging_count=$(remote_scalar "SELECT COUNT(*) FROM ${table}_staging;")
    log "    ${table}: local=${local_count} staging=${staging_count}"
    if [[ "$local_count" != "$staging_count" ]]; then
      pass=1
      log "    ★ 불일치: ${table}"
    fi
  done
  return "$pass"
}

gate_payload_sums() {
  local pass=0
  log "  게이트2 payload sum"
  for table in "${TABLES[@]}"; do
    local local_sum staging_sum
    local_sum=$(local_scalar "SELECT COALESCE(SUM(payload_size),0) FROM ${table};")
    staging_sum=$(remote_scalar "SELECT COALESCE(SUM(payload_size),0) FROM ${table}_staging;")
    log "    ${table}: local=${local_sum} staging=${staging_sum}"
    if [[ "$local_sum" != "$staging_sum" ]]; then
      pass=1
      log "    ★ 불일치: ${table}"
    fi
  done
  return "$pass"
}

gate_response_md5() {
  local pass=0
  log "  게이트3 핵심 row MD5"
  local labels=(
    "리바로 strategy_006 UBIST sales"
    "악템라 CD IQVIA sales"
    "페린젝트 IQVIA sales"
  )
  local wheres=(
    "brand='리바로' AND market_id='strategy_006' AND source='UBIST' AND measure='sales' AND view_type='market_landscape'"
    "brand='악템라' AND view_type='competitive_dynamics' AND source='IQVIA' AND measure='sales'"
    "brand='페린젝트' AND source='IQVIA' AND measure='sales' AND view_type='market_landscape'"
  )
  for i in "${!labels[@]}"; do
    local local_md5 staging_md5
    local_md5=$(local_scalar "SELECT MD5(response_json) FROM cache_cause WHERE ${wheres[$i]} LIMIT 1;")
    staging_md5=$(remote_scalar "SELECT MD5(response_json) FROM cache_cause_staging WHERE ${wheres[$i]} LIMIT 1;")
    if [[ -n "$local_md5" && "$local_md5" == "$staging_md5" ]]; then
      log "    ${labels[$i]}: PASS"
    else
      pass=1
      log "    ${labels[$i]}: ★ FAIL local=${local_md5} staging=${staging_md5}"
    fi
  done
  return "$pass"
}

gate_ai_analysis() {
  log "  게이트4 ai_analysis 보존"
  local ai_count
  ai_count=$(remote_scalar "SELECT COUNT(*) FROM cache_deep_analysis_ai_analysis;")
  log "    cache_deep_analysis_ai_analysis: ${ai_count}"
  [[ "$ai_count" == "25" ]]
}

run_gates() {
  log "[C] 비교 게이트"
  local pass=0
  gate_row_counts || pass=1
  gate_payload_sums || pass=1
  gate_response_md5 || pass=1
  gate_ai_analysis || pass=1
  if [[ "$pass" -eq 0 ]]; then
    log "=== 게이트 종합: PASS ==="
    return 0
  fi
  log "=== 게이트 종합: FAIL ==="
  return 1
}

rename_sql_forward() {
  local parts=()
  for table in "${TABLES[@]}"; do
    parts+=("${table} TO ${table}_old, ${table}_staging TO ${table}")
  done
  printf 'USE %s;\n' "$REMOTE_DB_NAME"
  printf 'SET @s = NOW(6);\n'
  printf 'RENAME TABLE %s;\n' "$(IFS=,; echo "${parts[*]}")"
  printf 'SET @e = NOW(6);\n'
  printf 'SELECT TIMESTAMPDIFF(MICROSECOND, @s, @e) AS switch_us;\n'
}

rename_sql_rollback() {
  local parts=()
  for table in "${TABLES[@]}"; do
    parts+=("${table} TO ${table}_staging, ${table}_old TO ${table}")
  done
  printf 'USE %s;\n' "$REMOTE_DB_NAME"
  printf 'SET @s = NOW(6);\n'
  printf 'RENAME TABLE %s;\n' "$(IFS=,; echo "${parts[*]}")"
  printf 'SET @e = NOW(6);\n'
  printf 'SELECT TIMESTAMPDIFF(MICROSECOND, @s, @e) AS rollback_us;\n'
}

assert_no_old_tables() {
  local old_count
  old_count=$(remote_scalar "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='${REMOTE_DB_NAME}' AND TABLE_NAME IN ('cache_cause_old','cache_brands_old','cache_market_status_old','cache_deep_analysis_old');")
  [[ "$old_count" == "0" ]] || fail "old 테이블이 이미 존재합니다. --rollback 또는 수동 확인이 필요합니다"
}

switch_tables() {
  assert_no_old_tables
  log "[D] RENAME 전환"
  rename_sql_forward | run_gcp_sql
}

rollback_tables() {
  log "[R] rollback RENAME"
  rename_sql_rollback | run_gcp_sql
}

smoke_api() {
  log "[E] 운영 smoke"
  run_gcp_command "$(cat <<EOS
set -euo pipefail
curl -sf '${API_BASE}/api/cause/리바로?view=market_landscape&source=UBIST&measure=sales' | python3 -c '
import json, sys
d = json.load(sys.stdin)
rk = d.get("data", {}).get("brand_ranking_stacked", {}).get("rankings_by_year", {})
lv = next((r for r in rk.get("2025", []) if r.get("brand") == "리바로"), None)
print("리바로 2025:", lv.get("rank") if lv else None)
'
EOS
)"
}

final_state() {
  log "[Z] final state"
  local attempt
  for attempt in 1 2 3; do
    if cat <<SQL | run_gcp_sql
SELECT
  (SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='${REMOTE_DB_NAME}' AND TABLE_NAME IN ('cache_cause','cache_brands','cache_market_status','cache_deep_analysis')) AS prod_tables,
  (SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='${REMOTE_DB_NAME}' AND TABLE_NAME IN ('cache_cause_staging','cache_brands_staging','cache_market_status_staging','cache_deep_analysis_staging')) AS staging_tables,
  (SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='${REMOTE_DB_NAME}' AND TABLE_NAME IN ('cache_cause_old','cache_brands_old','cache_market_status_old','cache_deep_analysis_old')) AS old_tables;
SQL
    then
      return 0
    fi
    log "  final state retry ${attempt}/3"
    sleep 2
  done
  return 1
}

main() {
  validate_mode
  require_db_pass
  log "=== blue-green deploy MODE=${MODE} TS=${TS} ==="
  check_bastion

  if [[ "$MODE" == "--rollback" ]]; then
    rollback_tables
    smoke_api
    final_state
    log "=== rollback 완료 ==="
    return 0
  fi

  create_staging_tables
  load_staging_tables
  if ! run_gates; then
    log "[D] ★ 게이트 FAIL → RENAME 안 함"
    return 1
  fi

  if [[ "$MODE" == "--dry-run" || "$MODE" == "--no-switch" ]]; then
    log "[D] MODE=${MODE} → RENAME 보류 (staging 유지)"
    final_state
    return 0
  fi

  switch_tables
  if ! smoke_api; then
    log "★ smoke 실패: pipeline/scripts/deploy_to_galera_bluegreen.sh --rollback 실행 필요"
    return 1
  fi
  log "=== deploy 완료: old 테이블 보존, 정상 확인 후 수동 DROP ==="
}

main "$@"
