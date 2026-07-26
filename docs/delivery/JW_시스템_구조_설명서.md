# JW 시장분석 시스템 구조 설명서

작성 기준: `jw-private/develop` 2026-07-26 소스
대상: JW 시장분석 실무자, MI팀, 운영 담당자, 개발자

## 1. 먼저 알아야 할 핵심

이 시스템은 원천 파일을 화면에서 바로 읽는 프로그램이 아니다. 원천 데이터를
검증하고 표준 형태로 적재한 뒤, 분석용 mart를 만들고, 백엔드 API가 그 결과를
프론트 화면과 챗봇에 전달한다.

가장 중요한 구분은 다음과 같다.

| 구분 | 시장을 정하는 기준 | 용도 |
| --- | --- | --- |
| 일반뷰 | **ATC4 코드만** | 사용자가 ATC 범위를 선택해 전체 치료 시장을 조회 |
| 전략뷰 Market Landscape | **MI Master 시트별 정의** | JW 제품 관점의 전략 시장 분석 |
| 전략뷰 Competitive Dynamics | Market Landscape 범위를 **직접 경쟁 필터로 한 번 더 축소** | 더 좁은 직접 경쟁 구도 분석 |

따라서 **“일반뷰 = Market Landscape”는 틀린 이해**다. Market Landscape와
Competitive Dynamics는 모두 전략뷰이며, 일반뷰와 별도 시장 계약을 사용한다.

`market_id`는 내부 연결용 식별자다. 사용자가 입력하는 값이나 외부 API 응답 필드로
노출하지 않는 것이 계약이다. 화면과 문서에서는 시장명, 브랜드명, ATC 코드 같은
업무 용어를 사용한다.

## 2. 전체 데이터 흐름

```text
원천 데이터
  UBIST / IQVIA NSA / IQVIA CSD·Keyword / MI Master / HIRA
        |
        v
인입
  업로드 포털 또는 정해진 수집 작업
        |
        v
검증·적재
  파일 계약 검사 -> 원천별 표준 테이블/Parquet
        |
        v
mart 생성
  일반뷰 ATC4 mart + 전략 ML mart + 전략 CD mart
        |
        v
백엔드 API
  요청 범위 확인 -> 지표·경쟁 브랜드·분석 레벨 조립
        |
        +------------------+
        v                  v
시장분석 포털            챗봇·연계 서비스
```

### 2.1 원천 데이터

원천은 매출·처방, 채널 활동, 시장 정의, 공공 통계로 나뉜다. 이 단계에서는
파일이나 외부 조회 결과를 그대로 보관하며, 아직 화면용 수치를 만들지 않는다.

- 코드 위치: `data/`, `pipeline/etl/io/`, `pipeline/scripts/etl/`
- 자동/수동: 정기 수집원은 작업이 자동으로 시작될 수 있다. 신규 파일 제공,
  MI Master 편집, 실패 세트 재실행은 사람의 작업이 필요하다.

### 2.2 인입

시장 데이터 파일은 업로드 포털에서 카테고리, 기준 기간, 파일을 선택하고 제출을
확정한다. 확정된 제출은 manifest를 만들고 인입 훅을 호출한다. 훅은 파일 형식과
내용 계약을 먼저 검사하며, 검증을 통과한 건만 다음 적재 단계로 넘긴다.

- 코드 위치: `pipeline/scripts/ingest_hook/`, `pipeline/etl/stages/`
- 포털 소스: 별도 `jw-data-input` 저장소
- 자동/수동: 제출 확정 이후 검증과 Job 시작은 자동이다. 대기·실패 세트의
  재실행은 사람이 수행한다.

### 2.3 적재

적재 단계는 서로 다른 원천 형식을 내부 공통 컬럼으로 바꾼다. 예를 들어 브랜드명,
제품명, ATC 코드, 성분, 제형, 회사명, 기간, 매출·처방량을 표준화한다. MI Master는
시장 정의, 상세 매핑, 브랜드 통합, 제품 행 등의 카탈로그로 분해된다.

- 코드 위치: `pipeline/etl/io/ubist_loader.py`,
  `pipeline/etl/io/iqvia_loader.py`, `pipeline/etl/io/catalog/`
