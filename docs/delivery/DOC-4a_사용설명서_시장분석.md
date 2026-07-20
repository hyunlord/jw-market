# DOC-4a · JW 시장분석 사용설명서 (시장분석 화면)

| 항목 | 값 |
|---|---|
| 문서 버전 | v1.0 |
| 기준 코드(develop) SHA | `7ca98403` (worktree `/tmp/jwm-develop-docs`; 본 문서 근거 영역 `pipeline/scripts/api/`는 `761b4def`와 무변경 — 검증 결과 그대로 유효) |
| 운영 배포 | GKE `llmops` ns · deployment `jw-market-backend-api` · generation **302** |
| 근거 코드 | `pipeline/scripts/api/` (routes/·dynamic_market/·models/·composers/) |
| 생성일 | 2026-07-17 |

> **본 문서의 범위.** 이 설명서는 JW 시장분석 화면을 사용하는 담당자(사용자)를 위한 안내서다. 시장분석 **화면 자체는 SI가 운영하는 포탈**이므로, 본 문서는 화면이 백엔드로부터 받는 **데이터·필터 계약**과 코드에서 확정된 사실만을 근거로 화면 요소를 설명한다. 화면의 색상·배치 등 순수 UI 표현은 다루지 않는다. 미구현 기능은 서술하지 않는다.
>
> 모든 라벨·필드명·범위는 develop `761b4def` 실코드에서 확인한 것이며, 확인 불가 항목은 **[확인 필요]**로 표시했다.

---

## 1. 시장분석 화면의 큰 그림

시장분석은 **세 개의 뷰(관점)** 로 구성된다. 백엔드는 뷰 키(`view` / `view_kind`)로 각 관점을 구분한다.

| 화면상 뷰 | 백엔드 뷰 키 | 심층분석 `view_kind` | 성격 |
|---|---|---|---|
| **일반뷰** | `general` | `general` | ATC4 시장 범위 기준의 일반 시장 분석 |
| **전략뷰 — ML** (Market Landscape) | `strategic_ml` | `strategic_ml` → `market_landscape` | JW가 정의한 전략 시장(ML) 기준 |
| **전략뷰 — CD** (Competitive Dynamics) | `strategic_cd` | `strategic_cd` → `competitive_dynamics` | 경쟁 동태(CD) 기준 |

근거:
- `view` 허용 키 `general` / `strategic_ml` / `strategic_cd` — `models/dynamic_market.py:237-244` (`DynamicMarketRequest.view` 설명 문자열: "general, strategic_ml, strategic_cd 중 하나").
- 심층분석 `view_kind` 허용값 `Literal["general", "strategic_ml", "strategic_cd"]` — `routes/deep_analysis.py:1391-1394`.
- `strategic_ml`→`market_landscape`, `strategic_cd`→`competitive_dynamics` 매핑 — `deep_analysis_vocabulary.py:6-9` (`STRENGTH_VIEW_KIND_BY_FORMAL_VIEW`).

> **뷰 선택 원칙(중요).** 사용자는 **브랜드와 뷰만 선택**하면 되며, 내부 시장 식별자(`market_id`)를 직접 지정할 필요가 없다. 백엔드가 브랜드명으로 시장을 내부 해석한다. 자세한 내용은 4장 참고.

---

## 2. 공통 선택: 브랜드 · 소스 · 지표

시장분석의 모든 뷰는 아래 공통 축을 받는다.

| 축 | 화면 선택값 | 백엔드 필드 | 근거 |
|---|---|---|---|
| **브랜드** | 브랜드명(예: 리바로) | `brand_name` / `filters.focus_brand_key` | `routes/market_filter.py:29-33`, `models/dynamic_market.py:211` |
| **소스** | UBIST 또는 IQVIA | `source` = `ubist` \| `iqvia` | `models/dynamic_market.py:246`, `models/market_filter.py:9` |
| **지표** | 매출 또는 수량 | `measure` = `sales` \| `qty` (기본 `sales`) | `models/dynamic_market.py:247` |

