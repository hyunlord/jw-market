# JW 시장분석 확장 구조 설명서

작성 기준: `701b3d63`, `b9d02269`, `8292c0dc` 포함 2026-07-26 소스
대상: JW 개발자, SI 인수인계 담당자, 데이터 파이프라인 운영자

## 1. 설계 원칙

현재 구조는 다음 세 가지 정본을 분리한다.

1. **시장 topology**: MI Master workbook
2. **사람 판단이 필요한 예외**: `pipeline/etl/config/mi_master_rules.yaml`
3. **원천별 분석 차원 계약**: `pipeline/contracts/dimension_registry.py`

Python 모듈에 시장 16개, CD 19개, 브랜드별 예외를 다시 배열로 적지 않는다.
새 시장과 새 차원을 추가할 때 먼저 정본 파일 또는 registry를 확장하고, 소비자는
그 선언을 읽게 한다.

절대 계약:

- 일반뷰는 ATC4 기반이다.
- Market Landscape와 Competitive Dynamics는 모두 전략뷰다.
- Competitive Dynamics는 Market Landscape를 `cd_filter`로 좁힌 범위다.
- `market_id`는 내부 연결 키이며 외부 요청·응답에 노출하지 않는다.
- 화면이 API가 확정한 브랜드 cohort를 다시 선별하지 않는다.

## 2. MI Master 자동 discovery

### 2.1 discovery 진입점

정본 진입점은 `pipeline/etl/mi_master_registry.py`의
`discover_mi_master_registry()`다.

처리 순서:

1. `시장정의 & Target` 시트 존재 확인
2. workbook의 모든 시트에서 3~12행 헤더 탐색
3. ATC 열과 molecule/제품 열이 있는 시트를 시장 상세 시트로 판정
4. `시장정의 & Target` 6행의 제품 열을 상세 시트와 매칭
5. 상세 시트마다 `strategy_NNN`, `ml_NNN` 생성
6. 제품 열과 collapse 규칙을 기준으로 `cd_NNN`, `cdf_NNN` 생성
7. 대상 브랜드, 원천, ATC4, 분석 축을 registry 객체로 반환
8. catalog, API metadata, 전략 mart가 같은 registry를 소비

주요 산출 타입:

```text
MiMasterRegistry
  market_sheets
  market_by_id
  market_definition_columns
  analyze_matrix
  cd_specs
  detail_sheets
  target_brands
```

### 2.2 제거된 16/19 고정 계약

리팩토링 전에는 “ML 시장 16개”, “CD 시장 19개”와 각 ID 목록이 여러 schema와
metadata 파일에 반복돼 있었다. 현재는 canonical workbook을 읽은 결과가 16/19인지
검증할 뿐, topology를 그 숫자로 제한하지 않는다.

다음 계약 테스트가 이 경계를 보호한다.

- `tests/etl/test_mi_master_selfservice.py`
  - 정본 workbook이 현재 16 ML/19 CD를 발견하는지 확인
  - `expected_row_counts.yaml`이 시장 수를 고정하지 않는지 확인
  - 임시 17번째 시트와 제품 열을 추가했을 때 코드 변경 없이 발견되는지 확인
  - 신규 시장이 catalog, API registry, metadata까지 전달되는지 확인
- `tests/api/test_dimension_registry_contract.py`
  - public catalog가 발견된 topology와 일치하는지 확인

자동 discovery는 “시장 수 제한 제거”이지 “어떤 시트든 무조건 허용”이 아니다.
헤더, 제품 열 매칭, 원천, 상세 시트 연결이 모호하면 fail-closed한다.

### 2.3 ID 안정성

ID는 발견 순서에서 생성된다. 따라서 시트 순서 변경이나 제품 열 재배치는 기존 ID를
바꿀 수 있다. 과거 순서를 유지해야 하는 예외는 `strategy_order`로 선언한다.

외부 클라이언트가 내부 ID에 의존하지 않도록 해야 한다. 공개 요청·응답은 브랜드,
시장 표시명, ATC 범위 등 업무 키를 사용한다.

## 3. override 선언 구조

정본 파일:

```text
pipeline/etl/config/mi_master_rules.yaml
```

### 3.1 topology 수준 규칙

| 규칙 | 용도 | 현재 사례 |
| --- | --- | --- |
| `strategy_order` | 기존 전략 ID 순서 보존 | 위너프 시트 위치 |
| `cd_collapses` | 여러 제품 열을 CD 하나로 결합 | 페린젝트·베노훼럼 |
| `cd_name_overrides` | CD 표시명 교정 | 위너프 |
| `analysis_axis_overrides` | 헤더만으로 추론할 수 없는 분석축 on/off | 리바로 계열, 악템라 |
| `catalog_source_type_overrides` | workbook 표기와 catalog 소비 원천 차이 | 가드렛, 엔커버 |
| `target_brand_name_overrides` | target 셀의 표시 브랜드 교정 | 위너프A+ |
| `target_brand_aliases` | 원천 alias 연결 | 가드렛·가드메트·위너프A+ |
| `jw_product_overrides` | JW 제품명·근거 메모 교정 | 위너프A+ |

