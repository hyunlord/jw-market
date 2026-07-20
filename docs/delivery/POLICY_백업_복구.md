# 백업·복구 정책

## 1. 목적과 범위

장애·오배포·오적재·권한 사고 후 JW Market을 검증 가능한 정상 세대로 복구한다. Galera 복제와 Deployment replica는 가용성 수단이지 백업이 아니다.

대상:

- GitHub/Gitea 코드와 릴리즈 provenance
- MariaDB mart 세대, promotion ledger, `__old_<run_id>` 백업 테이블
- cache blue-green 세대와 동적 캐시 무효화 대상
- jw-data-portal 업로드 원본·manifest·상태 데이터
- Kubernetes manifest와 비밀 리소스의 복구 절차(Secret 값 자체는 별도 보안 백업)

## 2. 정본과 보호 규칙

- 현재 서빙 DB는 이름으로 판단하지 않는다. backend Deployment의 `DB_NAME` live env와 promotion ledger가 정본이다.
- `stage`, `bak`, `old` 같은 이름 패턴으로 삭제 후보를 고르지 않는다.
- DB 행수는 `COUNT(*)`로만 검증한다.
- 백업 테이블은 존재뿐 아니라 1행 이상, 기대 행수, digest가 일치해야 유효하다.
- general·strategic·fdm·cache invalidation을 한 promotion 세트로 취급한다. 부분 복구로 세대를 섞지 않는다.

## 3. 백업 종류

| 대상 | 방식 | 검증 | 보존 기준 |
|---|---|---|---|
| 코드 | 원격 Git 저장소 + immutable commit/tag | remote SHA와 로컬 SHA 일치 | 보호 브랜치 정책 |
| Gitea | 운영 CronJob dump | Job 성공, 산출물 크기, 복원 리허설 | 플랫폼 정책 |
| mart | 격리 generation DB + promotion 전 `__old_<run_id>` | ledger, `COUNT(*)`, digest | 현 서빙 포함 최근 2세대 |
| backup tables | atomic promotion이 남긴 run별 백업 | component 완전성, 비어 있지 않음 | 최근 3 run_id |
| cache | staging → validate → atomic rename | canonical hash, row count, source epoch | 직전 정상 세대 |
| 업로드 원본 | NFS/오브젝트 스토리지의 원본 + manifest | object checksum, manifest 참조 | 업무·개인정보 정책 |
| Secret | 승인된 비밀 저장소의 버전/회전 정책 | 복호화 없이 복구 리허설 | 보안 정책 |

보존 수치는 코드의 기본 정책이며, 실제 삭제는 별도 PL 승인 전 자동 수행하지 않는다.

## 4. live 백업 상태 조회

고정 스케줄을 문서에서 신뢰하지 않고 실행 시 조회한다.

```bash
kubectl config current-context
kubectl -n llmops get cronjobs -o wide
kubectl -n llmops get jobs --sort-by=.metadata.creationTimestamp
kubectl -n llmops get cronjob jw-gitea-dump-daily -o yaml
```

mart 세대와 rollback 후보는 서빙 DB를 명시해 dry-run으로 확인한다.

```bash
python3 -m pipeline.scripts.rollback retention --list \
  --target-db "$DB_NAME" --keep-generations 2 --keep-backup-runs 3

python3 -m pipeline.scripts.rollback --to latest-good --dry-run \
  --target-db "$DB_NAME" --actor "$USER" --reason '<ticket>'
```

`retention --apply`와 rollback `--yes`는 이 조회 단계에서 금지한다.

## 5. 복구 의사결정

| 사고 | 우선 복구 |
|---|---|
| 새 이미지의 코드 결함 | 직전 immutable image digest + APP_VERSION |
| promotion 이후 데이터 결함 | ledger의 직전 complete/good promotion 세트 |
| 캐시만 오염 | 정본 데이터 확인 후 blue-green 재생성 또는 승인된 무효화 |
| 업로드 원본 유실 | 원본 백업 + checksum + manifest 복원 |
| 저장소 손상 | 원격/덤프에서 새 저장소로 복원 후 commit graph 검증 |
| 자격증명 유출 | 즉시 회수·회전 후 영향 workload 재배포 |

코드와 데이터가 함께 바뀐 릴리즈는 호환성 표를 먼저 확인하고, 이미지와 데이터의 복구 순서를 사건 책임자가 승인한다.

## 6. mart 복구 실행

1. 쓰기 창을 동결하고 backend `DB_NAME`, image digest, ledger 최신 세대를 기록한다.
2. `--dry-run` 결과에서 required component 전체, backup row count, digest, 대상 serving DB를 검토한다.
3. PL/DBA 승인 뒤에만 `--yes`를 사용한다.
4. CLI가 atomic rename, 동적 캐시 무효화, 사후 row count/digest, rollback ledger 기록을 완료해야 한다.
5. 복구 후 canonical API 골든과 독립 SQL 정합을 실행한다.

```bash
python3 -m pipeline.scripts.rollback --to <promotion-run-id> --yes \
  --target-db "$DB_NAME" --actor "$USER" --reason '<incident-id>'
```

CLI가 0행 백업, 누락 component, 다른 serving DB, scratch table 충돌을 보고하면 우회하지 않는다.

## 7. 복구 검증

필수 증거:

- 모든 live table의 `COUNT(*)`와 기대 행수
- 표본이 아닌 계약 대상 전체 component의 digest/epoch
- `Σ(부분)=전체` 게이트와 교차 소스 불변 게이트
- `/api/health`, 중앙 release acceptance, strict log 0
- 전체 pod imageID와 restart 수
- rollback event의 actor, reason, target run, 시각

## 8. 리허설과 점검 주기

- 분기 1회 격리 DB에서 mart rollback 왕복을 수행한다.
- 반기 1회 Gitea dump와 업로드 원본의 복원 리허설을 수행한다.
- 매월 backup Job 성공과 최근 산출물 무결성을 확인한다.
- 리허설은 운영 DB에 RENAME/DROP/write하지 않는다.
- RPO/RTO 목표는 서비스 소유자와 플랫폼팀이 승인해 사건 대응 문서에 별도로 기록한다. 미승인 수치를 이 문서가 보장하지 않는다.

## 9. 금지

- 서빙 DB를 이름 패턴으로 추정하거나 삭제 후보로 삼기
- 검증 없는 backup table을 정상으로 간주하기
- 라이브 테이블 직접 덮어쓰기
- 운영 캐시를 관측 목적으로 삭제하기
- Secret 값을 백업 보고서나 Git에 포함하기
- 승인 없는 `retention --apply`, rollback `--yes`, RENAME, DROP
