# 납품 문서 공통 기준 (실측, 2026-07-17)

## ★ 09:54 UTC 갱신 (문서 기준 SHA 전진: 761b4def → 7ca98403)
- develop 전진 12커밋(761b4def..7ca98403): 전부 ingest_hook/·deploy/k8s/{ingest-hook,crawler}/·RUNBOOK_MONTHLY.md·tests 영역.
  pipeline/scripts/api/·pipeline/etl/ 무변경 → API·DB·시장분석 서술은 761b4def 검증 그대로 유효.
- ★ingest hook 활성화됨(k8s_ingest_active.txt): deployment `jw-ingest-hook`(1/1, 이미지 jw-pipeline-orchestrator@sha256:fea29685…),
  svc `jw-ingest-hook`(8080), CronJob `jw-ingest-sweep-daily`(30 19 * * *, suspend=False).
  env: MinIO=http://minio.llmops.svc.cluster.local:9000, MARIADB_DATABASE=jw_mart_d2_stage_20260630_r2,
  ★INGEST_REHEARSAL_ROOT=/tmp/ingest-rehearsal 설정 = job_runner 격리(리허설) 모드(config.py 계약).
- ★ingest_ledger 운영 DB 생성됨: 실 DDL 캡처 완료, 행수 3(AUTO_INCREMENT=8). "미생성" 서술 전부 폐기.
- ★사이트 재배포: jw-data-portal(+worker) v0.5.2 → `v0.6.0-8ca9d98`(문서 기준 Gitea HEAD와 동일 커밋).
  STORAGE_PROVIDER=local 유지 + MINIO_*·INGEST_HOOK_TRIGGER/STATUS_URL(secretRef) env 추가(dataportal_env_v060.txt).
- crawl 클러스터 상태 불변(tier1/tier2 active·canonical 2종 suspend=True). 상류 30763e9c가 cutover 실행 기록·canonical 강등(demote).
- 신규 코드: ingest_hook/s3_input.py(S3 제출 읽기)·sigma_market.py(Σ brand raw_value == market_size_series 게이트).

## 문서 머리(모든 문서 공통)
- 기준 develop SHA: `761b4def` (worktree /tmp/jwm-develop-docs)
- 운영 backend: GKE `llmops` ns, deployment `jw-market-backend-api` (HPA `jw-market-backend-api-hpa` min2/max8·memory 60% 타깃, 초기 캡처 5/5 → 이후 스케일아웃 8/8 — k8s_hpa.txt), generation **302**,
  APP_VERSION(운영 이미지 커밋) `ad782bc064ba03a45eaa4f1e301dbd75b8bf9a9e`
  (주의: 의뢰서 기준 2b38c507에서 이후 갱신됨. ad782bc0은 develop 761b4def의 조상이 아님 — 별도 latency 릴리즈 브랜치 빌드.
   release annotation: jw-market/release=f139-brand-activity-general-scope)
- 사이트(jw-data-input): 정본 = Gitea `jw-market/jw-data-input.git`, HEAD(feat/market-ingest-v21) `8ca9d9870b2a90b08ebae321c0d56971b1590bad`, main `bc7d6248`.
  (의뢰서의 36aa856b는 로컬/Gitea 전 이력에서 미발견 — [확인 필요]로 처리, 실측 SHA 기재)
  소스 워크트리: /tmp/site-head (web/ = Next.js 앱)
- 생성일: 2026-07-17, 문서 버전 v1.0

## 인프라 실측
- 클러스터: GKE, ns `llmops`(우리 소관 전부) / `portal`(SI: portal-front·portal-back) / cicd 등
- backend svc: `jw-market-backend-api-service` (ClusterIP:80 → 8000), test: `jw-market-backend-api-test`(+service)
- 참조 배포: `jw-market-backend-api-reference-cycle0119` (성능 검증용 임시)
- 구 API(레거시): `jw-market-api` (jw-market-analysis:v0.9.1) + `jw-market-api-service`
- 사이트: deployment `jw-data-portal` + `jw-data-portal-worker` (jw-data-portal:v0.5.2), svc `jw-data-portal-service`(80)
  - 배포 env: STORAGE_PROVIDER=local, UPLOAD_BASE_PATH=/nfs-root/autoIngestion, Weaviate dedup 활성,
    NEXTAUTH_URL=https://jwai-dev.jwhealthcare.com/jw-data-portal/api/auth
  - ★ market MinIO ingest env(트리거/상태 URL·MinIO)는 코드(d066a31)에만 있고 배포 env에는 없음 = 미활성
