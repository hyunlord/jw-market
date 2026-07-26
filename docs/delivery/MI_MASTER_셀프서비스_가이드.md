# MI Master 셀프서비스 가이드

이 문서는 MI Master에서 전략 시장을 추가하거나 기존 시장 정의를 바꿀 때
어디까지 파일 수정만으로 반영되는지 설명합니다. 일반뷰는 계속 ATC4 기준이며,
이 절차는 `market_landscape`와 `competitive_dynamics` 전략뷰에만 적용됩니다.

## 1. 새 시장 추가

1. 기존 전략 시장 시트를 복사해 새 상세 시트를 만듭니다.
2. `시장정의 & Target` 시트에 새 열을 추가합니다.
3. 새 열의 6행에 대상 제품명, 7행에 ATC 코드, 10행에 데이터 소스를 입력합니다.
4. 분석 축을 사용할 경우 14~19행의 해당 항목을 채웁니다.
5. 경쟁시장 정의와 고객 우선순위가 있으면 48~50행과 54~57행을 채웁니다.
6. 상세 시트에는 3~12행 사이에 ATC 열과 성분 또는 제품 열을 포함한 헤더가
   있어야 합니다.

저장 후 catalog를 실행하면 다음 항목은 자동 생성됩니다.

- `strategy_NNN`, `ml_NNN`, `cd_NNN`, `cdf_NNN`
- Market Landscape 시장 목록과 분석 축
- Competitive Dynamics 기본 시장
- API 브랜드 목록과 시장 메타데이터
- JW 대상 제품 목록

테스트용 17번째 시장은 원본 파일을 복사한 임시 workbook에서 검증하며, 실제
MI Master 원본은 수정하지 않습니다.

## 2. 기존 시장 정의 변경

MI Master의 recode 열을 바꾸고 catalog를 다시 실행하면 다음 정책으로 반영됩니다.

| 정책 | 대상 예 | 동작 |
| --- | --- | --- |
| OVERWRITE | molecule, dosage_form, nhi_type, manufacturer | canonical 값이 있으면 기존 값을 교체 |
| ADD_ONLY | class 등 분류 축 | 기존 값을 지우지 않고 선언된 분류를 추가 |

변경 후에는 대상 시장의 catalog 행과 API 응답 골든을 함께 확인해야 합니다.
시장 구성원이 의도치 않게 늘거나 줄면 반영하지 않습니다.

## 3. 브랜드 예외 규칙 선언

코드의 개별 `if` 문 대신
`pipeline/etl/config/mi_master_rules.yaml`의 `record_rules`에 규칙을 추가합니다.

```yaml
record_rules:
  - id: example_rule
    stage: strategic_brand_fields
    match:
      sheet_name: "예시 시장"
      molecule: "EXAMPLE"
    actions:
      set:
        class: "Example Class"
    reason: "업무 근거를 한 문장으로 기록"
```

지원하는 action은 다음과 같습니다.

- `set`: 지정 값을 덮어씁니다.
- `copy`: `raw.FIELD` 또는 `record.field` 값을 복사합니다.
- `first_present`: 후보 중 첫 번째 비어 있지 않은 값을 사용합니다.
- `null_if_equal`: 두 축이 같은 값을 중복 노출하면 첫 번째 축을 null로 만듭니다.

모든 규칙에는 고유한 `id`, 적용 단계인 `stage`, 선택 조건인 `match`, 업무
근거인 `reason`을 작성합니다. 제이클의 Trisulfate 처리와 가드렛의
TIRZEPATIDE → GLP-1RA 처리가 이 형식의 기준 사례입니다.

## 4. 자동 처리되지 않는 결정

다음 항목은 데이터만 보고 안전하게 추론할 수 없으므로 선언 파일 검토가
필요합니다. Python 코드 수정은 필요하지 않지만 개발자 또는 데이터 담당자의
검토 없이 임의로 정하지 않습니다.

- 여러 제품 열을 하나의 CD 시장으로 합치는 `cd_collapses`
- 과거 ID 순서를 유지해야 하는 `strategy_order`
- 분석 축의 업무상 예외인 `analysis_axis_overrides`
- 기존 적재 계약과 화면용 데이터 소스가 다른 `catalog_source_type_overrides`
- 제품명 표기와 alias를 바꾸는 target/JW product override
- 기존 CD 시장의 특수 필터 의미

새 시장의 CD 정의가 단순한 “상세 시트 전체”가 아니라 별도 필터를 요구하면
필터 선언과 계약 테스트를 함께 추가해야 합니다.

## 5. 반영 전 확인

```bash
PYTHONPATH=.:pipeline/scripts/etl python3 -m pytest -q \
  tests/etl/test_mi_master_selfservice.py \
  tests/api/test_api_response_golden_v2.py
```

필수 확인 항목:

- 임시 신규 시장이 registry, catalog, API 목록에 모두 나타나는가
- 제이클과 가드렛 API 응답이 기존 골든과 동일한가
- 기존 일반뷰와 전략뷰 응답이 바뀌지 않았는가
- 가드렛 TIRZEPATIDE 원천 행과 MOUNJARO 제품은 존재하며, 중복 영문
  molecule placeholder만 제외되고 한국어 canonical 브랜드 `마운자로`가
  유지되는가
- 전체 `tests/` 회귀의 신규 실패가 0건인가

## 6. 담당 경계

| 작업 | MI Master 또는 선언 파일만으로 가능 | 개발자 확인 |
| --- | --- | --- |
| 표준 형식의 신규 전략 시장 추가 | 예 | 최초 검증 권장 |
| 기존 recode 값 변경 | 예 | 시장 구성 변화 시 필요 |
| 선언 엔진이 지원하는 브랜드 예외 추가 | 예 | 골든 갱신 검토 필요 |
| CD 단순 전체시장 추가 | 예 | 최초 검증 권장 |
| 새로운 필터 연산자 또는 계산식 추가 | 아니오 | 필요 |
| 일반뷰 ATC4 계약 변경 | 아니오 | 필요 |
| API 응답 필드 변경 | 아니오 | 필요 |

운영 반영은 이 파일 변경과 별개입니다. catalog 재생성, mart 검증, 캐시 처리,
test2/운영 승격은 해당 배포 절차의 승인을 따라야 합니다.
