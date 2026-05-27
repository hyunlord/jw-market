# JW Market Analysis Agent v0.9.0 — 데이터 파이프라인 핸드오프 문서

> **목적**: 현재 mock 으로 동작 중인 backend 를 **실제 데이터 파이프라인 출력으로 대체**하기 위한 완전 가이드.
> **대상 독자**: 데이터 파이프라인 팀.
> **현재 상태**: GKE 배포 완료 (`v0.9.0` image). mock JSON 응답 중. 명세 ↔ 응답 정합 100%.

---

## 0. 배포된 환경 정보

| 항목 | 값 |
|---|---|
| External URL | `https://jwai-dev.jwhealthcare.com/jw-market-analysis/` |
| Swagger UI | `https://jwai-dev.jwhealthcare.com/jw-market-analysis/docs` |
| OpenAPI JSON | `https://jwai-dev.jwhealthcare.com/jw-market-analysis/openapi.json` |
| GKE Deployment | `jw-market-api` (ns=`llmops`) |
| Image | `asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01/jw-market-analysis:v0.9.0` |
| 파이프라인 출력 위치 (제안) | `gs://prj-jw-agn-dev-ai-490605-mart/v0.9/` (TBD) 또는 NFS path 직접 mount |

---

## 1. API endpoint 5개 — 파이프라인이 채워야 할 데이터

### Endpoint 0 — `/api/health`
파이프라인 대상 외 (서비스 alive 체크).

### Endpoint 1 — `/api/brands`
**입력**: 없음 (또는 `?q=` 부분 일치, `?market_id=` 필터)
**출력**: JW brand 25개 평탄 리스트
**데이터 출처**: 16 시장의 `market_meta` + brand 카탈로그 (수동 메타 + IQVIA/UBIST 양쪽)

```json
[
  {
    "brand": "가드메트",
    "market_id": "strategy_003",
    "market_name": "가드메트",
    "market_name_short": "가드메트",
    "market_label_kor": "고지혈증/HMG-CoA",
    "mkt_team": "MKT 1팀",
    "sources": ["UBIST", "IQVIA"],
    "atc_codes": ["C10A1"],
    "atc_desc": "HMG-CoA reductase inhibitors",
    "is_jw": true,
    "is_target": true,
    "is_dual_source": true,
    "rank": 3
  }
]
```

**파이프라인 작업**: brand metadata table 1개 만들면 끝. brand 별 sources / ATC / mkt_team 매핑.

### Endpoint 2 — `/api/market-status`
**입력**: 없음
**출력**: 16 시장의 brand card list (front + back + back_extended 3단)
**데이터 출처**: brand 별 KPI + 마진 + 시장점유율 + 이슈 사건 + 진행 사업 등 → 매일 batch 로 계산

```json
[
  {
    "rank": 1,
    "brand": "라베칸",
    "company": "JW중외제약",
    "is_jw": true,
    "is_target": true,
    "front": { ... },         // 카드 앞면 (요약)
    "back": { ... },          // 카드 뒷면 (상세)
    "back_extended": { ... }, // 카드 클릭 시 확장 정보
    "market_id": "strategy_001",
    "market_name": "라베칸/라베칸듀오",
    ...
  }
]
```

**파이프라인 작업**: 25 brand × (front + back + back_extended) 정보 매일 batch 생성.

### Endpoint 3 — `/api/cause/{brand_name}?view&source&measure`
**입력**:
- `brand_name` (path): brand 한글명
- `view` (query): `market_landscape` | `competitive_dynamics`
- `source` (query): `UBIST` | `IQVIA`
- `measure` (query): `sales` | `volume` | `unit` | `dosage_unit` | `counting_unit`

**출력**: 11개 분석 차트 데이터 (시장 구조 + 경쟁 동향)

```json
{
  "brand": "가드메트",
  "market_id": "strategy_003",
  "market_meta": { ... 21 keys ... },
  "view": "market_landscape",
  "source": "UBIST",
  "measure": "sales",
  "unit_label": "KRW",
  "data": {
    "ei_ms_matrix": { ... },                  // Effort Index × M/S matrix
    "growth_contribution": { ... },           // 성장 기여도 분석
    "sources_data": { ... },                  // 시장 시계열 + HHI
    "growth_contribution_ms_matrix": { ... }, // 성장기여 × M/S
    "target_customer_competition": { ... },   // target 고객 경쟁
    "company_concentration_trend": { ... },   // HHI 추이
    "level_top5_trend": { ... },              // 레벨별 top5 시계열
    "brand_ranking_stacked": { ... },         // brand 순위 stacked
    "company_ranking_stacked": { ... },       // 회사 순위 stacked
    "analysis_levels": { ... },               // ATC level 분석
    "kpi": { ... }                            // KPI 요약
  }
}
```

**파이프라인 작업**: 25 brand × 12 variant (view 2 × source 2 × measure 5 - invalid 조합 제외) = 약 250+ combo 의 cause analysis. 시장 단위 (ATC 4) 로 한 번 계산 후 brand 별로 view 만 다르게.

> **이 endpoint 는 이미 v0.8 부터 운영되어 왔음**. v0.9 변경 없음. **파이프라인 측에서 새로 작업할 부분 아님** (단 schema 만 확인용으로 명시).