- DB: MariaDB Galera sts `galera-mariadb-galera`(3/3), svc `llmops-mariadb-service`/`galera-mariadb-galera`(3306)
  - 운영 DB_NAME(전 차원 공통): `jw_mart_d2_stage_20260630_r2`
  - 브랜드활동: `jw_brand_activity_stage`
  - DB user: llmops (비밀번호는 k8s secret `galera-mariadb-galera` — 값 기재 금지)
- MinIO: svc `minio`(9000)/`minio-console`(9090), ExternalName `llmops-minio-service`
- Gitea: deployment llmops-gitea-deployment, svc `llmops-gitea-service`(3000/22), org `jw-market` (jw-data-input.git, jw-market.git)
- 이미지 레지스트리(AR): `asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01`
- nodeSelector: knp-jw-agn-dev-genos-api-01 (backend)

## CronJob (llmops, 실측 — evidence/k8s_cron_svc.txt)
| 이름 | 스케줄 | SUSPEND |
|---|---|---|
| brand-activity-row-topic-monthly | 0 22 4 * * | False |
| brand-activity-topic-monthly | 0 19 4 * * | False |
| dynamic-market-cache-warm | 7,37 * * * * | False |
| dynamic-market-cache-warm-test2 | 7,37 * * * * | False |
| iqvia-general-sidecar-quarterly | 0 3 5 1,4,7,10 * (KST) | True |
| jw-agent3-refresh-daily | 0 21 * * * | False |
| jw-brand-activity-run | 0 0 30 2 * | True |
| jw-csd-sensor | */10 * * * * | True |
| jw-gitea-dump-daily | 40 19 * * * | False |
| jw-news-crawl-retention-daily | 0 19 * * * | True |
| jw-news-crawl-tier1-daily | 10 18 * * * | False |
| jw-news-crawl-tier1-daily-canonical | 10 18 * * * | True |
| jw-news-crawl-tier2-daily-slice | 40 18 * * * | False |
| jw-news-crawl-tier2-daily-slice-canonical | 40 18 * * * | True |
| jw-pipeline-orchestrator-poll-daily | 0 16 * * * | True |
| jw-cache-refresh-daily | 0 20 * * * | False |

## ingest hook
- 클러스터 미배포(deploy/cronjob/svc/job 어디에도 ingest 리소스 없음) = 전부 미활성, 활성화는 PL 게이트
- 코드: pipeline/scripts/ingest_hook/ (app.py 웹훅·상태 API, contract.py=manifest v2.1, ledger.py=ingest_ledger DDL)
- ingest_ledger 테이블: 운영 DB에 미생성(테이블 목록에 없음 — 활성화 시점 생성 계약)
- Job 이미지 기본값: jw-pipeline-orchestrator@sha256:6bffbc53...

## DB 스키마
- evidence/db_schema_dump.txt: jw_mart_d2_stage_20260630_r2 (91테이블) + jw_brand_activity_stage (7테이블) SHOW CREATE 전수 + 행수
- `_bak_*`/`_backup_*`/`_stage_*`/`_mig_stg_*`/`_old_*`/`__failed_*`/`_cutover_*` = 백업/작업용 (정본 아님으로 분류)
- ground truth: catalog_ml_market / catalog_cd_market / catalog_strategic_brand
- cache_cause·cache_deep_analysis 계열 = "제거 예정" 명기 대상

## 증거 파일 (이 디렉토리)
- k8s_llmops.txt, k8s_cron_svc.txt, k8s_portal_vmhome.txt, backend_deploy_env.txt, dataportal_env.txt, site_repo.txt, db_schema_dump.txt
- api_endpoints.md / api_captures.md (api-capture 에이전트 산출 — 완료 후 생성)