이 규칙들은 데이터만 보고 안전하게 추론할 수 없는 업무 결정이다. 자동 discovery가
대체하지 않는다.

### 3.2 레코드 수준 규칙

`record_rules`는 개별 Python `if sheet == ...`를 선언으로 옮긴 구조다.

```yaml
record_rules:
  - id: stable_rule_id
    stage: strategic_brand_fields
    match:
      sheet_name: "시장명"
      molecule: "원천값"
    actions:
      set:
        class: "표준값"
    reason: "업무 근거"
```

지원 action:

| action | 동작 |
| --- | --- |
| `set` | 상수값 설정 |
| `copy` | `raw.*`, `record.*`, context 경로에서 복사 |
| `first_present` | 첫 번째 유효 후보 선택 |
| `null_if_equal` | 두 필드가 같으면 중복 축 제거 |

적용 코드는 `apply_record_rules()` 하나이며, 각 소비 단계가 명시적인 `stage`와
context를 전달한다.

현재 보호 사례:

- 제이클: 원천 `MOLECULE DESC` 유지, class와 molecule 중복 제거
- 가드렛: molecule 기반 이름, TIRZEPATIDE를 GLP-1RA로 분류
- 위너프: 시트명·대상 제품 표기와 alias 조정

새 action이 필요하면 YAML만 추가하지 말고 rule engine, schema, 실패 주입 테스트를
함께 확장한다.

## 4. 차원 registry

정본:

```text
pipeline/contracts/dimension_registry.py
```

`DimensionSpec`은 다음을 한곳에서 선언한다.

- 내부 차원명
- 화면 표시명
- 원천 후보 컬럼
- 활성 여부
- 원천
- 공개 API 차원명
- 공백 정규화 정책

현재 원천 registry:

- UBIST: ATC3, ATC4, 판매사, 성분, 성분용량, 제형, 투여경로, 급여구분
- IQVIA NSA: 제조사, 성분 설명, molecule type, pack, strength, NHI

공유 함수는 enabled spec 조회, API 이름 변환, canonical 이름 변환, 표시 라벨,
정렬 순서, alias 후보, 빈값 정규화를 제공한다. API, mart, 필터 옵션, resolver가
각자 별도 alias 표를 만들지 않도록 한다.

### 4.1 새 차원 추가

1. 원천과 grain을 결정한다. 브랜드 단위인지 제품 단위인지 명시한다.
2. `DimensionSpec`을 해당 원천 registry에 추가한다.
3. source column이 mart까지 보존되는지 확인한다.
4. API 공개명이 기존 필드와 충돌하지 않는지 확인한다.
5. label, order hint, candidate alias가 필요한지 검토한다.
6. `tests/api/test_dimension_registry_contract.py`를 확장한다.
7. filter-options, dynamic request, mart 결과를 전수 대조한다.

제품 단위 차원을 브랜드 행에 올리면 과대 포함이 발생하므로 grain 검증을 생략하지
않는다.

## 5. 데이터 경계

### 5.1 일반 mart

코드:

```text
pipeline/etl/io/mart/general_*.py
pipeline/scripts/api/dynamic_market/general_*.py
```

일반 mart는 ATC4 안에서 원천 제품을 집계한다. MI Master의 class·molecule overlay를
일반뷰 시장 membership에 적용하지 않는다.

### 5.2 전략 ML mart

코드:

```text
pipeline/etl/io/catalog/
pipeline/etl/io/mart/strategic_ml.py
```

MI Master 상세 시트의 membership, recode, 제외 정책을 적용한다. 전략 시장
구성원이 바뀌면 market total, M/S, HHI, 순위도 같이 바뀐다.

### 5.3 전략 CD mart

코드:

```text
pipeline/etl/io/catalog/market/cd_*.py
pipeline/etl/io/mart/strategic_cd.py
```

ML membership에서 CD filter를 적용한다. `cd_filter_id`는 내부 FK이며 공개 계약이
아니다.

## 6. 남아 있는 제약

### 6.1 아직 사람 판단이 필요한 경우

- 시트 순서가 과거 ID와 달라질 때
- 여러 대상 제품 열을 CD 하나로 합칠 때
- 상세 시트 전체가 아닌 특수 CD 조건이 필요할 때
- workbook 원천 표기와 실제 catalog 원천이 다를 때
- 영문 molecule, 한글 브랜드, 제품명이 자동으로 일대일 매칭되지 않을 때
- 동일 표기가 class와 molecule 중 어느 축인지 업무 판단이 필요할 때
- 시장 삭제로 과거 ID·골든·화면 메뉴가 영향을 받을 때

### 6.2 남아 있는 선언과 고정 메타데이터

`mi_master_rules.yaml`의 topology·alias·record rule은 의도적으로 남은 업무
하드코딩이다. `pipeline/etl/config/market_metadata.yaml`에도 설명·운영 메모 등
정적 annotation이 남아 있다. topology 자체는 registry에서 생성하지만 annotation이
신규 시장에 자동으로 완성되는 것은 아니다.

다음 경계도 현재 코드에 남아 있다.