- 소스는 화면상 `ubist`/`iqvia` 두 값만 노출한다. 내부의 `iqvia_nsa` 표기는 사용자에게 노출되지 않는다(`models/market_filter.py:69`, `routes/market_filter.py:35`).
- **UBIST와 IQVIA의 차원은 서로 매핑되지 않는다.** 선택한 소스에 따라 사용할 수 있는 차원(레벨)·필터가 달라진다(`models/dynamic_market.py:55-65`, 3장 참조).

---

## 3. 필터: ATC · 차원 · 기간

### 3.1 ATC 다중선택 (시장 범위 1단계)

브랜드·뷰·소스를 고르면 화면은 먼저 ATC 계층(대분류→소분류) 옵션을 받아 표시한다.

- 백엔드 계약: `GET /api/market-filter/atc-options` — 입력 `brand_name`, `view`, `source` → **ATC1/2/3/4** 옵션을 `key`/`level`/`parent`/`flag` 형태로 반환(`routes/market_filter.py:16-40`, `models/market_filter.py:13-72`).
- 각 ATC 옵션의 **`flag=true`** 는 "선택한 브랜드가 해당 ATC 노드에 속함"을 뜻하며, 화면에서 **초기 체크/하이라이트 기준**으로 쓰인다(`models/market_filter.py:25-29`, `routes/market_filter.py:24-26`).
- 일반뷰에서 브랜드를 생략하면 전체 ATC universe가 반환된다(`routes/market_filter.py:30-33`).

ATC는 **다중선택(OR 결합)** 이다. 재계산 요청 시 `filters.atc4`에 선택한 ATC4 코드들이 OR 범위로 전달된다(`models/dynamic_market.py:201-205`, 설명: "공통 ATC4 OR 범위. 일반뷰는 scope, 전략뷰는 ML/CD 내부 narrowing으로 사용").

### 3.2 차원(레벨) — 소스별 목록

차원은 **소스별로 다르다.** 일반뷰에서 화면이 표시하는 분석 레벨은 아래와 같다(`dynamic_market/general_analysis_levels.py:54-70`, `GENERAL_LEVEL_SPECS`).

**UBIST 일반뷰 — 6개 레벨**

| 레벨(화면 라벨) | 백엔드 원천 필드 |
|---|---|
| 판매사 | seller |
| 성분 | molecule |
| 성분용량 | molecule_strength |
| 제형 | form |
| 투여경로 | route |
| 급여구분 | reimbursement |

**IQVIA 일반뷰 — 5개 레벨**

| 레벨(화면 라벨) | 백엔드 원천 필드 |
|---|---|
| MFR NAME KOR | mfr |
| MOLECULE TYPE | molecule_type |
| MOLECULE DESC | molecule_desc |
| STRENGTH | strength |
| NHI TYPE | nhi |

재계산 요청(`POST /api/dynamic-market`)에서 차원 필터로 좁힐 수 있는 값(`filters.analysis_level`)은 소스별로 다음과 같다(`models/dynamic_market.py:19-52`).

- **UBIST**: `seller`(판매사), `molecule`(성분), `molecule_strength`(성분용량), `form`(제형), `route`(투여경로), `reimbursement`(급여구분), `atc3`, `atc4`, 그리고 값-슬라이스용 `facility`(종별), `specialty`(진료과), `pairs`(종별×진료과).
  - 각 필터의 값 목록 안에서는 OR로 결합된다(`models/dynamic_market.py:24-38`).
  - 성분(`molecule`)은 복합 성분도 분해하지 않고 원문 한 값으로 취급한다(`models/dynamic_market.py:25-29`, 예: `PITAVASTATIN / EZETIMIBE`).
- **IQVIA**: `mfr_name_kor`, `molecule_type`, `molecule_desc`, `pack_desc`, `strength`, `nhi_type`, `audit_code`(값 슬라이스; 비우면 전체 audit matrix 포함)(`models/dynamic_market.py:46-52`).

