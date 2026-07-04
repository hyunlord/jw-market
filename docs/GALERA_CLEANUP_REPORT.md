# Galera cleanup report

작업일: 2026-06-30

## 요약

- `codex_stage2_scratch`: Galera `information_schema.schemata`에 존재하지 않아 DROP 실행 없음.
- `jw_mart_test_stage2`: 실제 런타임 참조와 코드 참조를 확인한 뒤 메타 백업을 남기고 DROP 완료. 회수량은 DROP 전 기준 약 1.95 GiB.
- `jw_mart_d1_stage_20260625_173115`: test2 backend가 `DB_NAME`/`BRIDGE_DB_NAME`으로 실제 사용 중이므로 DROP 금지. 제거는 PL 승인과 test2 DB 전환 후 별도 작업.
- staging 재발 방지: `jw_mart_d1_stage_*` import와 `jw_mart_d2_strategic_dim_stage_*` sidecar build에 성공 후 cleanup 옵션을 추가했다. 실패 시 staging schema는 보존된다.
- binlog: PURGE/retention 변경은 하지 않았다. 현재 14일 설정이지만 실제 파일 span은 약 5.68일, 총 약 209.26 GiB이다.

## STAGE 1. 안전 스키마 정리

### `codex_stage2_scratch`

- 확인: Galera schema 목록에 없음.
- 조치: DROP 실행 없음.
- 결과: 회수량 없음.

### `jw_mart_test_stage2`

DROP 전 확인:

- 크기: 13 tables, 약 1.95 GiB, approximate rows 15,225.
- 주요 테이블:
  - `mart_strategic_ml_brand_metric`: 약 1049.14 MiB.
  - `cache_deep_analysis_prodtest2_20260611_223500`: 약 281.70 MiB.
  - `cache_deep_analysis_prodtest2_20260611_223900`: 약 281.70 MiB.
  - `cache_deep_analysis_test2_20260611_220946`: 약 249.73 MiB.
  - `cache_cause_test2_20260611_195205`: 약 131.55 MiB.
- K8s 실제 env 참조:
  - `jw-market-backend-api`: `DB_NAME=jw_mart`.
  - `jw-market-backend-api-test`: `DB_NAME=jw_mart_d1_stage_20260625_173115`, `BRIDGE_DB_NAME=jw_mart_d1_stage_20260625_173115`.
  - `jw-chat-agent-poc`: `CHAT_CACHE_DB_NAME=jw_mart`.
  - `jw_mart_test_stage2`는 실제 deployment spec env에 없음. 과거 last-applied annotation에만 잔존 문자열이 있었다.
- DB 참조:
  - 외부 FK 참조 없음.
  - routine 없음.
  - view는 schema 내부 cache alias views만 확인.
- 코드 grep:
  - `jw_mart_test_stage2`는 보호 목록/테스트에서만 발견.
  - live/test backend runtime 경로 참조 없음.

메타 백업:

- 파일: `/tmp/jw_mart_test_stage2_metadata_root_20260630_002834.sql`
- SHA256: `960088d89e9503cfd141c6c8694677b57f4e64f88724624ead4341fdc614250f`
- 비고: `llmops` 계정은 `SHOW VIEW`/`DROP DATABASE` 권한이 없어 admin 계정으로 메타 백업 및 DROP을 수행했다.

DROP:

- 실행: `DROP DATABASE jw_mart_test_stage2`
- 결과: 성공.
- 회수량: DROP 전 schema size 기준 약 1.95 GiB.

3노드 정합:

| Node | wsrep state | cluster size | cluster status | wsrep ready | schema count |
| --- | --- | ---: | --- | --- | ---: |
| `galera-mariadb-galera-0` | Synced | 3 | Primary | ON | 0 |
| `galera-mariadb-galera-1` | Synced | 3 | Primary | ON | 0 |
| `galera-mariadb-galera-2` | Synced | 3 | Primary | ON | 0 |

## STAGE 2. staging 자동 cleanup

### 설계 선택

타임스탬프 staging schema를 유지하되, 성공 검증 이후 cleanup을 옵션으로 제공하는 방식을 선택했다.

- 고정 이름 재사용은 동시 실행 충돌과 실패 원인 보존 문제가 있다.
- 타임스탬프 schema + 성공 후 DROP은 병렬 실행 충돌이 적고, 실패 시 schema를 남겨 디버깅할 수 있다.
- 기본값은 보수적으로 `False`이다. cleanup이 필요한 자동화 경로에서 `--drop-target-after-success`를 명시한다.

### `jw_mart_d1_stage_*` import cleanup

파일: `pipeline/scripts/deploy/strategic_stage_import.py`

변경:

- `ImportConfig`/`DumpImportConfig`에 `drop_target_after_success` 추가.
- CLI에 `--drop-target-after-success` 추가.
- restore와 manifest verification이 성공한 뒤에만 `drop_schema_if_unprotected(conn, target_db)` 호출.
- manifest/summary에 `cleanup.target_db_dropped` 기록.
- 실패 시 DROP 경로에 도달하지 않으므로 staging schema는 보존된다.

