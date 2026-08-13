# MI Master 셀프서비스 가이드 v1.4

MI Master는 NFS 데이터가 아니라 저장소와 이미지에 포함되는 시장 정의 자산이다.
일반뷰는 ATC4 기준이고, 이 절차의 대상은 `market_landscape`와
`competitive_dynamics` 전략뷰다. 시장 추가는 워크북 맨 뒤에서만 한다.

## 1. 새 시장 작성

1. `시장정의 & Target` 시트의 마지막 시장 오른쪽에 새 열을 추가한다.
2. 6행에 대상 제품명, 7행에 ATC 코드, 10행에 `UBIST`, `IQVIA` 또는 둘 다를
   적는다.
3. 같은 이름의 상세 시트를 만들고 3~12행 중 한 행에 표 헤더를 둔다.
4. 분석할 축은 14~19행에 값을 적고, 사용하지 않을 축은 비운다.
5. 경쟁 시장을 부모 ML과 같게 쓸 때는 48~50행을 모두 비운다.
6. 경쟁 시장을 ATC4로 좁힐 때만 48~50행에 `A06B1`,
   `A06B1 + A06B2`처럼 ATC4 코드만 적는다.

> [!IMPORTANT]
> 48~50행이 모두 비어 있으면 CD는 부모 ML 구성원 전체를 사용한다. 임의 설명문,
> ATC3, Class, 급여, 제형 등의 복합 조건은 자동 해석하지 않고 오류로 중단한다.
> 이 조건들은 아직 개발자 선언이 필요하다.

### 상세 시트 헤더 사전

헤더 행은 3~12행에서 찾는다. 같은 행에 `ATC`가 들어간 열 하나와
`MOLECULE`, `성분`, `PRODUCT`, `제품` 중 하나가 들어간 열 하나가 있어야 한다.

| 축 | 정의 시트 행 | 권장 열 이름 | 현재 인식되는 다른 표기 |
| --- | ---: | --- | --- |
| 필수 ATC | 필수 | `ATC4` | 열 이름에 `ATC` 포함 |
| 필수 제품 | 필수 | `PRODUCT NAME KOR` | `제품` |
| Brand 선언 | 16 | `PRODUCT NAME KOR` | `제품` (현재 분석축 비활성) |
| Class | 14 | `Class` | `Class Recode`, `Class Recode 1`, `Class Recode 2`, `Class Recode 분류2`, `Dosage Form`, `Recode Class(성분)` |
| Molecule | 15 | `MOLECULE DESC` | `Molecule`, `Molecule Recode`, `성분`, `성분 Recode` |
| 제형 | 17 | `Dosage Form` | `Recode 제형`, `제형 Recode`, `투여 경로` |
| 함량·포장 | 18 | `PACK DESC` | `Strength`, `Strength2`, `규격 Recode`, `성분용량` |
| 기타 | 19 | `NHI TYPE` | `Fish oil 여부`, `Ox/Gx`, `Ox/Gx(바이오시밀러)` |

코드는 앞뒤 공백만 제거한 뒤 대소문자를 구분해 먼저 정확 일치로 찾고, 없으면
헤더가 선언 문자열로 시작하는 첫 번째 열을 사용한다. 전각/반각이나 대소문자를
자동 통일하지 않는다. 비슷한 열이 여러 개면 시트의 왼쪽 열이 먼저 선택되므로
권장 열 이름을 그대로 쓰는 것이 안전하다.

백지에서 시작하는 최소 예시는 다음과 같다.

```text
ATC4 | PRODUCT NAME KOR
A06B1 | 예시브랜드
```

Molecule 분석이 필요하면 `MOLECULE DESC`, Class 분석이 필요하면 `Class`를
추가한다. 분석 축을 정의 시트에서 켰는데 상세 시트 열을 인식하지 못하는 경우는
오류로 취급해야 한다. 현재 워크북의 16개 시트는 16행 Brand를 모두 선언하지만
Brand는 분석 축으로 활성화되지 않는다. 이 기존 동작은 화면 변경 위험 때문에
이번 버전에서 자동 오류로 바꾸지 않았으며 별도 계약 정리가 필요하다.

## 2. CD 범위

| 48~50행 | 결과 | 코드 작업 |
| --- | --- | --- |
| 모두 비움 | 부모 ML과 동일한 CD | 없음 |
| ATC4 코드만 입력 | 입력한 ATC4로 CD를 좁힘 | 없음 |
| 설명문·ATC3·Class·급여·제형·복합식 | 오류로 중단 | 필터 선언 필요 |

기존 19개 CD의 업무 필터는 그대로 유지된다. 기존 필터 중 부모 ML과 같은 것은
`cd_004`, `cd_006`, `cd_007`, `cd_014`, `cd_016`, `cd_017` 여섯 개다.
이들도 향후 빈 48~50행 기본값으로 옮길 수 있지만 이번 변경에서는 건드리지 않았다.

## 3. 저장소 반영

워크북과 코드 변경을 Gitea의 승인된 브랜치에 커밋한다. MI Master 파일명은 현재
여러 모듈의 버전 검증 상수와 연결돼 있으므로 파일명은 임의로 바꾸지 않는다.
파일명 독립화는 별도 계약 변경이다.

변경 전 다음 검증을 실행한다.

```bash
PYTHONPATH=.:pipeline/scripts/etl python3 -m pytest -q \
  tests/etl/test_mi_master_selfservice.py
```

## 4. 이미지 빌드와 DEV 배포

실측 배포 좌표는 Artifact Registry의 `stg` 프로젝트와 DEV GKE 클러스터 조합이다.
레지스트리는
`asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01`,
워크로드는 `llmops/jw-ingest-hook`이다. 다음 블록은 commit, tag, digest,
resourceVersion을 자동 산출한다. 실행 승인 회차에서만 사용한다.

