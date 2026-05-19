#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

PASS_COUNT=0
FAIL_COUNT=0

pass() {
  printf '[PASS] %s\n' "$1"
  PASS_COUNT=$((PASS_COUNT + 1))
}

fail() {
  printf '[FAIL] %s\n' "$1"
  printf '       %s\n' "$2"
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

check() {
  local name="$1"
  shift
  local output
  if output="$("$@" 2>&1)"; then
    pass "$name"
    if [[ -n "$output" ]]; then
      printf '       %s\n' "$output"
    fi
  else
    fail "$name" "$output"
  fi
}

if [[ ! -f .env ]]; then
  fail ".env exists" "Copy .env.example to .env and set local passwords first."
  printf '\nSummary: FAIL (%d passed, %d failed)\n' "$PASS_COUNT" "$FAIL_COUNT"
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

MARIADB_DATABASE="${MARIADB_DATABASE:-jw_mart}"
MARIADB_USER="${MARIADB_USER:-jwapp}"

root_sql() {
  docker exec jw-mariadb mariadb \
    -uroot \
    -p"${MARIADB_ROOT_PASSWORD}" \
    --batch \
    --raw \
    --skip-column-names \
    "$@"
}

app_sql() {
  docker exec jw-mariadb mariadb \
    -u"${MARIADB_USER}" \
    -p"${MARIADB_PASSWORD}" \
    --batch \
    --raw \
    --skip-column-names \
    "$@"
}

expect_eq() {
  local expected="$1"
  local actual="$2"
  local label="$3"
  if [[ "$actual" == "$expected" ]]; then
    pass "$label"
    printf '       %s\n' "$actual"
  else
    fail "$label" "expected=${expected}, actual=${actual}"
  fi
}

expect_contains() {
  local needle="$1"
  local actual="$2"
  local label="$3"
  if [[ "$actual" == *"$needle"* ]]; then
    pass "$label"
    printf '       %s\n' "$actual"
  else
    fail "$label" "expected output to contain ${needle}; actual=${actual}"
  fi
}

printf 'D13-la MariaDB Galera local verification\n'
printf 'Container: jw-mariadb\n'
printf 'Database : %s\n\n' "$MARIADB_DATABASE"

check "docker compose can read project" docker compose ps

health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' jw-mariadb 2>&1)"
expect_eq "healthy" "$health" "container health is healthy"

version="$(root_sql -e "SELECT VERSION();" 2>&1)"
expect_contains "12.0.2" "$version" "MariaDB version is 12.0.2"

db_exists="$(root_sql -e "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='${MARIADB_DATABASE}';" 2>&1)"
expect_eq "1" "$db_exists" "database exists"

app_access="$(app_sql "${MARIADB_DATABASE}" -e "SELECT DATABASE();" 2>&1)"
expect_eq "$MARIADB_DATABASE" "$app_access" "app user can access database"

json_result="$(
  root_sql "${MARIADB_DATABASE}" -e "
    DELETE FROM _jsoncheck WHERE id = 1;
    INSERT INTO _jsoncheck (id, payload)
    VALUES (1, JSON_OBJECT('name', 'Atorvastatin', 'amount', 100))
    ON DUPLICATE KEY UPDATE payload = VALUES(payload);
    SELECT CONCAT(name, '|', JSON_UNQUOTE(JSON_EXTRACT(payload, '$.amount')))
    FROM _jsoncheck
    WHERE id = 1;
  " 2>&1
)"
expect_eq "Atorvastatin|100" "$json_result" "JSON insert/select and virtual generated column"

index_exists="$(root_sql "${MARIADB_DATABASE}" -e "SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA='${MARIADB_DATABASE}' AND TABLE_NAME='_jsoncheck' AND INDEX_NAME='idx_name';" 2>&1)"
expect_eq "1" "$index_exists" "virtual generated column index exists"

explain_plan="$(root_sql "${MARIADB_DATABASE}" -e "EXPLAIN SELECT id FROM _jsoncheck WHERE name='Atorvastatin';" 2>&1)"
expect_contains "idx_name" "$explain_plan" "virtual generated column index is usable"

timezone="$(root_sql -e "SELECT @@global.time_zone;" 2>&1)"
expect_eq "+09:00" "$timezone" "global timezone is KST"

charset="$(root_sql -e "SELECT CONCAT(DEFAULT_CHARACTER_SET_NAME, '|', DEFAULT_COLLATION_NAME) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='${MARIADB_DATABASE}';" 2>&1)"
expect_eq "utf8mb4|utf8mb4_unicode_ci" "$charset" "database charset/collation"

printf '\nSummary: '
if [[ "$FAIL_COUNT" -eq 0 ]]; then
  printf 'PASS (%d passed, %d failed)\n' "$PASS_COUNT" "$FAIL_COUNT"
  exit 0
else
  printf 'FAIL (%d passed, %d failed)\n' "$PASS_COUNT" "$FAIL_COUNT"
  exit 1
fi