- 자동/수동: 정상 인입 뒤 자동 실행된다. 원천 컬럼 구조가 바뀌거나 매핑할 수 없는
  값이 생기면 개발자 또는 데이터 담당자의 확인이 필요하다.

### 2.4 mart 생성

mart는 화면 조회에 맞게 미리 계산한 분석 데이터다.

- 일반뷰: ATC4 범위 안에서 브랜드·회사·기간별 합계와 지표를 계산한다.
- 전략 ML: MI Master의 시트별 범위와 recode를 적용한다.
- 전략 CD: 전략 ML 범위에 MI Master의 직접 경쟁 필터를 적용한다.

코드 위치는 `pipeline/etl/io/mart/`이며, 월간 전체 체인과 단계 실행은
`pipeline/orchestrator/`, `pipeline/etl/run.py`, `RUNBOOK_MONTHLY.md`에서 관리한다.
오케스트레이터는 원천 epoch 변경을 감지해 자동 실행 여부를 판단한다. 운영 mart
교체와 복구는 별도 승인 절차다.

### 2.5 백엔드 API

백엔드는 mart를 조회하고 화면이 요구하는 형태로 조립한다. 시장 범위, 기간, 소스,
분석 레벨을 확인하고 브랜드 순위, 추이, 점유율, HHI, 경쟁 브랜드 집합 등을
응답한다.

- 코드 위치: `pipeline/scripts/api/`, `api/`
- 배포 단위: Python `jw-market-backend-api`
- 주의: Java BFF와 Python 백엔드는 별도 서비스다.

### 2.6 화면

프론트는 API 응답을 표, 차트, 필터로 표현한다. 화면에서 시장 정의나 지표를 다시
계산하지 않고 API 계약을 그대로 사용하는 것이 원칙이다.

- 시장분석 포털 소스: 별도 프론트 저장소
- 이 저장소의 화면 계약 자료: `docs/delivery/`, `tests/api/`

## 3. 일반뷰와 전략뷰

### 3.1 일반뷰

일반뷰 시장은 ATC4 코드의 합집합이다. 사용자가 ATC 계층 필터를 바꾸면 해당
ATC4 범위로 시장을 다시 조회한다. MI Master의 class, molecule recode, 직접 경쟁
필터를 일반뷰에 섞지 않는다.

예를 들어 C10A1을 조회하면 C10A1 범위의 원천 제품과 브랜드가 분모가 된다.
선택 브랜드가 속한 MI Master 시트가 무엇인지는 일반뷰 시장 범위를 바꾸지 않는다.

관련 코드:

- `pipeline/etl/io/mart/general_*.py`
- `pipeline/scripts/api/dynamic_market/general_*.py`
- `pipeline/contracts/dimension_registry.py`

### 3.2 전략뷰 Market Landscape

Market Landscape는 MI Master의 상세 시트 하나를 기본 시장으로 본다. 다음 요소를
함께 사용한다.

1. `시장정의 & Target`의 ATC4 및 데이터 소스
2. 상세 시트의 제품·성분 행
3. molecule 또는 class recode
4. 제형·용량·급여 등 분석 축
5. “제외” 표시
6. 선언된 브랜드 예외 규칙

즉 ATC4만 같은 제품을 모두 넣는 시장이 아니라, MI팀이 정의한 전략 시장이다.

관련 코드:

- `pipeline/etl/mi_master_registry.py`
- `pipeline/etl/io/catalog/`
- `pipeline/etl/io/mart/strategic_ml.py`

### 3.3 전략뷰 Competitive Dynamics

Competitive Dynamics는 Market Landscape와 별개의 일반 시장이 아니다.
Market Landscape에서 시작해 MI Master의 48~50행 직접 경쟁 정의와 상세 필터로
범위를 더 좁힌 전략뷰다. 단순한 시장은 상세 시트 전체를 사용할 수 있고, 일부
시장은 별도 필터 또는 여러 제품 열 합치기 규칙이 필요하다.

관련 코드:

- `pipeline/etl/io/catalog/market/cd_*.py`
- `pipeline/etl/io/mart/strategic_cd.py`
- `pipeline/etl/config/mi_master_rules.yaml`