### Endpoint 4 — `/api/deep-analysis/{brand_name}` (★ v0.9 핵심)
**입력**: `brand_name` (path) — query param 없음, 토글은 client side
**출력**: 시계열 예측 + 시나리오 + 이벤트 + AI 분석 — **이 endpoint 가 파이프라인 작업의 핵심**

```json
{
  "brand": "가드메트",
  "market_id": "strategy_003",
  "generated_at": "2026.05.19T08:00:00+09:00",
  "available_combos": ["UBIST.sales", "UBIST.volume", "IQVIA.sales", "IQVIA.unit", "IQVIA.dosage_unit", "IQVIA.counting_unit"],
  "market_meta": {
    "market_name": "고지혈증 (HMG-CoA Reductase Inhibitors)",
    "atc4_code": "C10A1",
    "atc4_name": "HMG-CoA reductase inhibitors",
    "sources": ["UBIST", "IQVIA"],
    "default_source": "UBIST"
  },
  "data": {
    "forecast": { ... },       // A. 시계열 예측 (6 combo × 6 brand)
    "simulation": { ... },     // B. 시뮬레이션 (6 combo × by_brand 6 brand)
    "events": [ ... ],         // C. 이슈 list (5건 이상, impact_score 내림차순, 최대 50)
    "ai_analysis": { ... }     // D. AI 4단 분석
  }
}
```

상세 schema 는 §2~§5 참조.

---

## 2. Deep-analysis: A. forecast — 6 combo × 6 brand 시계열

### 데이터 구조

```json
data.forecast.by_combo[combo].{
  period_unit: "월간" | "분기",     // UBIST=월간, IQVIA=분기
  unit_label: "KRW" | "Rx" | "Unit" | "Dosage Unit" | "Counting Unit",
  history_periods: ["2021-01", ..., "2025-12"],     // UBIST 60개월 / IQVIA 20분기
  forecast_periods: ["2026-01", ..., "2035-12"],    // UBIST 120개월 / IQVIA 40분기
  brands: [
    {
      brand: "가드메트",
      is_target: true,                              // 사용자 선택 brand (항상 첫번째)
      is_jw: true,
      history_values: [123, 145, ...],              // history_periods 길이와 동일
      forecast_values: [180, 195, ...],             // forecast_periods 길이와 동일
    },
    // ... 상위 5 brand (target 포함 총 6개)
  ]
}
```

### 6 combo 정의

| combo | source | measure | period_unit | unit_label |
|---|---|---|---|---|
| `UBIST.sales` | UBIST | 매출 | 월간 | KRW |
| `UBIST.volume` | UBIST | 처방량 | 월간 | Rx |
| `IQVIA.sales` | IQVIA | 매출 | 분기 | KRW |
| `IQVIA.unit` | IQVIA | 단위 | 분기 | Unit |
| `IQVIA.dosage_unit` | IQVIA | 투여단위 | 분기 | Dosage Unit |
| `IQVIA.counting_unit` | IQVIA | 카운팅 | 분기 | Counting Unit |

UBIST 만 있는 brand 는 `available_combos` 가 `["UBIST.sales", "UBIST.volume"]` 만, IQVIA 만 있는 brand 는 IQVIA 4개만.

### 데이터 기간

| Source | history | forecast |
|---|---|---|
| UBIST (월간) | **5년 = 60개월** (2021-01 ~ 2025-12) | **10년 = 120개월** (2026-01 ~ 2035-12) |
| IQVIA (분기) | **5년 = 20분기** (2021Q1 ~ 2025Q4) | **10년 = 40분기** (2026Q1 ~ 2035Q4) |

### Top 5 brand 선정 규칙

- **target brand** (선택 brand) 가 항상 첫번째
- 나머지 5 brand 는 **같은 ATC 4 시장 내** 최근 1년 매출/처방량 기준 상위 5
- `is_jw` flag: JW 제품이면 true (target 외에도 JW brand 가 있을 수 있음 — 예: 가드메트의 가드렛)

### 파이프라인 작업

1. **history_values**: IQVIA/UBIST raw data 에서 brand 별 시계열 추출 (이미 mart 에 존재)
2. **forecast_values**: 모델 학습 + 예측 → 다음 절 (§3) 참조
3. brands list 정렬 + top 5 추출 로직

---

## 3. Deep-analysis: B. simulation — 시나리오 + Anomaly (★ 신규)

### 3.1 전체 구조

```json
data.simulation.by_combo[combo].{
  period_unit: "월간" | "분기",
  unit_label: "KRW",
  available_brands: [{brand, is_target, is_jw}, ...],  // 6 brand (target 포함)
  by_brand: {
    "가드메트": {
      model: { ... },                  // 모델 정보 (Prophet/SARIMAX/HW/Linear/Mean)
      forecast_periods: [...],         // forecast 기간 (forecast 영역과 동일)
      scenarios: {
        base:  { values, final_value, label, method, ... },
        upper: { values, final_value, delta_pct_vs_base, label, method, ... },
        lower: { values, final_value, delta_pct_vs_base, label, method, floor_applied, ... }
      },
      horizon_ci_levels: {1y: 0.95, 3y: 0.90, 5y: 0.80, 10y: 0.50},
      stress: { ... },                 // 과거 anomaly mean shock 참고값
      confidence: { ... },             // 신뢰도 (CI width 기반)
      market_comparison: { ... },      // 시장 대비 CAGR
      momentum: { ... },               // forecast slope 기반
      anomaly_signals: { ... },        // 과거 이상 변동 탐지
      target_period: "2027-01",        // 시나리오 카드의 default target
      warnings: ["..."]                // 한계 사유 정직 노출
    },
    "가드렛": { ... },
    // ... 6 brand 모두
  }
}
```