#### 3.2.1 채널(종별) — UBIST

UBIST 종별은 원천 한국어 값이 MI팀 표기로 매핑된다(`etl/config/customer_dictionary.yaml:9-23`).

| MI팀 표기 | UBIST 원천 |
|---|---|
| TH | 상급종합병원 |
| GH | 종합병원 |
| Semi | 병원 |
| CL | 의원 |
| 기타 | 보건소 + 기타 (2:1 매핑) |

#### 3.2.2 진료과(specialty) — UBIST

UBIST 진료과 매핑(`etl/config/customer_dictionary.yaml:29-66`).

| MI팀 표기 | UBIST 원천 |
|---|---|
| IGF | 가정의학과 + 내과 + 일반의 (1:3 합산 매핑) |
| Cardio | 순환기 |
| GI | 소화기 |
| Endo | 내분비 |
| Nephro | 신장 |
| Uro | 비뇨의학과 |
| Neuro | 신경과 |

> **주의점 ① — "분리되지 않은 내과"의 의미.** UBIST 원천에는 `내과(IM)` 라는 상위(aggregate) 진료과가 있고, 그 아래 세부 원천 항목으로 순환기·소화기·내분비·신장·류마티스·혈액종양·호흡기·감염·알레르기, 그리고 **"분리되지 않은 내과"** 가 있다(`customer_dictionary.yaml:55-66`). 여기서 **"분리되지 않은 내과"는 세부 진료과로 분류되지 않은(미분류) 내과 건**을 가리키는 하나의 세부 항목일 뿐, **내과 전체가 아니다.** 상위 `내과(IM)` 행은 세부 항목 전건과 동일 grain을 중복하므로 집계 전에 제외된다(`customer_dictionary.yaml:53-54`).
>
> **→ 내과 전체를 보려면** "분리되지 않은 내과" 하나가 아니라, **내과 세부 진료과 항목들을 다중선택**해야 한다.

### 3.3 기간(period)

- 기간은 `YYYY-MM` 형식의 시작·종료로 지정한다: `options.period_range.start` / `end`(`models/dynamic_market.py:215-229`, 예 `2025-01` ~ `2026-04`).
- 범위를 지정하면 시계열이 해당 구간으로 잘려 계산된다(`dynamic_market/period_window.py`의 `trim_period_rows` 경유, `general_analysis_levels.py:97-108`).

---

## 4. 재계산과 시장 식별(브랜드+뷰만으로 충분)

화면에서 브랜드·뷰·소스·필터를 정하면 **재계산 요청**이 백엔드로 전송된다.

- 엔드포인트: `POST /api/dynamic-market` — "동적 시장 원인분석 재계산"(`routes/dynamic_market.py:64-73`). 응답은 원인분석(`/api/cause`)과 같은 필드 트리로 반환된다.
- 필터 옵션 조회: `GET /api/dynamic-market/filter-options`(포탈 필터 UI용), `GET /api/dynamic-market/brand-option-check`(브랜드 선택 직후 기본 체크 상태 확인)(`routes/dynamic_market.py:412-485`).

> **주의점 ③ — view 원칙: 브랜드+뷰 선택이며 `market_id` 지정 불요.** `market_id`는 공개 입력이 아니며, 백엔드가 **브랜드명으로 시장을 내부 해석**한다(`routes/dynamic_market.py:482-483`: "`market_id`는 공개 입력이 아니며, 브랜드명으로 시장을 내부 해석합니다"). 전략뷰에서는 브랜드가 속한 전략 시장(`ml_id`)이, 일반뷰에서는 대표 ATC4가 내부적으로 resolve된다(`models/market_filter.py:70` `market_id` 설명: "전략뷰에서 resolve된 ml_id 또는 일반뷰 대표 ATC4"; `dynamic_market/resolvers.py`의 `_view_source_id`, `analysis_levels.py:158-162`).
>
> 심층분석 계약에서 `market_id`의 의미는 뷰마다 다르다: **general=ATC4, strategic_ml=ml_id, strategic_cd=cd_id**(`routes/deep_analysis.py:1397-1399`). 이는 내부 계약값이며 사용자는 브랜드+뷰만 선택하면 된다.

