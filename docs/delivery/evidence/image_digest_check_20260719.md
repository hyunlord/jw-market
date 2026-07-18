# evidence — 인입 훅 이미지 v0.2.5 deps·digest 실측 (2026-07-19)

- 방식: **read-only**. 라이브 `kubectl get/exec`(구동 pod 내 `python` import), `gcloud artifacts docker images describe`(태그→digest), 로컬 `git`. 이미지 빌드·push 0 · 클러스터 변경 0 · DB write 0.
- 계기: A5 반영 검증 중 문서 내부 모순 발견 — §3.6은 인입 훅 이미지를 `v0.2.5-51e2c687`로, §4는 `v0.2.4-e984a057`로 서술. A5는 `boto3 ABSENT`만 실측했고 **ETL load 의존성(pyarrow·duckdb)은 미확인** → R-0(ETL 실 load 블로커) 조용한 재발 여부를 확정한다.

## A. 런타임 deps 실측 — R-0 판정
구동 인입 훅 pod `jw-ingest-hook-d7756d6-rj895`(ns `llmops`, container `trigger`, image `jw-pipeline-orchestrator:v0.2.5-51e2c687`) 내부 `python` import:

| 모듈 | 결과 | 버전 | Dockerfile@51e2c687 대조 |
|---|---|---|---|
| **pyarrow** | **PRESENT** | 24.0.0 | 일치(`pyarrow==24.0.0`) |
| **duckdb** | **PRESENT** | 1.5.4 | 일치(`duckdb==1.5.4`) |
| **openpyxl** | **PRESENT** | 3.1.5 | 일치(`openpyxl>=3.1`) |
| boto3 | **ABSENT** | (ModuleNotFoundError) | 미설치(정합) — A5 교차확인 |
| typer | PRESENT | 0.27.0 | 일치(`typer>=0.12`) |
| requests | PRESENT | 2.34.2 | 일치(`requests>=2.31`) |

- 런타임: `python 3.11.15`, `/usr/local/bin/python`.
- **판정: R-0 유지(재발 아님).** ETL load 경로(`python -m pipeline.etl.run` → s1_load=pyarrow·s3_enrich=duckdb)의 의존성 정상. 런타임 버전이 Dockerfile 지정과 정확히 일치 → 배포 digest `a362ceb8`가 `51e2c687` 트리(또는 deps 동일 트리)에서 빌드됐음을 교차입증.

## B. digest · 계보
| 이미지 | 태그 | digest(레지스트리 실측) | 근거 |
|---|---|---|---|
| 인입 훅(현행) | `v0.2.5-51e2c687` | `sha256:a362ceb8a60f04688917d8c73dddf63c29e22b295c6e45f1c4036424240d703a` | 라이브 pod imageID = 레지스트리 describe = git manifest 3자 일치 |
| v0.2.4(직전) | `v0.2.4-e984a057` | `sha256:e79aa0986a4e2163849d97d4b3aafacd05fe4db7fc3db48dc36a96164b4c46d8` | 레지스트리 describe(= jw market 진술 `e79aa098` 일치) |
| v0.2.0(poll pin) | — | `sha256:6bffbc5350aadd302124c500eb91b16415b0dcfe42c635774fd0abd763441996` | poll/BA/csd manifest pin |

- **빌드 기준 커밋**: 태그 규약 `v<semver>-<commit>` → `51e2c687`. 커밋 `51e2c687`("docs(delivery): jw chat 3종 계보 갭 교정", 2026-07-18 17:28)은 `e984a057`(v0.2.4 "Add pyarrow+duckdb", 2026-07-18 11:23)의 **자손**(`git merge-base --is-ancestor` YES).
- **v0.2.4→v0.2.5 이미지-내용 델타**(git `e984a057..51e2c687`, 이미지 COPY 경로 `pipeline/`·`docs/crawl`·api requirements):
  - Dockerfile·requirements **무변경** → deps(pyarrow/duckdb/openpyxl) v0.2.4와 동일(위 A로 실측 확인).
  - `pipeline/scripts/ingest_hook/` 만 변경: `job_runner.py`+93, **`load_verify.py` 신규**+73, `config.py`+30, `category_map.py`+24, `job_launcher.py`+2, `README.md`+3 (208 ins/17 del). = ingest_hook **load/verify 배선**("J5 load"). 즉 v0.2.5 = v0.2.4 deps + 훅 적재/검증 코드.
- **[확인 필요]**: v0.2.5 이미지의 **빌드/push 실행 주체·인가**. 계보·코드 델타·deps는 실측 확정되나, 누가 빌드해 `a362ceb8`로 push했는지, jw market "base=v0.2.4 고정" 진술과의 관계는 미확인 → `_UPDATE_QUEUE.md` B9.

## C. 참조처 어긋남(git manifest develop `f4c51075` vs 라이브 `llmops`)
| 리소스 | git manifest | 라이브 | 판정 |
|---|---|---|---|
| `jw-ingest-hook` Deployment | `@sha256:a362ceb8`(ingest-trigger-deployment.yaml) | 태그 `:v0.2.5-51e2c687`→`a362ceb8` | **digest 일치**; ⚠형식차: git=불변 digest, 라이브=가변 태그 |
| `jw-ingest-sweep-daily` CronJob(suspend) | `@sha256:a362ceb8`(ingest-sweep-cronjob.yaml, v0.2.5) | 태그 `:v0.2.4-e984a057`→pod `fea29685` | **드리프트**: 라이브 v0.2.4 / git v0.2.5(정렬은 별건·과도기 예비) |
| ingest-job-template(reference/) | `@sha256:a362ceb8` | (템플릿·미배포) | 훅 digest와 일치 |
| poll-daily CronJob(suspend) | `@sha256:6bffbc53` | `@sha256:6bffbc53` | 일치(v0.2.0) |
| brand-activity-run CronJob(suspend) | `@sha256:6bffbc53` | `@sha256:6bffbc53` | 일치 |
| csd-sensor CronJob(suspend) | `@sha256:6bffbc53` | `@sha256:6bffbc53` | 일치 |

- **가변 태그 드리프트 실증**: `:v0.2.4-e984a057` 태그가 레지스트리 `e79aa098` vs 구동 sweep pod imageID `fea29685`로 **동일 태그·두 digest**. 태그 재push 이력 존재. 라이브 훅 Deployment도 가변 태그(`:v0.2.5-51e2c687`)를 쓰므로(git manifest는 불변 digest) 향후 태그 재push 시 pod 재기동에서 조용히 바뀔 수 있음 → digest 동기 프로토콜 필요(통지 §②).
- sweep pod `jw-ingest-sweep-daily-29738610-nnhj8`: 0/2 Error(exit 1) — 마지막 로그 `urllib.error.HTTPError: HTTP Error 409: Conflict`(**deps 오류 아님**, 애플리케이션 충돌). R-0 판정과 무관. suspend 예비이므로 진단은 본 라운드 범위 밖.

## D. 문서 정합
- DOC-1b §3.6은 이미 `v0.2.5-51e2c687`/`boto3 ABSENT` 기재(정확). §4·footnote는 `v0.2.4-e984a057`(정합 대상) → 본 라운드에서 §4 row·note·footnote를 `v0.2.5-51e2c687`(a362ceb8)로 정합, 버전 변천 각주 추가, 빌드 주체 [확인 필요]5·B9 등재.