### 3.2 model — 모델 선택 정책

```json
model: {
  name: "Prophet" | "SARIMAX" | "Holt-Winters" | "Linear" | "Mean",
  variant: "basic_with_light_proxy_events",
  selection_reason: "data_size_60_supports_prophet",
  selection_policy: "data_size_dispatch_v1",
  params: {
    seasonality_mode: "additive",
    yearly_seasonality: true,
    weekly_seasonality: false,
    daily_seasonality: false
  },
  event_regressor: { ... },
  fit_quality: { ... }
}
```

**모델 자동 선택 정책 (`data_size_dispatch_v1`)**:

| history 길이 (월간 기준) | 모델 | 이유 |
|---|---|---|
| ≥ 60개월 (5년) | Prophet basic | 충분한 데이터 |
| 40~59 | SARIMAX | seasonality+trend 모델링 |
| 20~39 | Holt-Winters | 단순 + 안정 |
| 12~19 | Linear regression | trend only |
| < 12 | Mean baseline | data 부족 — 평균 + ±N% |

분기 단위 (IQVIA) 는 위 값을 1/3 로 매핑 (분기 20 = 월간 60 등). 또는 별도 dispatch table 정의 가능.

### 3.3 event_regressor — 이벤트의 모델 입력

```json
event_regressor: {
  enabled: true,
  mode: "proxy_light",
  max_regressors: 2,
  regressors: ["price_change_proxy", "market_entry_density_proxy"],
  limitations: ["no_labeled_events_table", "atc_competitor_proxy_can_overcount"]
}
```

**Phase 1 의도 (현재 mock 의 의도)**:
- 라벨링된 이벤트 테이블이 없는 상태에서 proxy 변수 2개를 Prophet regressor 로 추가
- `price_change_proxy`: 시장 평균 약가 변동률 — 약가 재협상 이벤트의 대리 변수
- `market_entry_density_proxy`: 같은 ATC 4 의 신규 brand 진입 개수 — 제네릭 출시 이벤트 대리 변수

**Phase 2 목표**:
- `events` 테이블 직접 라벨링 (수동 또는 R&D agent 자동) 후 정식 regressor 로 전환
- `mode: "labeled_events"` 로 전환

**enabled=false 일 때**: 이벤트는 단순 시각화 마커. 모델 입력 X.

### 3.4 scenarios — 상위/기준/하위 시나리오 (★ 핵심)

```json
scenarios: {
  base: {
    label: "기준",
    method: "selected_model_point_estimate",
    values: [180, 195, 210, ...],          // forecast_periods 길이와 동일
    final_value: 19395821751                // 마지막 시점 값 (또는 target_period 의 값)
  },
  upper: {
    label: "상위 (Best)",
    method: "selected_model_ci_upper_horizon_adaptive",
    values: [253, 273, 294, ...],          // horizon 별 interval_width 다르게 적용
    final_value: 27154150451,
    delta_pct_vs_base: 40                  // (upper.final - base.final) / base.final * 100
  },
  lower: {
    label: "하위 (Worst)",
    method: "selected_model_ci_lower_horizon_adaptive",
    values: [117, 127, 137, ...],
    final_value: 12607284138,
    delta_pct_vs_base: -35,
    floor_applied: false                    // declining trend 가 음수로 가면 0 으로 clamp 시 true
  }
}
```

### 3.5 시나리오 생성 알고리즘 (★ 데이터 파이프라인 핵심)

**Phase 1 (배포 직후 — 빠른 구현)**:

#### Prophet (history ≥ 60 개월)
```python
from prophet import Prophet

# 1) horizon 별 interval_width 다르게
horizons = {'1y': 12, '3y': 36, '5y': 60, '10y': 120}  # 월간 기준
ci_levels = {'1y': 0.95, '3y': 0.90, '5y': 0.80, '10y': 0.50}

# 2) Prophet 한 번 학습
m = Prophet(seasonality_mode='additive', yearly_seasonality=True)
if event_regressor_enabled:
    m.add_regressor('price_change_proxy')
    m.add_regressor('market_entry_density_proxy')
m.fit(df)

# 3) horizon 별로 다른 interval_width 적용
# 방법 A: horizon 별 모델 재학습 (정확하지만 4배 cost)
# 방법 B: 한 모델로 fit 후 forecast 마다 conformalization
results = {}
for label, n in horizons.items():
    m.interval_width = ci_levels[label]  # 새 interval_width
    future = m.make_future_dataframe(periods=n, freq='MS')
    forecast = m.predict(future)
    # 마지막 n 개만 forecast 영역
    results[label] = {
        'base': forecast['yhat'].iloc[-n:].tolist(),
        'upper': forecast['yhat_upper'].iloc[-n:].tolist(),
        'lower': forecast['yhat_lower'].iloc[-n:].tolist(),
    }

# 4) 응답에는 max horizon (10y) 의 120 시점 전체 저장
#    frontend 가 horizon 토글마다 horizon idx = years × 12 - 1 로 자름
final_values = results['10y']
scenarios.base.values  = final_values['base']
scenarios.upper.values = final_values['upper']
scenarios.lower.values = final_values['lower']
```