보호:

- 기존 `guard_stage_import`와 `drop_schema_if_unprotected` 보호를 그대로 사용한다.
- live/protected schema는 차단된다.

### `jw_mart_d2_strategic_dim_stage_*` sidecar cleanup

파일: `pipeline/scripts/build_strategic_filter_dimension_metric.py`

변경:

- CLI에 `--drop-target-after-success` 추가.
- 성공적인 `build_strategic_sidecar` 이후 `drop_stage_schema` 호출.
- `drop_stage_schema`는 `^jw_mart_d2_strategic_dim_stage_[0-9]{8}_[0-9]{6}$` 패턴만 DROP한다.
- manifest에 `cleanup.target_db_dropped` 기록.
- 실패 시 cleanup이 실행되지 않아 staging schema가 보존된다.

### 검증

실행:

```bash
python3 -m py_compile \
  pipeline/scripts/deploy/strategic_stage_import.py \
  pipeline/scripts/build_strategic_filter_dimension_metric.py

python3 -m pytest \
  tests/deploy/test_strategic_stage_import.py \
  tests/scripts/test_build_strategic_filter_dimension_metric.py \
  -q
```

결과:

- `py_compile`: 성공.
- pytest: `12 passed in 0.48s`.

## STAGE 3. d1 stage + binlog 확인

### `jw_mart_d1_stage_20260625_173115`

상태:

- 크기: 약 43.89 GiB.
- tables: 12.
- approximate rows: 808,219.

사용 여부:

- `jw-market-backend-api-test` deployment의 실제 env:
  - `DB_NAME=jw_mart_d1_stage_20260625_173115`
  - `BRIDGE_DB_NAME=jw_mart_d1_stage_20260625_173115`
- test2 backend health endpoint는 200을 반환했다.

판정:

- 현재 test2 serving DB로 실제 사용 중이다.
- 이번 작업에서는 DROP 준비만 가능하며 실행은 금지했다.

DROP 준비 조건:

1. test2 backend의 `DB_NAME`/`BRIDGE_DB_NAME`을 새 serving schema로 전환한다.
2. rollout 후 endpoint smoke와 핵심 동적시장/옵션 검증을 통과한다.
3. `jw_mart_d1_stage_20260625_173115` 참조가 K8s env/ConfigMap/Secret/code에서 0건임을 재확인한다.
4. 메타 백업과 필요 시 logical backup 정책을 확정한다.
5. PL 승인 후 DROP한다.

### binlog

확인값:

| Item | Value |
| --- | --- |
| `log_bin` | ON |
| `binlog_expire_logs_seconds` | 1209600 |
| `expire_logs_days` | 14 |
| binlog basename | `/bitnami/mariadb/data/mysql-bin` |
| `max_binlog_size` | 1073741824 |
| `SHOW BINARY LOGS` total | 약 209.25 GiB |
| actual file total | 약 209.26 GiB |
| oldest file mtime | 2026-06-24T08:11:16Z |
| newest file mtime | 2026-06-30T00:31:59Z |
| observed span | 약 5.68일 |

추정 회수량:

| Hypothetical keep window | Estimated reclaim |
| --- | ---: |
| 7일 | 0.00 GiB |
| 3일 | 약 180.86 GiB |
| 2일 | 약 194.86 GiB |

원인 후보:

- `mysql-bin.000771`~`mysql-bin.000775`가 2026-06-28 16:27~16:32 UTC 사이 빠르게 1 GiB 단위로 회전했다.
- 이는 상시 균일 write보다는 대량 적재/swap 류 batch write의 영향일 가능성이 높다.
- 단, binlog event level 분석은 이번 범위에서 read-only 확인만 했으므로 최종 원인은 추가 binlog sampling 또는 적재 작업 타임라인 대조가 필요하다.

권고:

- 이번 작업에서 binlog `PURGE` 또는 retention 변경은 하지 않았다.
- 7일 retention은 현재 span 기준 즉시 회수 효과가 없다.
- 2~3일 purge는 180~195 GiB 수준 회수 가능성이 있으나 PITR/백업 복구 여유를 크게 줄인다.
- PL이 PITR 요구 기간과 백업 정책을 결정한 뒤 별도 작업으로 실행해야 한다.

## 커밋 범위

이번 커밋 대상:

- `pipeline/scripts/deploy/strategic_stage_import.py`
- `pipeline/scripts/build_strategic_filter_dimension_metric.py`
- `tests/deploy/test_strategic_stage_import.py`
- `tests/scripts/test_build_strategic_filter_dimension_metric.py`
- `docs/GALERA_CLEANUP_REPORT.md`

제외:

- 운영 d1 stage DROP.
- binlog PURGE/retention 변경.
- unrelated dirty files, 특히 `jw-chat-agent-poc/**`.
