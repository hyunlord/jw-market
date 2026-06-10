# 260518 Correctness Rebuild 배포 Runbook

작성일: 2026-06-11

이 문서는 Stage 2 방식 (2), 즉 검증된 로컬 cache를 GCP Galera 운영 DB에
logical dump/load로 반영하는 절차를 기록한다. 최종 목표는 GCP에서
`run_market_pipeline.sh --all`로 직접 재빌드하는 B4지만, 이번 배포는 이미
검증된 로컬 staging 결과를 빠르게 반영하는 중간 단계다.

## 배포 대상

대상 테이블은 세 개뿐이다.

- `cache_cause`
- `cache_market_status`
- `cache_brands`

다음 테이블은 절대 건드리지 않는다.

- `cache_deep_analysis_ai_analysis`
- 로컬 `cache_deep_analysis`

이 테이블들은 Agent2 산출물 lifecycle에 속한다. 원인분석 cache rebuild와 함께
전송하거나 swap하면 Agent2 검증 상태를 깨뜨릴 수 있어 제외한다.

## 왜 logical dump/load인가

Galera는 큰 CTAS나 단일 transaction에서 writeset size 한계에 걸릴 수 있다.
Stage 1에서 검증한 staging 결과는 이미 완성된 cache이므로, 운영에서 다시
계산하지 않고 logical dump를 staging table에 적재한 뒤 atomic RENAME만 수행한다.

기각한 대안:

- 운영 full rebuild: B4의 최종 목표지만, 이번 배포에는 시간이 길고 write surface가 넓다.
- CTAS: Galera writeset 한계 때문에 제외한다.
- cache 전체 전송: `cache_deep_analysis_ai_analysis` 보호 원칙과 충돌한다.

## 전체 순서

1. 로컬 checkpoint commit을 만든다. push는 PL 승인 전까지 하지 않는다.
2. GCP Galera에 SELECT와 scratch CREATE/DROP이 되는지 확인한다.
3. strategy_001/016의 옛 period diff 잔가닥을 값 단위로 확인한다.
4. 로컬에서 `cache_cause`, `cache_market_status`, `cache_brands`만 blue-green swap한다.
5. 검증된 로컬 staging 세 테이블을 logical dump로 만든다.
6. 2-hop SSH로 GCP에 전송하고, GCP staging table에 load한다.
7. 운영 live 세 테이블을 `*_old_<ts>`로 백업한다.
8. 여기서 STOP하고 PL 승인을 받는다.
9. 승인 후 `cache_*_staging`을 live 이름으로 atomic RENAME한다.
10. 문제가 있으면 reverse RENAME으로 즉시 롤백한다.

## GCP 접근

접근은 bastion 2-hop이다.

1. `kube@192.168.81.177`
2. `GCP@34.47.113.232`

비밀번호와 key passphrase는 로그에 남기지 않는다. 운영 명령 로그는 실행한 SQL
종류, row count, digest만 남기고 credential 값은 남기지 않는다.

## Local Blue-Green

로컬 swap은 가역이다. 예시는 timestamp가 `20260611_XXXXXX`일 때다.

```sql
RENAME TABLE
  cache_cause TO cache_cause_old_20260611_XXXXXX,
  jw_mart_stage1_20260611_015318.cache_cause TO cache_cause;
```

실제 실행 시 세 테이블 모두 같은 timestamp를 쓴다. 롤백은 반대 방향 RENAME이다.

```sql
RENAME TABLE
  cache_cause TO cache_cause_failed_20260611_XXXXXX,
  cache_cause_old_20260611_XXXXXX TO cache_cause;
```

## GCP Blue-Green

GCP에서는 먼저 staging table을 만든다.

```sql
CREATE TABLE cache_cause_staging LIKE cache_cause;
CREATE TABLE cache_market_status_staging LIKE cache_market_status;
CREATE TABLE cache_brands_staging LIKE cache_brands;
```

logical dump를 load한 뒤, row count와 canonical digest가 로컬 staging과 같은지
확인한다. 운영 swap은 PL 승인 후에만 실행한다.

```sql
RENAME TABLE
  cache_cause TO cache_cause_old_<ts>,
  cache_cause_staging TO cache_cause;
```

롤백:

```sql
RENAME TABLE
  cache_cause TO cache_cause_failed_<ts>,
  cache_cause_old_<ts> TO cache_cause;
```

`cache_market_status`, `cache_brands`도 같은 패턴을 쓴다.

## 검증 체크리스트

- 세 cache table row count와 digest가 로컬 staging과 일치한다.
- `cache_deep_analysis_ai_analysis` row count와 digest가 전후 동일하다.
- 운영 API에서 리바로/리바로젯 Class/Molecule, 제이클 molecule, IQVIA 제형/strength,
  B1/B2 ranking/level trend 계약이 반영된다.
- backend image 재배포 없이 cache swap만으로 served data가 바뀐다.

## Stage 2 전용 스크립트

`pipeline/scripts/deploy_cache_stage2_way2.sh`는 cache 3종만 다룬다. 스크립트에는
`cache_deep_analysis*`가 table 배열에 들어오면 실패하는 guard가 있다. 운영 swap
SQL 출력은 PL 승인 전 검토용이며, 승인 없이 실행하지 않는다.