**중요**: `horizon_ci_levels.1y/3y/5y/10y` 는 단순 메타데이터 (frontend 가 신뢰도 카드에 표시). 실제 values 는 단일 시계열 (max horizon 길이) 로 응답.
**합의 사항**: 응답의 `values[]` 는 가장 긴 horizon (10y) 의 CI 폭을 기반으로 한 값. 토글 시 frontend 가 horizon 별 적절한 CI 값을 표시하기 위해 `horizon_ci_levels` 메타가 따로 있음.

#### SARIMAX (statsmodels)
```python
from statsmodels.tsa.statespace.sarimax import SARIMAX

model = SARIMAX(history, order=(1,1,1), seasonal_order=(1,1,1,12)).fit()
forecast_obj = model.get_forecast(steps=120)
ci = forecast_obj.conf_int(alpha=0.10)  # 90% CI
scenarios.base.values  = forecast_obj.predicted_mean.tolist()
scenarios.upper.values = ci.iloc[:, 1].tolist()
scenarios.lower.values = ci.iloc[:, 0].tolist()
```

#### Holt-Winters (CI 없음 — bootstrap 필요)
```python
from statsmodels.tsa.holtwinters import ExponentialSmoothing
import numpy as np

model = ExponentialSmoothing(history, trend='add', seasonal='add', seasonal_periods=12).fit()
base = model.forecast(steps=120)

# bootstrap simulation prediction intervals
residuals = model.resid
n_sim = 1000
sims = np.zeros((n_sim, 120))
for i in range(n_sim):
    noise = np.random.choice(residuals, 120, replace=True)
    sims[i] = base + np.cumsum(noise)  # simple drift; 더 정교한 simulation 가능
upper = np.percentile(sims, 95, axis=0)
lower = np.percentile(sims, 5, axis=0)
```

또는 임시로 **휴리스틱 ±N%**:
```python
scenarios.upper.values = [v * 1.20 for v in base]   # ±20% 시작
scenarios.lower.values = [v * 0.80 for v in base]
# warnings 에 명시: "holt_winters_heuristic_ci"
```

#### Mean baseline (history < 12)
```python
mean_val = np.mean(history)
scenarios.base.values  = [mean_val] * 120
scenarios.upper.values = [mean_val * 1.30] * 120  # ±30% 휴리스틱
scenarios.lower.values = [mean_val * 0.70] * 120
# warnings: "data_size_lt_12_mean_baseline_only"
```

### 3.6 Post-processing 검증 (필수)

```python
def validate_scenarios(base, upper, lower):
    # 1. 순서 검증: lower < base < upper at every time point
    for t in range(len(base)):
        if not (lower[t] < base[t] < upper[t]):
            # CI 깨짐 — fallback
            return False
    # 2. floor 적용: declining trend 가 음수로 가면 0 또는 history min 으로 clamp
    history_min = min(history)
    floor_applied = False
    for t in range(len(lower)):
        if lower[t] < 0:
            lower[t] = max(0, history_min * 0.5)
            floor_applied = True
    return floor_applied
```

순서 깨지면:
- 이전 batch 결과 재사용 또는
- 단순 휴리스틱 fallback (`±N%`) 사용
- `warnings` 에 명시: `"ci_order_violation_fallback_used"`

### 3.7 horizon_ci_levels — 메타데이터

```json
horizon_ci_levels: {
  "1y": 0.95,
  "3y": 0.90,
  "5y": 0.80,
  "10y": 0.50
}
```

frontend 가 "예측 신뢰도" 카드에 표시. 값 자체는 fixed (또는 model.fit_quality.mape_backtest 기반 조정 가능).

### 3.8 stress — 과거 anomaly mean shock (별도 영역)

```json
stress: {
  method: "anomaly_mean_shock_reference",
  upper_delta_pct: 165.3,      // 과거 up anomaly 평균
  lower_delta_pct: -31.6,      // 과거 down anomaly 평균
  upper_used_fallback: false,  // sample 부족 시 휴리스틱 사용했나
  lower_used_fallback: false,
  shown_as_primary_ci: false,  // UI 에는 reference 로만 표시
  note: "과거 anomaly 평균 shock 기반 stress test 참고값. UI 차트의 시나리오 선 / CI 음영과 별개."
}
```

**계산**:
- `anomaly_signals.items` 의 up direction 평균 `delta_pct` → `upper_delta_pct`
- down direction 평균 → `lower_delta_pct`
- sample 부족 (1건) 시 fallback (역사 평균 ±N%)

### 3.9 confidence / market_comparison / momentum