## 4. 데이터 소스별 역할

| 소스 | 담는 내용 | 주 사용 화면·기능 | 갱신 방법 | 갱신 주기 |
| --- | --- | --- | --- | --- |
| UBIST | 월 단위 처방·매출·수량, ATC, 제품, 회사, 성분·제형 차원 | 일반뷰, UBIST 기반 전략뷰, 처방 추이 | 파일 인입 후 표준 적재와 mart 재계산 | [확인 필요: 운영 제공 일정] |
| IQVIA NSA | 분기 단위 시장 매출, 제품·제조사·성분·pack 등 | 일반뷰와 IQVIA 기반 전략 시장 | 원천 파일/캐시 적재 후 mart 재계산 | [확인 필요: 운영 제공 일정] |
| IQVIA CSD | 채널·종별·진료과별 콜 활동 | 브랜드 활동의 콜 수, 응답 분포, 활동량 | CSD workbook 인입 후 brand-activity 적재 | [확인 필요: 운영 제공 일정] |
| IQVIA Keyword | 브랜드·기간별 키워드/INTEREST 원천 | 키워드 점유, 키워드×INTEREST | Keyword workbook 인입 후 brand-activity 적재 | [확인 필요: 운영 제공 일정] |
| MI Master | 전략 시장, 대상 제품, recode, 제외, 분석 축, 직접 경쟁 정의 | Market Landscape, Competitive Dynamics, 전략 메타데이터 | 정본 workbook 교체 후 catalog·mart 검증 | 업무 변경 시 |
| HIRA | 질환·환자 수·분포 등 공공 통계 | 챗봇의 HIRA 근거 질의와 관련 분석 | HIRA 조회·수집 작업 | [확인 필요: 운영 수집 일정] |

UBIST와 IQVIA NSA는 같은 숫자로 합쳐 쓰는 원천이 아니다. 기간 단위도 각각 월,
분기로 다르므로 화면은 선택한 소스의 분모와 기간을 사용한다. CSD와 Keyword는
시장 크기 원천이 아니라 브랜드 활동을 설명하는 원천이다.

## 5. 주요 계산 로직

### 5.1 M/S(시장 점유율)

점유율은 같은 시장, 같은 기간, 같은 소스, 같은 측정값 안에서 계산한다.

```text
브랜드 M/S(%) = 브랜드 값 / 해당 시장 전체 브랜드 값의 합 × 100
```

분모에서 빠지는 범위는 분자에서도 빠져야 한다. 일반뷰의 분모는 선택 ATC4 범위,
전략 ML의 분모는 MI Master 시장 구성원, 전략 CD의 분모는 그보다 좁힌 직접 경쟁
구성원이다. 다른 소스나 다른 기간 값을 섞지 않는다.

### 5.2 성장률과 CQGR

단순 성장률은 기준값과 현재값의 차이를 기준값으로 나눈다.

```text
성장률(%) = (현재값 / 기준값 - 1) × 100
```

CQGR/CMGR은 고정 기준기간에서 현재기간까지 실제 경과한 기간 수를 사용한다.

```text
기간당 복합성장률(%) =
  ((현재값 / 기준값) ^ (1 / 경과기간수) - 1) × 100
```

UBIST는 월 12개, IQVIA NSA는 분기 4개를 1년으로 인식한다. 기준값이 0이거나
필요한 이력이 없으면 억지로 0%를 만들지 않고 계산 불가 사유를 반환한다.

### 5.3 HHI와 CR5

HHI는 시장에 있는 각 브랜드의 점유율을 제곱해 더한다.

```text
HHI = Σ(브랜드 점유율(%)²)
```

값이 클수록 소수 브랜드에 집중된 시장이다. 연 단위 HHI는 완결된 연도만 사용한다.
UBIST는 12개월, IQVIA NSA는 4분기가 모두 있어야 완결 연도로 본다.

CR5는 같은 분모 안에서 점유율 상위 5개 브랜드의 점유율 합이다.

```text
CR5(%) = 상위 5개 브랜드 M/S의 합
```

### 5.4 성장 기여도