---

## 5. 그래프/시계열 · 노출 브랜드와 강조

시장분석 화면의 브랜드 시계열/랭킹에는 두 개념이 구분된다.

- **노출(표시) 브랜드:** 화면에 함께 표시되는 브랜드 집합. 선택 브랜드를 먼저 두고, 나머지는 총매출 기준 상위 경쟁 브랜드로 채운다. 경쟁 브랜드 상한은 **5개**다(`competitor_ranking.py:20-37`, `MAX_COMPETITOR_COUNT = 5`; 랭킹 `top_n=5`, `dynamic_market/cause_ranking.py:88-91`). 선택 브랜드는 이 상한과 별개로 항상 포함된다.
- **강조(하이라이트):** 표시된 브랜드 중 특정 브랜드를 시각적으로 강조하는 것. 백엔드는 각 브랜드에 `is_selected`(선택 브랜드)·`is_jw`(JW 자사 브랜드) 플래그를 실어 보낸다. 계약상 **`is_selected` 브랜드는 굵게, `is_jw`는 강조 표시 대상**이다(`openapi_docs.py:843-844`, `openapi_docs.py:970`). 즉 **강조 대상은 항상 노출 브랜드의 부분집합**이다(선택/자사 브랜드는 노출에 항상 포함되므로).

> **[확인 필요] — 하이라이트 min5/max15.** 브랜드활동/트래커 계열에서 논의된 "하이라이트 min5 / max15" 상수는 develop `761b4def`의 시장분석 백엔드(`pipeline/scripts/api/dynamic_market/`, `competitor_ranking.py`)에서 리터럴로 확인되지 않았다. 시장분석 노출 상한은 위의 경쟁 5개(+선택 브랜드) 계약으로만 확정된다. min5/max15 규칙이 실제 화면에 적용되는지는 포탈(SI) 프론트 또는 브랜드활동 트래커 코드에서 별도 확인이 필요하다.

---

## 6. 성장률 지표 (CMGR / CQGR)

시장 규모의 성장률(월 단위 CMGR, 분기 단위 CQGR)은 **비연율화 복리** 기준이다.

- 계산식: `(현재값 / 기준값) ** (1 / 경과기간) - 1` — **경과 기간당 복리 성장률**이며 **연율화하지 않는다**(`market_growth.py:18-36`, `compound_period_growth_pct`).
- 코드 주석(F-133 계약, `market_growth.py:27-31`): "the growth metric is the compound growth **per elapsed period** ... **not an annualized rate**. ... 한두 기간을 연율화할 때 생기는 급등(spike)을 피하기 위함."
- 기준값(baseline)은 범위 내에서 하나로 고정된다(`market_growth.py:44` "against one baseline fixed for the range"). 기준이 없거나 0이면 해당 기간은 값 없이 사유(`insufficient_history`/`zero_baseline`/`invalid_baseline`)로 표기된다(`market_growth.py:67-72`).

> **주의점 ② — 성장률은 비연율화 월/분기 복리다.** CMGR/CQGR 카드에 표시되는 성장률은 연 환산 값이 아니라, **경과한 월(또는 분기)에 대한 복리 성장률**이다. 초기 몇 개 기간의 값이 연율화 지표보다 낮게 보이는 것은 이 정의상 정상이다.

---

## 7. 심층분석 (브랜드별 딥리포트)

브랜드를 선택하면 해당 브랜드의 심층분석(딥리포트)을 조회할 수 있다.