```json
confidence: {
  score: 71,                      // 0~100
  method: "ci_width_normalized",
  ci_width_absolute: 7273433156,   // (upper - lower) / 2
  ci_width_relative_pct: 37.5,     // ci_width / base * 100
  label: "보통"                    // "낮음" | "보통" | "높음"
}

market_comparison: {
  delta_pp: 5.5,                  // brand_cagr - market_cagr (percentage point)
  brand_cagr_pct: 8.1,            // brand 자체의 5y CAGR 또는 forecast horizon CAGR
  market_cagr_pct: 3.2,           // 같은 ATC 4 시장의 CAGR
  basis: "same_atc4_within_source",
  horizon: "forecast_period",     // "historical_5y" 또는 "forecast_period"
  method: "brand_cagr_minus_market_cagr_same_source"
}

momentum: {
  value_pct_per_period: 2.7,      // forecast 의 평균 기울기 (%/월 또는 %/분기)
  label: "가속 추세",              // "감속 추세" | "정체" | "가속 추세"
  basis: "forecast_first_n_periods",
  n_periods: 3,                    // forecast 의 처음 N 시점 평균
  method: "forecast_slope_avg"
}
```

### 3.10 anomaly_signals — 과거 이상 변동 자동 탐지 (★)

```json
anomaly_signals: {
  method: "rolling_z_score_with_yoy_check",
  threshold_z: 3.0,
  threshold_yoy_pct: 50.0,
  window: 6,                       // 월간=6, 분기=4
  fallback_top_n: 3,
  items: [
    {
      period: "2025-12",
      value: 3953852006,            // 해당 시점 실제 관측값
      expected_value: 6082849241,   // rolling mean 기준 기대값
      delta_pct: -34.5,             // 전월/전분기 대비 변화율
      yoy_pct: -15.0,               // 전년 동기 대비
      z_score: -3.32,
      direction: "down",            // "up" | "down"
      threshold_pass: true,         // |z| >= 3 또는 |YoY| >= 50%
      fallback_rank: null,          // threshold_pass=true 면 null. fallback 시 1~3
      matched_event_id: "iss-1"     // 같은 시점 events 매칭 (인과 X)
    },
    // ... 최대 fallback_top_n=3 까지
  ]
}
```

#### 알고리즘

```python
import numpy as np
from collections import deque

def detect_anomaly(history_values, history_periods, events, source='UBIST'):
    window = 6 if source == 'UBIST' else 4
    threshold_z = 3.0
    threshold_yoy = 50.0
    
    items = []
    for t in range(window, len(history_values)):
        # rolling mean / std (직전 window 만 — t 자신 제외)
        roll_vals = history_values[t-window:t]
        rolling_mean = np.mean(roll_vals)
        rolling_std = np.std(roll_vals)
        if rolling_std == 0: continue
        
        value = history_values[t]
        z = (value - rolling_mean) / rolling_std
        
        # delta_pct (전월/전분기 대비)
        delta_pct = (value - history_values[t-1]) / history_values[t-1] * 100
        
        # YoY (전년 동기 대비)
        yoy_idx = t - 12 if source == 'UBIST' else t - 4
        if yoy_idx >= 0 and history_values[yoy_idx] != 0:
            yoy_pct = (value - history_values[yoy_idx]) / history_values[yoy_idx] * 100
        else:
            yoy_pct = None
        
        threshold_pass = abs(z) >= threshold_z or (yoy_pct is not None and abs(yoy_pct) >= threshold_yoy)
        
        if threshold_pass:
            direction = 'up' if value > rolling_mean else 'down'
            matched_event_id = None
            for ev in events:
                if ev['period_map'][source] == history_periods[t]:
                    matched_event_id = ev['id']
                    break
            items.append({
                'period': history_periods[t],
                'value': value,
                'expected_value': rolling_mean,
                'delta_pct': delta_pct,
                'yoy_pct': yoy_pct,
                'z_score': z,
                'direction': direction,
                'threshold_pass': True,
                'fallback_rank': None,
                'matched_event_id': matched_event_id,
            })
    
    # Fallback: 부족하면 |z| 큰 순으로 top-3 채우기
    if len(items) < 3:
        all_scored = []
        for t in range(window, len(history_values)):
            # 위와 동일하게 z 계산 (생략)
            all_scored.append((abs_z, t, ...))
        all_scored.sort(reverse=True)
        existing_periods = {it['period'] for it in items}
        rank = len(items)
        for abs_z, t, ... in all_scored:
            if history_periods[t] in existing_periods: continue
            rank += 1
            items.append({
                ...,
                'threshold_pass': False,
                'fallback_rank': rank,
            })
            if len(items) >= 3: break
    
    return {
        'method': 'rolling_z_score_with_yoy_check',
        'threshold_z': threshold_z,
        'threshold_yoy_pct': threshold_yoy,
        'window': window,
        'fallback_top_n': 3,
        'items': items[:3],
    }
```

### 3.11 warnings — 정직성 노출

예측 한계를 사용자가 알 수 있도록 명시:

```json
warnings: [
  "event_regressor_is_proxy_not_labeled_event",      // proxy_light mode 사용 중
  "forecast_horizon_10y_is_extrapolation_heavy",     // 10년 예측은 외삽
  "data_size_lt_20_holt_winters_unstable",          // 데이터 부족
  "floor_applied_declining_trend",                   // declining trend 가 음수가 됨
  "holt_winters_heuristic_ci",                       // HW 의 CI 가 휴리스틱
  "ci_order_violation_fallback_used"                 // 검증 깨짐 fallback
]
```

