# JW Market 전달 문서

`docs/delivery/`는 JW Market 인수자가 시스템을 이해하고 운영할 때 사용하는 문서 정본이다. 특정 시점의 SHA·generation·image tag는 계약이 아니다. **코드 기준은 원격 `develop`, 실행체 기준은 Kubernetes live query**로 확인한다.

## 1. 먼저 읽을 문서

| 순서 | 문서 | 용도 |
|---:|---|---|
| 1 | [DOC-1 시스템 아키텍처](DOC-1_개발문서_시스템아키텍처.md) | 구성요소·데이터 흐름·인프라 경계 |
| 2 | [DOC-5 운영 문서](DOC-5_운영문서.md) | 정기 운영·인입·장애·모니터링 |
| 3 | [배포·승격·롤백 런북](RUNBOOK_배포_승격_롤백.md) | digest 조회부터 test2·운영·롤백까지 |
| 4 | [계정·권한 온보딩 런북](RUNBOOK_계정_권한_온보딩.md) | 신청·승인·발급·검증·회수 |
| 5 | [백업·복구 정책](POLICY_백업_복구.md) | 백업 범위·보존·복구·리허설 |

## 2. 문서 맵

| 문서 | 대상 독자 | 내용 |
|---|---|---|
| [DOC-1](DOC-1_개발문서_시스템아키텍처.md) | 개발·운영 | 전체 시스템과 배포 경계 |
| [DOC-1b](DOC-1b_개발문서_크롤_BA파이프라인.md) | 데이터·AI 개발 | 뉴스 크롤·브랜드 활동 파이프라인 |
| [DOC-1c](DOC-1c_개발문서_채팅에이전트.md) | 채팅 개발 | 채팅 에이전트·wf301·GenOS 연동 |
| [DOC-2](DOC-2_DB_스키마정의서.md) | DB·백엔드 개발 | mart·cache·catalog·ledger 스키마 |
| [DOC-2b](DOC-2b_DB_크롤_BA_테이블.md) | 데이터·AI 개발 | 크롤·브랜드 활동 테이블 |
| [DOC-3](DOC-3_API_명세서.md) | API 소비자 | 시장분석 백엔드 API 계약 |
| [DOC-3b](DOC-3b_API_채팅.md) | 채팅·포탈 개발 | 채팅 API 계약 |
| [DOC-4a](DOC-4a_사용설명서_시장분석.md) | 업무 사용자 | 시장분석 화면 사용법 |
| [DOC-4b](DOC-4b_사용설명서_jw-data-input.md) | 업로드 사용자 | 데이터 인입 포털 사용법 |
| [DOC-4c](DOC-4c_사용설명서_브랜드활동_데이터맥락.md) | 업무 사용자 | 브랜드 활동 데이터 맥락 |
| [DOC-4d](DOC-4d_사용설명서_채팅.md) | 채팅 사용자 | 질문 유형·파일 질답·한계 |
| [DOC-5](DOC-5_운영문서.md) | 운영자 | 정기 운영·증분 인입·장애 대응 |

## 3. 현재 좌표 확인

문서 머리의 오래된 캡처값을 복사하지 않는다.

```bash
# 코드 정본
git fetch jw-private develop
git rev-parse jw-private/develop

# backend/test2 실행체
kubectl config current-context
kubectl -n llmops get deploy jw-market-backend-api jw-market-backend-api-test \
  -o custom-columns='NAME:.metadata.name,GEN:.metadata.generation,IMAGE:.spec.template.spec.containers[?(@.name=="jw-market-backend-api")].image,READY:.status.readyReplicas'

# 포털 실행체
kubectl -n llmops get deploy jw-data-portal jw-data-portal-worker \
  -o custom-columns='NAME:.metadata.name,GEN:.metadata.generation,IMAGE:.spec.template.spec.containers[0].image,READY:.status.readyReplicas'

# 스케줄과 활성 여부
kubectl -n llmops get cronjobs -o wide
```

Pod 실제 digest는 Deployment image tag가 아니라 `status.containerStatuses[].imageID`로 확인한다.

## 4. 문서 유지 규칙

1. 사실에는 코드 경로, live 리소스명 또는 라운드 evidence를 붙인다.
2. SHA·generation·digest는 문서 본문에 "현재값"으로 고정하지 않는다. live 조회법을 기록한다.
3. 자격증명 값은 금지한다. Secret 리소스·키 이름만 기록한다.
4. 미확인 사항은 `[확인 필요]`로 남기고 추측으로 채우지 않는다.
5. API·DB·화면 계약이 바뀌면 관련 DOC와 사용설명서를 같은 변경에서 갱신한다.
6. 배포 증거는 문서가 아니라 라운드별 audit에 보존한다.

## 5. 전달 패키지 범위

JW 전달 zip에는 위 DOC 문서와 운영 런북·정책만 포함한다. 다음은 저장소에는 남아도 인수 패키지에서는 제외한다.

- 조사 evidence, audit 원문, 작업 큐
- 작성 과정의 open question·게이트 메모
- 중복·폐기 판정 문서와 버전별 임시 handoff

패키지의 `MANIFEST.sha256`과 `SOURCE_STATE.txt`로 파일 무결성과 원격 source SHA를 확인한다.