시장의 시작값과 종료값 차이를 시장 성장분으로 보고, 각 브랜드의 증감이 그 성장분
중 얼마를 설명하는지 계산한다.

```text
시장 성장분 = 시장 종료값 - 시장 시작값
브랜드 성장 기여도(%) = 브랜드 증감 / 시장 성장분 × 100
```

시장 전체가 감소한 경우에는 부호를 포함해 해석해야 한다.

### 5.5 브랜드 경쟁 순위

브랜드 경쟁 집합은 **선택 브랜드 + 경쟁 브랜드 5개**, 최대 6개다.

1. 선택 브랜드를 먼저 고정한다.
2. 같은 시장 범위의 총매출 내림차순으로 나머지 브랜드를 정렬한다.
3. 동률이면 안정적인 브랜드 키 순서를 사용한다.
4. 선택 브랜드를 제외한 상위 5개를 붙인다.

선택 브랜드가 매출 Top 5 안에 있어도 전체 개수는 5개로 줄지 않는다. 프론트가
최근 값으로 다시 Top 5를 뽑거나 0값 브랜드를 삭제해서도 안 된다.

## 6. 구성 요소 관계

| 구성 요소 | 책임 | 다른 요소와의 관계 |
| --- | --- | --- |
| ETL 파이프라인 | 원천 검증, 표준화, catalog, mart 생성 | 인입 결과를 받아 API용 데이터를 만듦 |
| Python 백엔드 API | mart 조회와 응답 조립 | 포털과 챗봇이 호출 |
| 프론트 | 필터·표·차트 표시 | API 응답을 재정의하지 않고 표현 |
| 인입 포털·훅 | 파일 제출, manifest, 검증, 적재 Job 시작 | ETL의 진입점 |
| 크롤러 | 뉴스 수집, 사건·브랜드 점수 생성 | 시장 수치와 별도 근거 데이터를 제공 |
| 챗봇 | 질문 의도 파악, 데이터 도구 호출, 답변 조립 | 백엔드/HIRA/문서 브리지를 사용 |
| 문서 브리지 | 첨부 문서의 저장·검색 연결 | 챗봇이 파일을 직접 파싱하지 않도록 분리 |

관련 코드 디렉토리:

```text
pipeline/etl/             원천 적재, catalog, mart
pipeline/orchestrator/    실행 순서와 epoch 판단
pipeline/scripts/api/     Python API 서비스
pipeline/scripts/crawler/ 뉴스 수집·가공
chat/jw-chat-agent-poc/   챗봇
chat/wf301-vdb-bridge/    문서 업로드·검색 브리지
deploy/                   이미지·Kubernetes 선언
tests/                    계약·회귀 테스트
```

## 7. 변경 요청을 어디로 보내야 하는가

| 바꾸려는 것 | 먼저 볼 곳 | 담당 |
| --- | --- | --- |
| 전략 시장 구성·분류·대상 제품 | MI Master | MI팀, 최초 반영 검증은 개발자 |
| 브랜드 한정 예외 | `pipeline/etl/config/mi_master_rules.yaml` | MI팀 근거 + 개발자 반영 |
| 일반뷰 ATC4 시장 규칙 | 일반 mart/API | 개발자 |
| 새로운 계산식·API 필드 | mart/API 계약 | 개발자 |
| 화면 배치·표현 | 프론트 저장소 | 프론트 개발자 |
| 원천 파일 컬럼 변경 | loader와 workbook 계약 | 데이터/백엔드 개발자 |

MI Master 실무 절차는
[`MI_MASTER_셀프서비스_가이드.md`](MI_MASTER_셀프서비스_가이드.md)를,
개발자 확장 절차는
[`JW_확장구조_개발자용.md`](JW_확장구조_개발자용.md)를 따른다.

## 8. [확인 필요]

- 각 원천의 JW 업무상 확정 갱신 주기와 제출 마감시간
- MI Master 변경 요청을 승인하는 담당자와 운영 반영 SLA
- 운영 포털에서 사용하는 화면 명칭과 메뉴 위치의 최신 캡처

위 항목은 코드만으로 확정할 수 없어 임의 수치를 쓰지 않았다.