frontend 가 이걸 받아 UI 에 표시 (현재는 미구현, 추후 추가 예정).

---

## 4. Deep-analysis: C. events — 이슈 list

```json
data.events: [
  {
    id: "iss-1",
    title: "피타바스타틴 제네릭 5개사 동시 출시",
    summary: "식약처가 제네릭 5개사 동시 허가 발표. 2026 Q1 약가 협상 예정.",
    body_full: "본문 전체 (modal 표시용, \n\n 으로 단락 분리)",
    category: "rd" | "capital" | "policy" | "trend" | "supply",
    date: "2025-12-02",
    period_map: {
      UBIST: "2025-12",        // anomaly 매칭 / 차트 marker 용
      IQVIA: "2025Q4"
    },
    impact_score: 85,            // 0~100, sort 키
    source: "식약처 보도자료"      // 출처
  },
  // ... 최대 50건, impact_score 내림차순
]
```

### 카테고리 정의

| code | label | 색상 (frontend) |
|---|---|---|
| `rd` | 신약/R&D | jw red family |
| `capital` | 자본/경영 | blue family |
| `policy` | 정책/규제 | beige family |
| `trend` | 외부/트렌드 | green family |
| `supply` | 공급/원료 | gray family |

### 파이프라인 작업

- 외부 뉴스 크롤링 (현재 `news_raw`, `events` 테이블 운영 중) → impact_score 자동 산정 + 카테고리 분류
- `period_map` 은 일자 → 월/분기 단위 변환 (`date` 의 month/quarter 추출)
- R&D agent 등 내부 시스템에서 들어오는 event 도 포함 가능
- impact_score: LLM 으로 자동 산정 또는 keyword/sentiment 기반

---

## 5. Deep-analysis: D. ai_analysis — AI 4단 분석

```json
data.ai_analysis: {
  generated_at: "2026.05.19T08:00:00+09:00",
  phenomenon: {
    title: "가드메트 M/S 8.3%, 4분기 연속 성장",
    body: "2025 Q4 기준 가드메트는 ...",
    bullets: ["YoY 매출 +12.4%", "QoQ +3.1%", ...]
  },
  cause: { title, body, bullets },         // 원인
  prediction: { title, body, bullets },    // 예측
  recommendation: { title, body, bullets } // 권고
}
```

### 작성 정책

- **brand 별 + 매일 1회 batch 갱신**
- source/measure/horizon/brand 토글과 **무관 — 단일 텍스트**
- 4단 narrative: 현상 → 원인 → 예측 → 권고
- 각 stage: title (1줄 헤드라인) + body (2~3문장) + bullets (2~3개, 각 bullet 에 수치 인용 권장)
- LLM 사용 가능. cause + forecast + events 데이터를 input 으로 받아 narrative 생성

### 파이프라인 작업

```python
# 매일 batch job
for brand in JW_BRANDS:
    deep_data = fetch_market_data(brand)   # cause + forecast + events
    ai_analysis = llm_summarize_4stages(
        brand=brand,
        market_data=deep_data,
        prompt_template=AI_4STAGE_TEMPLATE
    )
    write_to_storage(brand, 'ai_analysis', ai_analysis)
```

---

## 6. Frontend ↔ Response path 매핑 (필수 이해)

### Forecast 차트
| Frontend | path |
|---|---|
| 차트 line × 6 brand | `data.forecast.by_combo[k].brands[].{history_values, forecast_values}` |
| X축 라벨 | `history_periods + forecast_periods[0..horizonIdx]` |
| Y축 단위 | `unit_label` |
| Event marker | `data.events[].period_map[source]` |

### Simulation 6 카드
| Frontend 카드 | path |
|---|---|
| 상위 시나리오 | `simulation.by_combo[k].by_brand[B].scenarios.upper.{values[horizonIdx], final_value, delta_pct_vs_base}` |
| 기준 시나리오 | `scenarios.base.{values[horizonIdx], final_value}` |
| 하위 시나리오 | `scenarios.lower.{values[horizonIdx], final_value, delta_pct_vs_base, floor_applied}` |
| 예측 신뢰도 | `horizon_ci_levels.{1y\|3y\|5y\|10y}` |
| 시장 대비 | `market_comparison.{delta_pp, market_cagr_pct, brand_cagr_pct}` |
| 예측 Momentum | `momentum.{value_pct_per_period, label}` |

### Anomaly 박스
| Frontend | path |
|---|---|
| 메타 정보 | `anomaly_signals.{threshold_z, threshold_yoy_pct, window, fallback_top_n}` |
| 각 카드 | `anomaly_signals.items[].{period, delta_pct, z_score, direction, fallback_rank, matched_event_id}` |
| 매칭 이슈 정보 | `data.events[id=matched_event_id]` 로 lookup |

### Issue panel + AI 4단
| Frontend | path |
|---|---|
| 좌측 이슈 카드 | `data.events[]` |
| AI 4단 헤더+본문+bullet | `data.ai_analysis.{phenomenon\|cause\|prediction\|recommendation}.{title, body, bullets}` |

### horizonIdx 계산식 (★)