- 엔드포인트: `GET /api/deep-analysis/{brand_name}` — 입력 `view_kind`(general/strategic_ml/strategic_cd), `market_id`, `source`로 검증된 컨텍스트에서 심층분석 payload를 반환한다(`routes/deep_analysis.py:1367-1400`).
- 구성 요소(사전 계산 블록 기반, `deep_analysis_serving.py`):
  - **예측(forecast)** 과 **시뮬레이션(simulation)** — 동일한 사전 계산 블록(`deep_forecast_block` 테이블)에서 함께 제공된다(`routes/deep_analysis.py:1372-1373`, `deep_analysis_serving.py:20`, `load_forecast_block`).
  - **브랜드 강도(brand strength)** — 시장 기준(`agent3_brand_strength_market`)과 소스 기준(`agent3_brand_strength_source`) 테이블에서 제공된다(`deep_analysis_serving.py:21-22`).
- 예측과 시뮬레이션은 **동일한 사전 계산 block을 사용**하므로 두 결과는 서로 정합적이다(`routes/deep_analysis.py:1372-1373`).

> 심층분석의 세부 섹션 구성/문안(리포트 소제목 등)은 사전 계산 블록의 내용에 의존하며, 본 백엔드 코드는 블록을 읽어 전달하는 서빙 어댑터다(`deep_analysis_serving.py:1` "Read-only adapters for the formal deep-analysis serving tables"). 화면에 표시되는 정확한 섹션 문안 목록은 **[확인 필요]**(포탈 렌더 계층 또는 블록 생성 파이프라인에서 확정).

---

## 8. 화면 캡처 플레이스홀더 (캡처 리스트)

아래 항목은 실제 포탈 화면 캡처로 대체되어야 한다. 캡처 시 3장의 라벨·값이 화면과 일치하는지 대조할 것.

1. `[화면: 일반뷰 — 브랜드/소스/지표 선택 상단바]`
2. `[화면: ATC 다중선택 트리 (ATC1~4, 브랜드 소속 노드 하이라이트)]`
3. `[화면: 일반뷰 UBIST 6개 분석 레벨(판매사·성분·성분용량·제형·투여경로·급여구분) 탭]`
4. `[화면: 일반뷰 IQVIA 5개 분석 레벨 탭]`
5. `[화면: 진료과 필터 — 내과 세부 진료과 다중선택 및 "분리되지 않은 내과" 항목]`
6. `[화면: 기간(period_range) 선택 컨트롤]`
7. `[화면: 브랜드 시계열 그래프 — 노출 브랜드와 선택/자사 강조 표시]`
8. `[화면: 브랜드/기업 랭킹 (선택 브랜드 + 상위 경쟁 5)]`
9. `[화면: 성장률 카드 (CMGR / CQGR)]`
10. `[화면: 전략뷰 ML(Market Landscape)]`
11. `[화면: 전략뷰 CD(Competitive Dynamics)]`
12. `[화면: 심층분석 — 예측/시뮬레이션/브랜드 강도]`

**플레이스홀더 총 12개.**

---

## 부록 · 확인 필요 항목 요약

| # | 항목 | 사유 | 처리 |
|---|---|---|---|
| 1 | 하이라이트 min5/max15 규칙 | 시장분석 백엔드 코드에 리터럴 미확인 (5장) | 포탈(SI) 프론트/트래커 소관 → 저장소 작업 메모 `OPEN_QUESTIONS.md` |
| 2 | 심층분석 화면 섹션 문안 목록 | 사전 계산 블록/포탈 렌더 의존 (7장) | 포탈(SI) 렌더 계층 소관 → 저장소 작업 메모 `OPEN_QUESTIONS.md` |

**[확인 필요] 총 2건 — 둘 다 포탈(SI) 렌더 계층 소관으로, 백엔드(jw market) 조사로는 해소 불가. 저장소 작업 메모 `OPEN_QUESTIONS.md`에 타 세션(SI) 회신 항목으로 등재되어 있으며 전달 패키지에서는 제외한다.**