```bash
set -euo pipefail

CTX='gke_prj-jw-agn-dev-ai-490605_asia-northeast3_kcl-jw-agn-dev-genos'
NS='llmops'
DEPLOY='jw-ingest-hook'
REGISTRY='asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01'
COMMIT="$(git rev-parse HEAD)"
TAG="mi-master-${COMMIT:0:8}-$(date -u +%Y%m%d%H%M%S)"
IMAGE="${REGISTRY}/jw-pipeline-orchestrator:${TAG}"

test -z "$(git status --porcelain)"
docker build --platform linux/amd64 \
  -f deploy/docker/pipeline-orchestrator.Dockerfile \
  --build-arg "APP_VERSION=${COMMIT}" \
  -t "${IMAGE}" .
docker push "${IMAGE}"
DIGEST="$(gcloud artifacts docker images describe "${IMAGE}" \
  --format='value(image_summary.digest)')"
IMAGE_REF="${REGISTRY}/jw-pipeline-orchestrator@${DIGEST}"
RV="$(kubectl --context "${CTX}" -n "${NS}" get deploy "${DEPLOY}" \
  -o jsonpath='{.metadata.resourceVersion}')"
PATCH="$(jq -nc \
  --arg rv "${RV}" --arg image "${IMAGE_REF}" --arg app "${COMMIT}" \
  '{metadata:{resourceVersion:$rv},spec:{template:{spec:{containers:[{
    name:"trigger",image:$image,env:[
      {name:"APP_VERSION",value:$app},
      {name:"INGEST_JOB_IMAGE",value:$image}
    ]
  }]}}}}')"
kubectl --context "${CTX}" -n "${NS}" patch deploy "${DEPLOY}" \
  --type=strategic -p "${PATCH}"
kubectl --context "${CTX}" -n "${NS}" rollout status deploy/"${DEPLOY}"
kubectl --context "${CTX}" -n "${NS}" get deploy "${DEPLOY}" -o json \
  | jq -r '[.status.readyReplicas,
            .spec.template.spec.containers[0].image,
            ([.spec.template.spec.containers[0].env[]
              | select(.name=="APP_VERSION") | .value][0])] | @tsv'
```

성공 판단 출력은 `1`, 새 `sha256:` digest, 현재 commit SHA가 한 줄에 나오는 것이다.
이 문서 작성 회차에서는 빌드·push·배포를 실행하지 않았다.

## 5. catalog와 mart 반영

배포만으로 catalog DB는 바뀌지 않는다. `sync_catalog_tables`는 현재
`--sync-catalog-db`를 명시한 경로에서만 DB를 갱신한다. 라이브 계보의 기존
`정의·로직 재반영`은 기존 raw를 이용한 mart-only 경로이며, 변경된 MI Master를
표준 승인 게이트로 승격하는 경로로 인정하지 않는다.

> [!WARNING]
> catalog 기본 동기화와 업로드/재반영 경로 통합 변경이 배포되기 전에는 어느
> 버튼도 MI Master 셀프서비스 완료 경로가 아니다. 통합 변경이 배포된 뒤에는
> `인입 포털 > 진행 현황`에서 대상 소스를 선택해 `정의·로직 재반영` 또는 기존
> 파일 업로드 중 하나를 1회 실행하고, 승인 대기와 publish가 끝날 때까지 중단하지
> 않는다. 그 전에는 PL 승인 실행 회차를 사용한다.

예상 시간은 UBIST 약 6시간, IQVIA NSA 약 2시간이다. 실행 직후에는 다음 SQL을
읽기 전용으로 확인한다. `ingested_at`은 실행 당일이어야 하고 두 테이블의
`source_file_version`은 배포된 MI Master와 같아야 한다.

```sql
SELECT source_file_version, COUNT(*) AS rows, MAX(ingested_at) AS ingested_at
FROM catalog_ml_market
GROUP BY source_file_version
ORDER BY source_file_version;

SELECT source_file_version, COUNT(*) AS rows, MAX(ingested_at) AS ingested_at
FROM catalog_cd_market
GROUP BY source_file_version
ORDER BY source_file_version;
```

## 6. MI팀 확인

1. 원인분석의 Market Landscape에서 한 시트의 브랜드가 같은 시장으로 묶였는지
   전건 확인한다.
2. 변경한 시장의 ATC4만 값 계산에 쓰였는지 확인한다.
3. 추가·삭제·수정한 브랜드의 노출이 정의대로 바뀌었는지 확인한다.
4. 변경하지 않은 시장과 일반뷰가 그대로인지 확인한다.

## 7. 막혔을 때

| 증상 | 확인 |
| --- | --- |
| `unrecognized direct competition declaration` | 48~50행을 비우거나 ATC4 코드만 적었는지 확인 |
| 분석 축이 비어 있음 | 위 헤더 사전의 정확한 문자열과 14~19행 선언을 확인 |
| MI Master SHA 불일치 | 빌드에 포함된 워크북과 catalog manifest의 SHA를 대조 |
| 시장 ID가 이동함 | 새 시장을 중간에 삽입하지 않았는지 확인 |
| source type 오류 | 10행을 `UBIST`, `IQVIA` 또는 둘 다로 선언했는지 확인 |
| catalog 날짜가 갱신되지 않음 | 배포만 하고 적재/재반영을 실행하지 않았는지 확인 |

시장 삭제 정책과 시트 순서 변경 정책은 아직 PL 확정 전이다. 시트를 삭제하거나
기존 시트 사이에 삽입하지 않는다.