```python
stepsPerYear = 12 if period_unit == '월간' else 4
horizonIdx = horizonYears * stepsPerYear - 1
# 월간: 1년=11, 3년=35, 5년=59, 10년=119
# 분기: 1년=3,  3년=11, 5년=19, 10년=39
```

values 배열 길이가 horizonIdx 보다 짧으면 `final_value` 로 fallback.

---

## 7. 데이터 흐름 + 파이프라인 작업 단위

### 일간 batch (매일 새벽 03:00 추천)

```
[T-1일 마감 데이터]
       ↓
[1] IQVIA/UBIST raw mart 갱신 (이미 운영 중)
       ↓
[2] brand × combo (6) × 시장 단위 cause analysis 계산
    → /api/cause/{brand} 응답 캐싱 (이미 v0.8 부터 운영)
       ↓
[3] brand × combo (6) 시계열 forecast 모델 학습 + 예측
    → forecast.by_combo[k].brands[]
       ↓
[4] brand × combo × by_brand 시뮬레이션
    → scenarios.{base,upper,lower}
    → confidence/market_comparison/momentum/stress
       ↓
[5] anomaly_signals 탐지
    → rolling z-score + YoY check + matched_event_id lookup
       ↓
[6] events 갱신 (크롤러 + impact_score 산정)
    → data.events
       ↓
[7] AI 4단 분석 LLM 생성
    → data.ai_analysis
       ↓
[8] 통합 JSON 빌드 → backend 가 읽을 위치에 저장
    deep_analysis_{brand}.json × 25 brand
       ↓
[9] backend pod 가 다음 health check 또는 reload signal 시 새 데이터 픽업
```

### 응답 저장 위치 (제안)

현재 backend 는 `/app/backend/data/deep_analysis_{brand}.json` 을 image 안에 박아서 사용 중. 운영 시:

| 옵션 | 설명 | 권장도 |
|---|---|---|
| A. Image rebuild + redeploy | 매일 새 image 빌드 후 rolling update | ❌ overhead 큼 |
| B. PVC / NFS mount + 매일 파일 갱신 | `/mnt/data/deep_analysis_{brand}.json` 파일 매일 batch 가 덮어쓰기, backend 가 lazy reload | ✅ 권장 |
| C. GCS bucket + 매일 동기화 | GCS → PVC sync 또는 backend 가 GCS 직접 read | ✅ 권장 |
| D. DB (PostgreSQL JSONB) + backend 가 DB read | 기존 mart 와 통합, query 시 read | 향후 |

**v0.9 초기에는 B 또는 C 가 빠른 도입 가능**. backend 코드 `main.py` 의 mock 로드 부분만 NFS/GCS read 로 교체.

---

## 8. 알려진 mock 한계 (실제 backend 구현 시 주의)

### 8.1 mock 의 단순화 — 실제 와 다를 부분

| 항목 | mock | 실제 backend |
|---|---|---|
| brand 응답 차등 | 가드메트 1개만 응답 (brand 무관 fixed) | 25 brand 모두 별도 응답 |
| scenarios delta_pct | 모든 combo 가 정확히 -35% / +40% | brand × combo × 시점마다 다름 |
| anomaly items | 3건 fixed | 실제 anomaly 개수에 따라 0~다수 |
| events matching | iss-4 처럼 시점 불일치도 강제 매칭 | period_map 정확 매칭만 |
| AI 4단 | brand 무관 fixed text | brand × 일자별 갱신 |

### 8.2 검증 로직 누락 (mock 은 통과한 척)

- `lower < base < upper` 순서 — mock 은 항상 OK 지만 실제 모델 수렴 실패 시 깨질 수 있음
- `values 배열 길이 = forecast_periods 길이` — 모델이 horizon 짧게 뱉으면 깨짐
- `matched_event_id` 가 `events[].id` 에 실제 존재해야 — orphan 매칭 검증 필요
- `horizon_ci_levels` 키와 `available_combos` 의 period_unit 일치

### 8.3 응답 크기

가드메트 1 brand 의 deep-analysis 응답 = **267 KB**. 25 brand × 267 KB = **6.7 MB**. NFS/GCS 에 충분히 저장 가능. backend memory 에는 brand 별 lazy load 권장 (현재는 boot 시 1 brand 만 로드).

---

## 9. 응답 schema 검증 코드 (파이프라인 출력 후 self-test 용)