- 일부 시장별 특수 mart 계산과 분석축 후처리
- 특수 CD filter semantics
- target customer priority 해석
- AI 분석·챗봇 측의 시장별 골든과 fallback
- 별도 프론트 저장소의 표시 순서·라벨

새 시장이 API 목록에 나타난다는 사실만으로 모든 화면과 AI 분석이 완성됐다고
판정하면 안 된다.

### 6.3 ML에서 CD로의 분리

기본값은 “상세 시트 전체” CD다. 하지만 실제 직접 경쟁 범위가 더 좁거나 여러 제품
열을 합쳐야 하면 업무 결정이 필요하다. 이 결정은 자동 추론하지 않는다.

### 6.4 원천 스키마 변경

현재 discovery는 시트 구조를 동적으로 찾지만, 헤더 의미까지 무제한으로 추론하지
않는다. ATC, molecule/성분, product/제품을 식별할 수 없는 새 헤더는 loader와
mapping catalog 확장이 필요하다.

## 7. 확장 체크리스트

### 7.1 새 전략 시장

- [ ] 기존 동일 원천 시트를 복사했는가
- [ ] 상세 헤더가 3~12행에 있는가
- [ ] ATC와 molecule/제품 헤더가 있는가
- [ ] `시장정의 & Target` 6·7·10행이 채워졌는가
- [ ] ML membership과 CD membership이 각각 정의됐는가
- [ ] 시트 순서가 기존 ID를 바꾸지 않는가
- [ ] alias·collapse·분석축 예외가 필요한가
- [ ] 임시 workbook discovery 테스트가 통과하는가
- [ ] catalog/API metadata까지 신규 시장이 전달되는가
- [ ] 일반뷰가 무회귀인가

### 7.2 새 분석 차원

- [ ] source와 grain이 정의됐는가
- [ ] `DimensionSpec`이 registry에 추가됐는가
- [ ] source column이 mart까지 보존되는가
- [ ] 공개 API 이름과 label이 정해졌는가
- [ ] 필터 후보값에서 빈값·중복이 제거되는가
- [ ] 일반·전략 경계가 유지되는가
- [ ] 전체 population 계약 테스트가 있는가

### 7.3 새 데이터 소스

- [ ] 파일·API 원천 계약과 보존 위치가 정의됐는가
- [ ] 기간 형식과 갱신 단위가 정의됐는가
- [ ] loader가 fail-closed하는가
- [ ] 내부 공통 grain과 measure가 정의됐는가
- [ ] source별 dimension registry가 있는가
- [ ] 일반 mart와 전략 mart 중 어디에 참여하는지 명시됐는가
- [ ] 다른 소스와 합산하지 않는 경계가 정의됐는가
- [ ] 인입·재실행·롤백 runbook이 있는가

### 7.4 새 계산식

- [ ] 분자·분모·기간·source가 명시됐는가
- [ ] 0·결측·불완전 연도 처리 규칙이 있는가
- [ ] tie-break와 반올림 경계가 결정적인가
- [ ] API와 화면이 같은 결과를 사용하는가
- [ ] 골든·실패 주입·전체 회귀가 있는가

## 8. 검증 명령

핵심 계약:

```bash
PYTHONPATH=.:pipeline/scripts/etl python3 -m pytest -q \
  tests/etl/test_mi_master_selfservice.py \
  tests/api/test_dimension_registry_contract.py \
  tests/api/test_api_response_golden_v2.py
```

추가 검증:

1. 전체 `tests/`에서 신규 실패 0
2. 변경 시장의 catalog before/after 구성원 대조
3. ML과 CD의 분모·순위·HHI 대조
4. 일반뷰 ATC4 무회귀
5. 공개 API 스키마 add/delete/rename 0
6. 프론트가 API cohort를 재정렬·절단하지 않는지 확인

## 9. 정본 위치

| 항목 | 정본 |
| --- | --- |
| MI Master workbook | `data/JW 주요 약품 수동 매핑/` |
| workbook source pin | 같은 디렉토리의 pin 파일 및 orchestrator 입력 계약 |
| 시장 discovery | `pipeline/etl/mi_master_registry.py` |
| 시장 예외 | `pipeline/etl/config/mi_master_rules.yaml` |
| 차원 계약 | `pipeline/contracts/dimension_registry.py` |
| column mapping | `pipeline/etl/config/master_column_mapping_catalog.md` |
| catalog ETL | `pipeline/etl/io/catalog/` |
| mart | `pipeline/etl/io/mart/` |
| API | `pipeline/scripts/api/` |
| 계약 테스트 | `tests/etl/`, `tests/api/` |

정본을 복제한 상수·목록을 새로 만들지 않는다. 필요한 정보가 registry에 없다면
registry 계약을 확장한 뒤 소비자가 읽게 한다.

## 10. [확인 필요]

- 신규 시장 annotation을 `market_metadata.yaml`에서 완전히 자동 생성할 범위
- AI 분석·챗봇 골든이 신규 시장을 자동 수용하는 최종 계약
- 운영 정본 workbook 교체와 source pin 갱신의 조직별 승인 담당
- MI Master 변경에서 운영 화면 반영까지의 확정 SLA