```python
import json
from jsonschema import validate

# 명세서 기반 minimal schema (실제는 더 정교)
DEEP_ANALYSIS_SCHEMA = {
    "type": "object",
    "required": ["brand", "market_id", "generated_at", "available_combos", "market_meta", "data"],
    "properties": {
        "brand": {"type": "string"},
        "market_id": {"type": "string"},
        "generated_at": {"type": "string"},
        "available_combos": {
            "type": "array",
            "items": {"enum": ["UBIST.sales", "UBIST.volume", "IQVIA.sales", "IQVIA.unit", "IQVIA.dosage_unit", "IQVIA.counting_unit"]}
        },
        "market_meta": {
            "type": "object",
            "required": ["market_name", "atc4_code", "atc4_name", "sources", "default_source"]
        },
        "data": {
            "type": "object",
            "required": ["forecast", "simulation", "events", "ai_analysis"]
        }
    }
}

# 의미적 검증
def validate_deep_analysis(resp):
    errors = []
    
    # 1. 시나리오 순서
    for combo, sim in resp['data']['simulation']['by_combo'].items():
        for brand_name, sim_brand in sim['by_brand'].items():
            sc = sim_brand['scenarios']
            if not (sc['lower']['final_value'] < sc['base']['final_value'] < sc['upper']['final_value']):
                errors.append(f"{combo}/{brand_name}: 시나리오 순서 깨짐")
            
            # 길이 일치
            fp_len = len(sim_brand['forecast_periods'])
            for s in ['base', 'upper', 'lower']:
                if len(sc[s]['values']) != fp_len:
                    errors.append(f"{combo}/{brand_name}: {s}.values 길이 ≠ forecast_periods")
    
    # 2. anomaly matched_event_id
    event_ids = {ev['id'] for ev in resp['data']['events']}
    for combo, sim in resp['data']['simulation']['by_combo'].items():
        for brand_name, sim_brand in sim['by_brand'].items():
            for item in sim_brand['anomaly_signals']['items']:
                eid = item.get('matched_event_id')
                if eid and eid not in event_ids:
                    errors.append(f"{combo}/{brand_name}: matched_event_id={eid} not in events")
    
    # 3. forecast brands 6개 + target 첫번째
    for combo, fc in resp['data']['forecast']['by_combo'].items():
        if len(fc['brands']) != 6:
            errors.append(f"{combo}: forecast.brands ≠ 6")
        if not fc['brands'][0].get('is_target'):
            errors.append(f"{combo}: brands[0] is_target ≠ true")
    
    return errors

# 사용
with open('deep_analysis_가드메트.json') as f:
    resp = json.load(f)
validate(resp, DEEP_ANALYSIS_SCHEMA)
errors = validate_deep_analysis(resp)
assert not errors, errors
```

---

## 10. 첨부 파일

| 파일 | 용도 |
|---|---|
| `JW_Market_Analysis_API_Spec_v0_9_0.html` | 완전한 API 명세서 (146 row schema + 3개 매핑 도식) |
| `JW_Spec_vs_Swagger_Matching.xlsx` | 명세 ↔ 응답 양방향 정합 검증 결과 |
| `deep_analysis_가드메트.json` | mock 응답 예시 (267 KB) — 파이프라인이 만들어야 할 출력 형식 reference |
| `jw_market_v0.9.0_hardcoded_mockup.html` | 단일 파일 mockup — 더블클릭으로 frontend 동작 시연 |

---

## 11. 우선순위 추천

### Phase 1 (배포 직후 — 빠른 시연)
1. ✅ 25 brand × 6 combo × forecast (Prophet/SARIMAX/HW/Linear/Mean 자동 선택)
2. ✅ scenarios (CI 자동 추출 또는 휴리스틱)
3. ✅ anomaly_signals (rolling z-score)
4. ⚠️ ai_analysis 단순 LLM template (수동 검토 후 일간 batch)
5. ⚠️ event_regressor proxy_light mode (라벨링 안 된 상태)

### Phase 2 (안정화 — 정합성)
1. rolling-origin backtest → `model.fit_quality.mape_backtest_3m`, `residual_std` 실측
2. HW bootstrap CI (현재 휴리스틱 → 정식 prediction interval)
3. floor_applied 검증 + ci_order_violation fallback 로직
4. anomaly mean shock 기반 stress 영역 실제 계산

### Phase 3 (사업 가치 강화)
1. labeled events table 구축 → event_regressor `mode: "labeled_events"` 전환
2. brand × event 의 정량적 영향 추정 (Prophet add_regressor 의 coefficient)
3. AI 4단 narrative LLM 고도화 (cause + forecast + events 통합 추론)

---

## 12. 질의 응답 / 의사결정 필요 사항

| # | 항목 | 현재 상태 | 의사결정 필요 |
|---|---|---|---|
| 1 | 응답 저장 위치 | image 안 (mock) | NFS / GCS / DB 중 선택 |
| 2 | brand 별 lazy load 시점 | boot 시 1 brand (가드메트만) | brand 별 첫 호출 시 lazy load 권장 |
| 3 | LLM 비용 | mock | 25 brand × 매일 4 stage = 약 100 호출/day. cost 추정 필요 |
| 4 | event_regressor 의 정확한 proxy 변수 정의 | "price_change_proxy", "market_entry_density_proxy" | 실제 계산식 정의 필요 |
| 5 | impact_score 산정 방식 | mock 임의값 | LLM 자동 vs 수동 rule |
| 6 | events 의 source / 갱신 주기 | mock | 크롤러 (현재 운영) + R&D agent (예정) |

---

## 13. Contact

- **PM/Tech Lead**: 김관현 (PM, JW 중외제약)
- **JW 측 PM**: 김욱 PM (Market Analysis Agent)
- **External URL 운영**: `https://jwai-dev.jwhealthcare.com/jw-market-analysis/`
- **Swagger UI 로 실제 응답 확인**: `https://jwai-dev.jwhealthcare.com/jw-market-analysis/docs`

문의사항은 명세서 (`JW_Market_Analysis_API_Spec_v0_9_0.html`) 의 schema row 와 함께 확인 부탁드립니다.
