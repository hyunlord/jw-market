# JW Market Backend API — EP별 실호출 캡처

- 대상 pod: `jw-market-backend-api (deploy, ns=llmops, 8 replicas; image sha256:d5e23b72...cd66)` — `kubectl exec -i deploy/jw-market-backend-api -- python3` (urllib, http://localhost:8000)
- pod 기준 캡처 시각(UTC): 각 항목 `ts_utc` (배치 시작 2026-07-17T09:17:00.157932Z)
- 응답 요약 규칙: dict는 전체 key 보존, list는 첫 1개 원소 + `...(N개 중 1개)`, 400자 초과 문자열은 절단. `bytes`는 원본 응답 크기.
- EXTERNAL_PATH_PREFIX=/jw-market-backend-api 는 FastAPI root_path(문서/프록시용)로, 실경로는 프리픽스 없이 `/api/...` 그대로 유효.

## root_frontend
- 요청: `GET /`
- ts_utc: 2026-07-17T09:16:33.277921Z  | status: 200  | bytes: 493936
- resp headers: `{"content-type": "text/html; charset=utf-8"}`
- 응답(구조 보존 요약):
```json
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>JW 시장분석 Agent · Strategic View</title>
<link r
```

## health
- 요청: `GET /api/health`
- ts_utc: 2026-07-17T09:16:33.293070Z  | status: 200  | bytes: 107
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "status": "ok",
 "markets_loaded": 25,
 "brands_loaded": 25,
 "version": "ad782bc064ba03a45eaa4f1e301dbd75b8bf9a9e"
}
```

## market_status
- 요청: `GET /api/market-status`
- ts_utc: 2026-07-17T09:16:33.296996Z  | status: 200  | bytes: 35015
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "brand_cards": [
  {
   "atc_codes": [
    "A2B2"
   ],
   "atc_desc": "PROTON PUMP INHIBITORS (PPI)",
   "back": {
    "cagr_5y_pct": -14.4248,
    "ms_first_period_pct": 1.1456,
    "period_first": "2021-01",
    "sales_first_period_krw": 659622706.6999
   },
   "back_extended": {
    "atc_count": 1,
    "brand_cagr_5y_pct": -14.4248,
    "direct_competition_count": 116,
    "excess_growth_pct": -24.4048,
    "is_dual_source": false,
    "market_cagr_5y_pct": 9.98,
    "market_definition_full": "A2B2",
    "market_definition_label": "1 ATC",
    "market_label_kor": "위궤양/PPI",
    "market_size_recent": 97133049963.03,
    "source_label": "UBIST",
    "sources": [
     "UBIST"
    ]
   },
   "brand": "라베칸",
   "company": "녹십자",
   "front": {
    "default_source": "UBIST",
    "gr_mom_pct": -15.0741,
    "gr_qoq_pct": -4.9002,
    "gr_yoy_mat_pct": -8.8746,
    "gr_yoy_pct": 0.2722,
    "gr_yoy_ym_pct": 0.2722,
    "ms_change_yoy_pct": -0.0235,
    "ms_recent_pct": 0.3154,
    "sources_data": {
     "UBIST": {
      "gr_mom_pct": -15.0741,
      "gr_qoq_pct": -4.9002,
      "gr_yoy_mat_pct": -8.8746,
      "gr_yoy_pct": 0.2722,
      "gr_yoy_ym_pct": 0.2722,
      "measure": "sales",
      "ms_change_yoy_pct": -0.0235,
      "ms_recent_pct": 0.3154,
      "unit_label": "KRW",
      "value_recent": 306414433.41
     }
    },
    "value_recent": 306414433.41
   },
   "is_jw": true,
   "is_target": true,
   "market_id": "strategy_001",
   "market_name": "라베칸/라베칸듀오",
   "market_name_short": "라베칸",
   "mkt_team": "MKT 1팀",
   "nhi_type": "NHI",
   "rank": 54,
   "sources": [
    "UBIST"
   ],
   "total_brands_in_market": 391
  },
  "...(25개 중 1개)"
 ],
 "kpi_summary": {
  "IQVIA": {
   "avg_cagr_5y_pct": 12.0553,
   "avg_ms_per_brand_pct": 12.07,
   "avg_yoy_pct": 23.44,
   "brand_count": 14,
   "gr_yoy_mat_pct": 75.66,
   "gr_yoy_pct": 23.44,
   "gr_yoy_ym_pct": 23.44,
   "ms_change_yoy_pct": 1.34,
   "period_recent": "2026-Q1",
   "sales_down_count": 8,
   "sales_up_count": 6,
   "total_sales_recent_krw": 42199652302.0
  },
  "UBIST": {
   "avg_cagr_5y_pct": 14.0136,
   "avg_ms_per_brand_pct": 4.37,
   "avg_yoy_pct": -1.9,
   "brand_count": 14,
   "gr_yoy_mat_pct": 21.36,
   "gr_yoy_pct": -1.9,
   "gr_yoy_ym_pct": -1.9,
   "ms_change_yoy_pct": -1.11,
   "period_recent": "2026-05",
   "sales_down_count": 10,
   "sales_up_count": 4,
   "total_sales_recent_krw": 25446694191.02
  }
 }
}
```

## brands_default
- 요청: `GET /api/brands`
- ts_utc: 2026-07-17T09:16:33.309694Z  | status: 200  | bytes: 10801
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
[
 {
  "atc_codes": [
   "A2B2"
  ],
  "atc_desc": "PROTON PUMP INHIBITORS (PPI)",
  "brand": "라베칸",
  "general_sources": [
   "UBIST",
   "...(2개 중 1개)"
  ],
  "is_dual_source": false,
  "is_jw": true,
  "is_target": true,
  "market_id": "strategy_001",
  "market_label_kor": "위궤양/PPI",
  "market_name": "라베칸/라베칸듀오",
  "market_name_short": "라베칸",
  "mkt_team": "MKT 1팀",
  "rank": 1,
  "sources": [
   "UBIST",
   "...(2개 중 1개)"
  ],
  "strategic_sources": [
   "UBIST"
  ]
 },
 "...(25개 중 1개)"
]
```

## brands_search
- 요청: `GET /api/brands?q=%EB%A6%AC%EB%B0%94%EB%A1%9C&limit=5`
- ts_utc: 2026-07-17T09:16:33.313633Z  | status: 200  | bytes: 479
- resp headers: `{"content-type": "application/json", "x-has-more": "false", "x-total-matches": "1", "x-result-limit": "5"}`
- 응답(구조 보존 요약):
```json
[
 {
  "brand": "리바로",
  "sources": [
   "UBIST",
   "...(2개 중 1개)"
  ],
  "strategic_sources": [
   "UBIST"
  ],
  "general_sources": [
   "UBIST",
   "...(2개 중 1개)"
  ],
  "contexts": [
   {
    "view_kind": "general",
    "market_id": "C10A1",
    "market_name": "STATINS (HMG-COA RED)",
    "has_market_data": true
   },
   "...(3개 중 1개)"
  ],
  "is_jw_target": true
 }
]
```

## cause_ml
- 요청: `GET /api/cause/%EB%A6%AC%EB%B0%94%EB%A1%9C?view=market_landscape&source=UBIST&measure=sales`
- ts_utc: 2026-07-17T09:16:38.343817Z  | status: 200  | bytes: 2256903
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "brand": "리바로",
 "brand_name": "리바로",
 "brand_key": "리바로",
 "market_id": "strategy_006",
 "view": "market_landscape",
 "source": "UBIST",
 "measure": "sales",
 "unit_label": "KRW",
 "data": {
  "kpi": {
   "market_size_recent": 213925043319.3602,
   "market_cagr_5y_pct": 9.3677,
   "top3_share_pct": 20.37,
   "hhi_recent": 262.4174,
   "direct_competition_count": 566,
   "target_brand": "리바로",
   "target_company": "JW중외제약",
   "target_ei": 41.6922,
   "ei": 41.6922,
   "ei_basis": "endpoint_5y",
   "ei_period_years": 5,
   "ei_note": null,
   "brand_cagr_pct": 3.9056,
   "market_cagr_pct": 9.3677,
   "target_momentum": -0.0165,
   "target_rank": 6,
   "target_share_pct": 3.7577,
   "brand_value_recent": 8038598793.61,
   "brand_share_pct": 3.7577,
   "momentum_score": -0.0165
  },
  "sources_data": {
   "periods_unit": "월간",
   "periods_count": 65,
   "market_size_series": [
    {
     "period": "2021-01",
     "value": 127640243701.1299,
     "yoy_growth_pct": null,
     "mom_growth_pct": null,
     "sales_krw": 127640243701.1299
    },
    "...(65개 중 1개)"
   ],
   "market_yoy_series": {
    "2021-01": null,
    "2021-02": null,
    "2021-03": null,
    "2021-04": null,
    "2021-05": null,
    "2021-06": null,
    "2021-07": null,
    "2021-08": null,
    "2021-09": null,
    "2021-10": null,
    "2021-11": null,
    "2021-12": null,
    "2022-01": 16.6287,
    "2022-02": 10.9046,
    "2022-03": 7.9755,
    "2022-04": 9.7277,
    "2022-05": 13.1914,
    "2022-06": 6.841,
    "2022-07": 8.6149,
    "2022-08": 13.9455,
    "2022-09": 11.6859,
    "2022-10": 12.2422,
    "2022-11": 12.1226,
    "2022-12": 9.5857,
    "2023-01": 10.8629,
    "2023-02": 16.4942,
    "2023-03": 14.9287,
    "2023-04": 9.3178,
    "2023-05": 14.7817,
    "2023-06": 15.7687,
    "2023-07": 11.6095,
    "2023-08": 10.0462,
    "2023-09": 8.321,
    "2023-10": 11.4617,
    "2023-11": 9.6464,
    "2023-12": 4.479,
    "2024-01": 14.2211,
    "2024-02": 10.4332,
    "2024-03": 3.0914,
    "2024-04": 15.4846,
    "2024-05": 8.024,
    "2024-06": 2.2878,
    "2024-07": 15.5467,
    "2024-08": 9.8046,
    "2024-09": 11.6258,
    "2024-10": 15.441,
    "2024-11": 8.84,
    "2024-12": 17.2357,
    "2025-01": 4.8956,
    "2025-02": 11.867,
    "2025-03": 10.5948,
    "2025-04": 10.5205,
    "2025-05": 6.3235,
    "2025-06": 9.7876,
    "2025-07": 6.7422,
    "2025-08": 2.0217,
    "2025-09": 19.806,
    "2025-10": -2.4148,
    "2025-11": 2.7878,
    "2025-12": 8.1636,
    "2026-01": 10.1089,
    "2026-02": 0.0036,
    "2026-03": 13.5802,
    "2026-04": 7.4502,
    "2026-05": 4.86
   },
   "market_yoy_recent_pct": 4.86,
   "hhi_series_5y": [
    {
     "period": "2021",
     "period_full": "2021",
     "year": 2021,
     "hhi": 329.4089
    },
    "...(5개 중 1개)"
   ],
   "hhi_recent": 262.4174,
   "cagr_5y_pct": 9.3677
  },
  "market_size_series": [
   {
    "period": "2021-01",
    "value": 127640243701.1299,
    "yoy_growth_pct": null,
    "mom_growth_pct": null,
    "sales_krw": 127640243701.1299
   },
   "...(65개 중 1개)"
  ],
  "market_yoy_series": {
   "2021-01": null,
   "2021-02": null,
   "2021-03": null,
   "2021-04": null,
   "2021-05": null,
   "2021-06": null,
   "2021-07": null,
   "2021-08": null,
   "2021-09": null,
   "2021-10": null,
   "2021-11": null,
   "2021-12": null,
   "2022-01": 16.6287,
   "2022-02": 10.9046,
   "2022-03": 7.9755,
   "2022-04": 9.7277,
   "2022-05": 13.1914,
   "2022-06": 6.841,
   "2022-07": 8.6149,
   "2022-08": 13.9455,
   "2022-09": 11.6859,
   "2022-10": 12.2422,
   "2022-11": 12.1226,
   "2022-12": 9.5857,
   "2023-01": 10.8629,
   "2023-02": 16.4942,
   "2023-03": 14.9287,
   "2023-04": 9.3178,
   "2023-05": 14.7817,
   "2023-06": 15.7687,
   "2023-07": 11.6095,
   "2023-08": 10.0462,
   "2023-09": 8.321,
   "2023-10": 11.4617,
   "2023-11": 9.6464,
   "2023-12": 4.479,
   "2024-01": 14.2211,
   "2024-02": 10.4332,
   "2024-03": 3.0914,
   "2024-04": 15.4846,
   "2024-05": 8.024,
   "2024-06": 2.2878,
   "2024-07": 15.5467,
   "2024-08": 9.8046,
   "2024-09": 11.6258,
   "2024-10": 15.441,
   "2024-11": 8.84,
   "2024-12": 17.2357,
   "2025-01": 4.8956,
   "2025-02": 11.867,
   "2025-03": 10.5948,
   "2025-04": 10.5205,
   "2025-05": 6.3235,
   "2025-06": 9.7876,
   "2025-07": 6.7422,
   "2025-08": 2.0217,
   "2025-09": 19.806,
   "2025-10": -2.4148,
   "2025-11": 2.7878,
   "2025-12": 8.1636,
   "2026-01": 10.1089,
   "2026-02": 0.0036,
   "2026-03": 13.5802,
   "2026-04": 7.4502,
   "2026-05": 4.86
  },
  "market_yoy_recent_pct": 4.86,
  "hhi_series_5y": [
   {
    "period": "2021",
    "period_full": "2021",
    "year": 2021,
    "hhi": 329.4089
   },
   "...(5개 중 1개)"
  ],
  "hhi_recent": 262.4174,
  "brand_ranking": {
   "years": [
    2022,
    "...(5개 중 1개)"
   ],
   "yearly": [
    {
     "year": 2022,
     "rankings": [
      {
       "brand": "리피토",
       "company": "비아트리스",
       "is_target": false,
       "is_jw": false,
       "is_others": false,
       "value": 202784072212.55,
       "rank": 1,
       "ms_pct": 10.7666
      },
      "...(11개 중 1개)"
     ]
    },
    "...(5개 중 1개)"
   ],
   "brands": [
    {
     "brand": "리바로",
     "company": "JW중외제약",
     "is_target": true,
     "is_jw": true,
     "yearly_values": [
      {
       "year": 2022,
       "value": 86330525946.61,
       "ms_pct": 4.5836,
       "rank": 5
      },
      "...(5개 중 1개)"
     ]
    },
    "...(7개 중 1개)"
   ],
   "top_brands": [
    "리바로",
    "...(7개 중 1개)"
   ],
   "series": {
    "리피토": [
     202784072212.55,
     "...(5개 중 1개)"
    ],
    "로수젯": [
     149882765192.4999,
     "...(5개 중 1개)"
    ],
    "아토젯": [
     90766214242.2099,
     "...(5개 중 1개)"
    ],
    "크레스토": [
     90641312907.8099,
     "...(5개 중 1개)"
    ],
    "리바로": [
     86330525946.61,
     "...(5개 중 1개)"
    ],
    "로수바미브": [
     67733398163.7499,
     "...(5개 중 1개)"
    ],
    "아토르바": [
     39483369046.36,
     "...(5개 중 1개)"
    ],
    "리피로우": [
     36256137062.14,
     "...(5개 중 1개)"
    ],
 
...(요약 절단)
```

## cause_cd
- 요청: `GET /api/cause/%EB%A6%AC%EB%B0%94%EB%A1%9C?view=competitive_dynamics&source=UBIST&measure=sales`
- ts_utc: 2026-07-17T09:16:38.491576Z  | status: 200  | bytes: 2256918
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "brand": "리바로",
 "brand_name": "리바로",
 "brand_key": "리바로",
 "market_id": "strategy_006",
 "view": "competitive_dynamics",
 "source": "UBIST",
 "measure": "sales",
 "unit_label": "KRW",
 "data": {
  "kpi": {
   "market_size_recent": 213925043319.3602,
   "market_cagr_5y_pct": 9.3677,
   "top3_share_pct": 20.37,
   "hhi_recent": 262.4174,
   "direct_competition_count": 566,
   "target_brand": "리바로",
   "target_company": "JW중외제약",
   "target_ei": 41.6922,
   "ei": 41.6922,
   "ei_basis": "endpoint_5y",
   "ei_period_years": 5,
   "ei_note": null,
   "brand_cagr_pct": 3.9056,
   "market_cagr_pct": 9.3677,
   "target_momentum": -0.0165,
   "target_rank": 6,
   "target_share_pct": 3.7577,
   "brand_value_recent": 8038598793.61,
   "brand_share_pct": 3.7577,
   "momentum_score": -0.0165
  },
  "sources_data": {
   "periods_unit": "월간",
   "periods_count": 65,
   "market_size_series": [
    {
     "period": "2021-01",
     "value": 127640243701.1299,
     "yoy_growth_pct": null,
     "mom_growth_pct": null,
     "sales_krw": 127640243701.1299
    },
    "...(65개 중 1개)"
   ],
   "market_yoy_series": {
    "2021-01": null,
    "2021-02": null,
    "2021-03": null,
    "2021-04": null,
    "2021-05": null,
    "2021-06": null,
    "2021-07": null,
    "2021-08": null,
    "2021-09": null,
    "2021-10": null,
    "2021-11": null,
    "2021-12": null,
    "2022-01": 16.6287,
    "2022-02": 10.9046,
    "2022-03": 7.9755,
    "2022-04": 9.7277,
    "2022-05": 13.1914,
    "2022-06": 6.841,
    "2022-07": 8.6149,
    "2022-08": 13.9455,
    "2022-09": 11.6859,
    "2022-10": 12.2422,
    "2022-11": 12.1226,
    "2022-12": 9.5857,
    "2023-01": 10.8629,
    "2023-02": 16.4942,
    "2023-03": 14.9287,
    "2023-04": 9.3178,
    "2023-05": 14.7817,
    "2023-06": 15.7687,
    "2023-07": 11.6095,
    "2023-08": 10.0462,
    "2023-09": 8.321,
    "2023-10": 11.4617,
    "2023-11": 9.6464,
    "2023-12": 4.479,
    "2024-01": 14.2211,
    "2024-02": 10.4332,
    "2024-03": 3.0914,
    "2024-04": 15.4846,
    "2024-05": 8.024,
    "2024-06": 2.2878,
    "2024-07": 15.5467,
    "2024-08": 9.8046,
    "2024-09": 11.6258,
    "2024-10": 15.441,
    "2024-11": 8.84,
    "2024-12": 17.2357,
    "2025-01": 4.8956,
    "2025-02": 11.867,
    "2025-03": 10.5948,
    "2025-04": 10.5205,
    "2025-05": 6.3235,
    "2025-06": 9.7876,
    "2025-07": 6.7422,
    "2025-08": 2.0217,
    "2025-09": 19.806,
    "2025-10": -2.4148,
    "2025-11": 2.7878,
    "2025-12": 8.1636,
    "2026-01": 10.1089,
    "2026-02": 0.0036,
    "2026-03": 13.5802,
    "2026-04": 7.4502,
    "2026-05": 4.86
   },
   "market_yoy_recent_pct": 4.86,
   "hhi_series_5y": [
    {
     "period": "2021",
     "period_full": "2021",
     "year": 2021,
     "hhi": 329.4089
    },
    "...(5개 중 1개)"
   ],
   "hhi_recent": 262.4174,
   "cagr_5y_pct": 9.3677
  },
  "market_size_series": [
   {
    "period": "2021-01",
    "value": 127640243701.1299,
    "yoy_growth_pct": null,
    "mom_growth_pct": null,
    "sales_krw": 127640243701.1299
   },
   "...(65개 중 1개)"
  ],
  "market_yoy_series": {
   "2021-01": null,
   "2021-02": null,
   "2021-03": null,
   "2021-04": null,
   "2021-05": null,
   "2021-06": null,
   "2021-07": null,
   "2021-08": null,
   "2021-09": null,
   "2021-10": null,
   "2021-11": null,
   "2021-12": null,
   "2022-01": 16.6287,
   "2022-02": 10.9046,
   "2022-03": 7.9755,
   "2022-04": 9.7277,
   "2022-05": 13.1914,
   "2022-06": 6.841,
   "2022-07": 8.6149,
   "2022-08": 13.9455,
   "2022-09": 11.6859,
   "2022-10": 12.2422,
   "2022-11": 12.1226,
   "2022-12": 9.5857,
   "2023-01": 10.8629,
   "2023-02": 16.4942,
   "2023-03": 14.9287,
   "2023-04": 9.3178,
   "2023-05": 14.7817,
   "2023-06": 15.7687,
   "2023-07": 11.6095,
   "2023-08": 10.0462,
   "2023-09": 8.321,
   "2023-10": 11.4617,
   "2023-11": 9.6464,
   "2023-12": 4.479,
   "2024-01": 14.2211,
   "2024-02": 10.4332,
   "2024-03": 3.0914,
   "2024-04": 15.4846,
   "2024-05": 8.024,
   "2024-06": 2.2878,
   "2024-07": 15.5467,
   "2024-08": 9.8046,
   "2024-09": 11.6258,
   "2024-10": 15.441,
   "2024-11": 8.84,
   "2024-12": 17.2357,
   "2025-01": 4.8956,
   "2025-02": 11.867,
   "2025-03": 10.5948,
   "2025-04": 10.5205,
   "2025-05": 6.3235,
   "2025-06": 9.7876,
   "2025-07": 6.7422,
   "2025-08": 2.0217,
   "2025-09": 19.806,
   "2025-10": -2.4148,
   "2025-11": 2.7878,
   "2025-12": 8.1636,
   "2026-01": 10.1089,
   "2026-02": 0.0036,
   "2026-03": 13.5802,
   "2026-04": 7.4502,
   "2026-05": 4.86
  },
  "market_yoy_recent_pct": 4.86,
  "hhi_series_5y": [
   {
    "period": "2021",
    "period_full": "2021",
    "year": 2021,
    "hhi": 329.4089
   },
   "...(5개 중 1개)"
  ],
  "hhi_recent": 262.4174,
  "brand_ranking": {
   "years": [
    2022,
    "...(5개 중 1개)"
   ],
   "yearly": [
    {
     "year": 2022,
     "rankings": [
      {
       "brand": "리피토",
       "company": "비아트리스",
       "is_target": false,
       "is_jw": false,
       "is_others": false,
       "value": 202784072212.55,
       "rank": 1,
       "ms_pct": 10.7666
      },
      "...(11개 중 1개)"
     ]
    },
    "...(5개 중 1개)"
   ],
   "brands": [
    {
     "brand": "리바로",
     "company": "JW중외제약",
     "is_target": true,
     "is_jw": true,
     "yearly_values": [
      {
       "year": 2022,
       "value": 86330525946.61,
       "ms_pct": 4.5836,
       "rank": 5
      },
      "...(5개 중 1개)"
     ]
    },
    "...(7개 중 1개)"
   ],
   "top_brands": [
    "리바로",
    "...(7개 중 1개)"
   ],
   "series": {
    "리피토": [
     202784072212.55,
     "...(5개 중 1개)"
    ],
    "로수젯": [
     149882765192.4999,
     "...(5개 중 1개)"
    ],
    "아토젯": [
     90766214242.2099,
     "...(5개 중 1개)"
    ],
    "크레스토": [
     90641312907.8099,
     "...(5개 중 1개)"
    ],
    "리바로": [
     86330525946.61,
     "...(5개 중 1개)"
    ],
    "로수바미브": [
     67733398163.7499,
     "...(5개 중 1개)"
    ],
    "아토르바": [
     39483369046.36,
     "...(5개 중 1개)"
    ],
    "리피로우": [
     36256137062.14,
     "...(5개 중 1개)"
    
...(요약 절단)
```

## cause_404
- 요청: `GET /api/cause/%EC%97%86%EB%8A%94%EB%B8%8C%EB%9E%9C%EB%93%9C`
- ts_utc: 2026-07-17T09:16:38.610590Z  | status: 404  | bytes: 64
- 응답(구조 보존 요약):
```json
{
 "detail": {
  "error": "brand_not_found",
  "brand": "없는브랜드"
 }
}
```

## deep_strategic
- 요청: `GET /api/deep-analysis/%EB%A6%AC%EB%B0%94%EB%A1%9C`
- ts_utc: 2026-07-17T09:16:51.783061Z  | status: 200  | bytes: 571929
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "brand": "리바로",
 "brand_name": "리바로",
 "market_id": "strategy_006",
 "market_name": "리바로 리바로젯",
 "available_combos": [
  "UBIST.sales",
  "...(2개 중 1개)"
 ],
 "data": {
  "forecast": {
   "backtest_available": true,
   "by_combo": {
    "UBIST.sales": {
     "baseline": {
      "ms_recent_pct": 3.7576,
      "value_recent": 8038598793.61
     },
     "brands": [
      {
       "baseline": {
        "ms_recent_pct": 3.7576,
        "value_recent": 8038598793.61
       },
       "brand": "리바로",
       "company": null,
       "confidence": {
        "ci_width_absolute": 1032758272.524,
        "ci_width_relative_pct": 12.8474,
        "label": "높음",
        "method": "ci_width_normalized",
        "score": 80
       },
       "forecast_intervals": {
        "ci_accumulation_guard_applied": true,
        "ci_lower_95": [
         8038598793.61,
         "...(60개 중 1개)"
        ],
        "ci_upper_95": [
         8038598793.61,
         "...(60개 중 1개)"
        ],
        "funnel_floor_applied": true,
        "funnel_meta": {
         "funnel_applied_steps": 49,
         "funnel_floor_coefficient": "2 * 1.96 * residual_std * sqrt(t / seasonality)",
         "funnel_total_steps": 60,
         "residual_std": 263458742.9908,
         "seasonality": 12
        },
        "lower_95_natural": [
         8038598793.61,
         "...(60개 중 1개)"
        ],
        "lower_floor_applied": false,
        "upper_95_natural": [
         8038598793.61,
         "...(60개 중 1개)"
        ]
       },
       "forecast_model": {
        "event_regressor": {
         "enabled": false,
         "limitations": [
          "event_regressor_disabled_phase_30",
          "...(2개 중 1개)"
         ],
         "max_regressors": 0,
         "mode": "proxy_light",
         "regressors": []
        },
        "fit_quality": {
         "backtest_available": true,
         "mape_backtest_3m": 3.2975,
         "residual_std": 281512599.7037
        },
        "name": "HoltWinters",
        "params": {
         "damped_trend": true,
         "seasonal": "add",
         "seasonal_periods": 12,
         "trend": "add"
        },
        "selection_policy": "data_size_dispatch_v1",
        "selection_reason": "prophet_fallback_to_holtwinters",
        "variant": "damped"
       },
       "forecast_ms_pct": [
        3.7577,
        "...(60개 중 1개)"
       ],
       "forecast_values": [
        8038598793.61,
        "...(60개 중 1개)"
       ],
       "forecast_warnings": [
        "prophet_fit_failed_fallback:ModuleNotFoundError"
       ],
       "history_ms_pct": [
        5.0341,
        "...(65개 중 1개)"
       ],
       "history_periods": [
        "2021-01",
        "...(65개 중 1개)"
       ],
       "history_values": [
        6425562578.27,
        "...(65개 중 1개)"
       ],
       "is_jw": true,
       "is_target": true,
       "rank": 6
      },
      "...(6개 중 1개)"
     ],
     "forecast_periods": [
      "2026-05",
      "...(60개 중 1개)"
     ],
     "history_periods": [
      "2021-01",
      "...(65개 중 1개)"
     ],
     "period_unit": "월",
     "target_brand": "리바로",
     "unit_label": "KRW"
    },
    "UBIST.volume": {
     "baseline": {
      "ms_recent_pct": 5.0573,
      "value_recent": 14603081.4099
     },
     "brands": [
      {
       "baseline": {
        "ms_recent_pct": 5.0573,
        "value_recent": 14603081.4099
       },
       "brand": "리바로",
       "company": null,
       "confidence": {
        "ci_width_absolute": 6971461.5006,
        "ci_width_relative_pct": 47.7396,
        "label": "보통",
        "method": "ci_width_normalized",
        "score": 65
       },
       "forecast_intervals": {
        "ci_accumulation_guard_applied": true,
        "ci_lower_95": [
         14603081.4099,
         "...(60개 중 1개)"
        ],
        "ci_upper_95": [
         14603081.4099,
         "...(60개 중 1개)"
        ],
        "funnel_floor_applied": false,
        "funnel_meta": {
         "funnel_applied_steps": 0,
         "funnel_floor_coefficient": "2 * 1.96 * residual_std * sqrt(t / seasonality)",
         "funnel_total_steps": 60,
         "residual_std": 472875.9969,
         "seasonality": 12
        },
        "lower_95_natural": [
         14603081.4099,
         "...(60개 중 1개)"
        ],
        "lower_floor_applied": true,
        "upper_95_natural": [
         14603081.4099,
         "...(60개 중 1개)"
        ]
       },
       "forecast_model": {
        "event_regressor": {
         "enabled": false,
         "limitations": [
          "event_regressor_disabled_phase_30",
          "...(2개 중 1개)"
         ],
         "max_regressors": 0,
         "mode": "proxy_light",
         "regressors": []
        },
        "fit_quality": {
         "backtest_available": true,
         "mape_backtest_3m": 4.3877,
         "residual_std": 509317.6282
        },
        "name": "HoltWinters",
        "params": {
         "damped_trend": true,
         "seasonal": "add",
         "seasonal_periods": 12,
         "trend": "add"
        },
        "selection_policy": "data_size_dispatch_v1",
        "selection_reason": "prophet_fallback_to_holtwinters",
        "variant": "damped"
       },
       "forecast_ms_pct": [
        5.0573,
        "...(60개 중 1개)"
       ],
       "forecast_values": [
        14603081.4099,
        "...(60개 중 1개)"
       ],
       "forecast_warnings": [
        "prophet_fit_failed_fallback:ModuleNotFoundError"
       ],
       "history_ms_pct": [
        6.0068,
        "...(65개 중 1개)"
       ],
       "history_periods": [
        "2021-01",
        "...(65개 중 1개)"
       ],
       "history_values": [
        10845337.7,
        "...(65개 중 1개)"
       ],
       "is_jw": true,
       "is_target": true,
       "rank": 3
      },
      "...(6개 중 1개)"
     ],
     "forecast_periods": [
      "2026-05",
      "...(60개 중 1개)"
     ],
     "history_periods": [
      "2021-01",
      "...(65개 중 1개)"
     ],
     "period_unit": "월",
     "target_brand": "리바로",
     "unit_label": "Rx"
    }
   },
   "disc
...(요약 절단)
```

## deep_general
- 요청: `GET /api/deep-analysis/%EB%A6%AC%EB%B0%94%EB%A1%9C?view=general`
- ts_utc: 2026-07-17T09:16:52.718342Z  | status: 200  | bytes: 755794
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "available_combos": [
  "IQVIA.counting_unit",
  "...(6개 중 1개)"
 ],
 "brand": "리바로",
 "brand_key": "리바로",
 "brand_name": "리바로",
 "data": {
  "events": [
   {
    "id": "889fb8f4f3bc8c30",
    "category": "rd",
    "category_label": "신약/R&D",
    "date": "2026-05-04",
    "period_map": {
     "UBIST": "2026-05",
     "IQVIA": "2026-Q2"
    },
    "impact_score": 82,
    "title": "리바로 구강붕해정 개발 경쟁 가열...JW중외도 가세",
    "summary": "JW중외제약이 고지혈증 치료제 '리바로'의 구강붕해정 제형 개발을 위한 임상 1상 시험계획을 승인받으며, 후발 제약사들의 도전에 대응한 시장 수성 전략을 본격화했다.",
    "body_full": "JW중외제약이 간판 고지혈증 치료제 '리바로(성분명 피타바스타틴)'의 제형 다변화에 본격 착수했다. 최근 비씨월드제약과 파마비전 등 후발 제약사들이 물 없이 복용 가능한 구강붕해정(ODT)을 앞세워 시장 도전에 나서자, 오리지널사로서 직접 동일 제형 개발에 나서며 방어막을 치는 모습이다.\n\n4일 관련 업계에 따르면 식품의약품안전처는 지난 4월 30일 JW중외제약의 피타바스타틴 구강붕해정 제형에 대한 임상 1상 시험계획(IND)을 승인했다. 이번 임상은 건강한 성인을 대상으로 기존 정제인 '리바로정'과 새롭게 개발 중인 구강붕해정 제형 간의 생물학적 동등성 및 안전성을 비교하기 위해 진행된다.\n\n구강붕해정은 입안에서 침에 의해 빠르게 녹는 제형으로, 알약을 삼키기 어려운 연하곤란 환자나 고령층, 혹은 물을 마...(1365 chars)",
    "source": "데일리팜",
    "url": "https://www.dailypharm.com/user/news/338121",
    "source_url": "https://www.dailypharm.com/user/news/338121",
    "related_coverage_count": null,
    "related_sources": null,
    "related_titles": null,
    "related_urls": null,
    "on_list": true,
    "on_chart": true
   },
   "...(47개 중 1개)"
  ],
  "forecast": {
   "backtest_available": true,
   "by_combo": {
    "IQVIA.counting_unit": {
     "baseline": {
      "ms_recent_pct": 9.4016,
      "value_recent": 34255000.0
     },
     "brands": [
      {
       "baseline": {
        "ms_recent_pct": 9.4016,
        "value_recent": 34255000.0
       },
       "brand": "리바로",
       "company": null,
       "confidence": {
        "ci_width_absolute": 3421844.8907,
        "ci_width_relative_pct": 9.9893,
        "label": "매우높음",
        "method": "ci_width_normalized",
        "score": 95
       },
       "forecast_intervals": {
        "ci_accumulation_guard_applied": false,
        "ci_lower_95": [
         34255000.0,
         "...(20개 중 1개)"
        ],
        "ci_upper_95": [
         34255000.0,
         "...(20개 중 1개)"
        ],
        "funnel_floor_applied": true,
        "funnel_meta": {
         "funnel_applied_steps": 18,
         "funnel_floor_coefficient": "2 * 1.96 * residual_std * sqrt(t / seasonality)",
         "funnel_total_steps": 20,
         "residual_std": 872919.615,
         "seasonality": 4
        },
        "lower_95_natural": [
         34255000.0,
         "...(20개 중 1개)"
        ],
        "lower_floor_applied": false,
        "upper_95_natural": [
         34255000.0,
         "...(20개 중 1개)"
        ]
       },
       "forecast_model": {
        "event_regressor": {
         "enabled": false,
         "limitations": [
          "event_regressor_disabled_phase_30",
          "...(2개 중 1개)"
         ],
         "max_regressors": 0,
         "mode": "proxy_light",
         "regressors": []
        },
        "fit_quality": {
         "backtest_available": true,
         "mape_backtest_3m": 5.8784,
         "residual_std": 876076.2942
        },
        "name": "HoltWinters",
        "params": {
         "damped_trend": true,
         "seasonal": "add",
         "seasonal_periods": 4,
         "trend": "add"
        },
        "selection_policy": "data_size_dispatch_v1",
        "selection_reason": "data_size_20_quarters_supports_holtwinters_damped",
        "variant": "damped"
       },
       "forecast_ms_pct": [
        9.4016,
        "...(20개 중 1개)"
       ],
       "forecast_values": [
        34255000.0,
        "...(20개 중 1개)"
       ],
       "forecast_warnings": [],
       "history_ms_pct": [
        7.648,
        "...(20개 중 1개)"
       ],
       "history_periods": [
        "2021-Q2",
        "...(20개 중 1개)"
       ],
       "history_values": [
        24373360.0,
        "...(20개 중 1개)"
       ],
       "is_jw": false,
       "is_target": true,
       "rank": 3
      },
      "...(6개 중 1개)"
     ],
     "forecast_periods": [
      "2026-Q1",
      "...(20개 중 1개)"
     ],
     "history_periods": [
      "2021-Q2",
      "...(20개 중 1개)"
     ],
     "period_unit": "분기",
     "target_brand": "리바로",
     "unit_label": "counting unit"
    },
    "IQVIA.dosage_unit": {
     "baseline": {
      "ms_recent_pct": 9.4016,
      "value_recent": 34255000.0
     },
     "brands": [
      {
       "baseline": {
        "ms_recent_pct": 9.4016,
        "value_recent": 34255000.0
       },
       "brand": "리바로",
       "company": null,
       "confidence": {
        "ci_width_absolute": 3421844.8907,
        "ci_width_relative_pct": 9.9893,
        "label": "매우높음",
        "method": "ci_width_normalized",
        "score": 95
       },
       "forecast_intervals": {
        "ci_accumulation_guard_applied": false,
        "ci_lower_95": [
         34255000.0,
         "...(20개 중 1개)"
        ],
        "ci_upper_95": [
         34255000.0,
         "...(20개 중 1개)"
        ],
        "funnel_floor_applied": true,
        "funnel_meta": {
         "funnel_applied_steps": 18,
         "funnel_floor_coefficient": "2 * 1.96 * residual_std * sqrt(t / seasonality)",
         "funnel_total_steps": 20,
         "residual_std": 872919.615,
         "seasonality": 4
        },
        "lower_95_natural": [
         34255000.0,
         "...(20개 중 1개)"
        ],
        "lower_floor_applied": false,
        "upper_95_natural": [
         34255000.0,
         "...(20개 중 1개)"
        ]
       },
       "forecast_model": {
        "event_regressor": {
         "enabled": false,
         "limitations": [
          "event_regressor_disabled_phase_30",
          "...(2개 중 1개)"
         ],
         "max_regressors": 0,
         "mode": "proxy_light",
         "regressors": []
        },
        "fit_quality": {
         "backtest_available": true,
         "mape_backtest_3m": 5.8784,
         "residual_std": 876076.2942
        },
        "name": "HoltWinters",
        "params": {
         "damped_trend": true,
         "seasonal": "add",
         "
...(요약 절단)
```

## deep_formal_ml
- 요청: `GET /api/deep-analysis/%EB%A6%AC%EB%B0%94%EB%A1%9C?view_kind=strategic_ml&source=ubist`
- ts_utc: 2026-07-17T09:16:53.644828Z  | status: 200  | bytes: 591184
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "brand": "리바로",
 "brand_name": "리바로",
 "brand_key": "리바로",
 "view_kind": "strategic_ml",
 "source": "ubist",
 "market_id": "ml_006",
 "market_name": "리바로 리바로젯",
 "data": {
  "forecast": {
   "backtest_available": true,
   "by_combo": {
    "UBIST.sales": {
     "baseline": {
      "ms_recent_pct": 3.757670756485253,
      "value_recent": 8038598793.61
     },
     "brands": [
      {
       "baseline": {
        "ms_recent_pct": 3.757670756485253,
        "value_recent": 8038598793.61
       },
       "brand": "리바로",
       "company": null,
       "confidence": {
        "ci_width_absolute": 1032758272.5240774,
        "ci_width_relative_pct": 12.847491198901881,
        "label": "높음",
        "method": "ci_width_normalized",
        "score": 80
       },
       "forecast_intervals": {
        "ci_accumulation_guard_applied": true,
        "ci_lower_95": [
         8038598793.61,
         "...(60개 중 1개)"
        ],
        "ci_upper_95": [
         8038598793.61,
         "...(60개 중 1개)"
        ],
        "funnel_floor_applied": true,
        "funnel_meta": {
         "funnel_applied_steps": 49,
         "funnel_floor_coefficient": "2 * 1.96 * residual_std * sqrt(t / seasonality)",
         "funnel_total_steps": 60,
         "residual_std": 263458742.99083596,
         "seasonality": 12
        },
        "lower_95_natural": [
         8038598793.61,
         "...(60개 중 1개)"
        ],
        "lower_floor_applied": false,
        "upper_95_natural": [
         8038598793.61,
         "...(60개 중 1개)"
        ]
       },
       "forecast_model": {
        "event_regressor": {
         "enabled": false,
         "limitations": [
          "event_regressor_disabled_phase_30",
          "...(2개 중 1개)"
         ],
         "max_regressors": 0,
         "mode": "proxy_light",
         "regressors": []
        },
        "fit_quality": {
         "backtest_available": true,
         "mape_backtest_3m": 3.2975672676096877,
         "residual_std": 281512599.70377713
        },
        "name": "HoltWinters",
        "params": {
         "damped_trend": true,
         "seasonal": "add",
         "seasonal_periods": 12,
         "trend": "add"
        },
        "selection_policy": "data_size_dispatch_v1",
        "selection_reason": "prophet_fallback_to_holtwinters",
        "variant": "damped"
       },
       "forecast_ms_pct": [
        3.7577,
        "...(60개 중 1개)"
       ],
       "forecast_values": [
        8038598793.61,
        "...(60개 중 1개)"
       ],
       "forecast_warnings": [
        "prophet_fit_failed_fallback:ModuleNotFoundError"
       ],
       "history_ms_pct": [
        5.0341,
        "...(65개 중 1개)"
       ],
       "history_periods": [
        "2021-01",
        "...(65개 중 1개)"
       ],
       "history_values": [
        6425562578.27,
        "...(65개 중 1개)"
       ],
       "is_jw": true,
       "is_target": true,
       "rank": 6
      },
      "...(6개 중 1개)"
     ],
     "forecast_periods": [
      "2026-05",
      "...(60개 중 1개)"
     ],
     "history_periods": [
      "2021-01",
      "...(65개 중 1개)"
     ],
     "period_unit": "월",
     "target_brand": "리바로",
     "unit_label": "KRW"
    },
    "UBIST.volume": {
     "baseline": {
      "ms_recent_pct": 5.057336394496162,
      "value_recent": 14603081.409999998
     },
     "brands": [
      {
       "baseline": {
        "ms_recent_pct": 5.057336394496162,
        "value_recent": 14603081.409999998
       },
       "brand": "리바로",
       "company": null,
       "confidence": {
        "ci_width_absolute": 6971461.500668777,
        "ci_width_relative_pct": 47.739660588996045,
        "label": "보통",
        "method": "ci_width_normalized",
        "score": 65
       },
       "forecast_intervals": {
        "ci_accumulation_guard_applied": true,
        "ci_lower_95": [
         14603081.409999998,
         "...(60개 중 1개)"
        ],
        "ci_upper_95": [
         14603081.409999998,
         "...(60개 중 1개)"
        ],
        "funnel_floor_applied": false,
        "funnel_meta": {
         "funnel_applied_steps": 0,
         "funnel_floor_coefficient": "2 * 1.96 * residual_std * sqrt(t / seasonality)",
         "funnel_total_steps": 60,
         "residual_std": 472875.99698129995,
         "seasonality": 12
        },
        "lower_95_natural": [
         14603081.409999998,
         "...(60개 중 1개)"
        ],
        "lower_floor_applied": true,
        "upper_95_natural": [
         14603081.409999998,
         "...(60개 중 1개)"
        ]
       },
       "forecast_model": {
        "event_regressor": {
         "enabled": false,
         "limitations": [
          "event_regressor_disabled_phase_30",
          "...(2개 중 1개)"
         ],
         "max_regressors": 0,
         "mode": "proxy_light",
         "regressors": []
        },
        "fit_quality": {
         "backtest_available": true,
         "mape_backtest_3m": 4.387714916389385,
         "residual_std": 509317.6282344476
        },
        "name": "HoltWinters",
        "params": {
         "damped_trend": true,
         "seasonal": "add",
         "seasonal_periods": 12,
         "trend": "add"
        },
        "selection_policy": "data_size_dispatch_v1",
        "selection_reason": "prophet_fallback_to_holtwinters",
        "variant": "damped"
       },
       "forecast_ms_pct": [
        5.0573,
        "...(60개 중 1개)"
       ],
       "forecast_values": [
        14603081.409999998,
        "...(60개 중 1개)"
       ],
       "forecast_warnings": [
        "prophet_fit_failed_fallback:ModuleNotFoundError"
       ],
       "history_ms_pct": [
        6.0068,
        "...(65개 중 1개)"
       ],
       "history_periods": [
        "2021-01",
        "...(65개 중 1개)"
       ],
       "history_values": [
        10845337.7,
        "...(65개 중 1개)"
       ],
       "is_jw": true,
       "is_target": true,
       "rank": 3
      },
      "...(6개 중 1개)"
     ],
     "forecast_periods": [
      "2026-05",
      "...(60개 중 1개)"
     ],
     "histor
...(요약 절단)
```

## deep_404
- 요청: `GET /api/deep-analysis/%EC%97%86%EB%8A%94%EB%B8%8C%EB%9E%9C%EB%93%9C`
- ts_utc: 2026-07-17T09:16:53.764779Z  | status: 404  | bytes: 64
- 응답(구조 보존 요약):
```json
{
 "detail": {
  "error": "brand_not_found",
  "brand": "없는브랜드"
 }
}
```

## filter_options_general
- 요청: `GET /api/dynamic-market/filter-options?view=general&brand=%EB%A6%AC%EB%B0%94%EB%A1%9C&source=ubist`
- ts_utc: 2026-07-17T09:16:56.103374Z  | status: 200  | bytes: 1042894
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "view": "general",
 "source": "ubist",
 "market_id": null,
 "dimensions": [
  {
   "dimension_type": "molecule",
   "label": "성분",
   "values": [
    {
     "key": "1앰플(2㎖) 중 iloprost",
     "value": "1앰플(2㎖) 중 iloprost",
     "row_count": 1,
     "default": false,
     "selected": false,
     "flag": false
    },
    "...(1540개 중 1개)"
   ]
  },
  "...(8개 중 1개)"
 ],
 "atc": {
  "atc1": [
   {
    "key": "A",
    "value": "A",
    "label": "A",
    "level": "atc1",
    "parent": null,
    "default": false,
    "selected": false
   },
   "...(16개 중 1개)"
  ],
  "atc2": [
   {
    "key": "A01",
    "value": "A01",
    "label": "A01",
    "level": "atc2",
    "parent": "A",
    "default": false,
    "selected": false
   },
   "...(89개 중 1개)"
  ],
  "atc3": [
   {
    "key": "A01A",
    "value": "A01A",
    "label": "A01A",
    "level": "atc3",
    "parent": "A01",
    "default": false,
    "selected": false
   },
   "...(245개 중 1개)"
  ],
  "atc4": [
   {
    "key": "A10C1",
    "value": "A10C1",
    "label": "A10C1",
    "level": "atc4",
    "parent": "A10C",
    "default": false,
    "selected": false,
    "flag": false
   },
   "...(364개 중 1개)"
  ],
  "selectable_levels": [
   "atc3",
   "...(2개 중 1개)"
  ]
 },
 "brand": "리바로",
 "brand_matched": {
  "atc3": [
   "C10A"
  ],
  "atc4": [
   "C10A1"
  ],
  "form": [
   "정제, 저작정(TB)"
  ],
  "molecule": [
   "pitavastatin calcium"
  ],
  "molecule_strength": [
   "pitavastatin calcium 1mg [470902ATB]",
   "...(3개 중 1개)"
  ],
  "reimbursement": [
   "급여",
   "...(2개 중 1개)"
  ],
  "route": [
   "내복"
  ],
  "seller": [
   "JW중외제약"
  ]
 },
 "default_selections": {
  "atc1": [
   "C"
  ],
  "atc2": [
   "C10"
  ],
  "atc3": [
   "C10A"
  ],
  "atc4": [
   "C10A1"
  ]
 },
 "applied_selections": {}
}
```

## filter_options_strategic
- 요청: `GET /api/dynamic-market/filter-options?view=strategic&brand=%EB%A6%AC%EB%B0%94%EB%A1%9C&source=ubist`
- ts_utc: 2026-07-17T09:16:56.291038Z  | status: 200  | bytes: 1019
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "view": "strategic",
 "source": "ubist",
 "market_id": "ml_006",
 "dimensions": [],
 "atc": {
  "atc1": [
   {
    "key": "C",
    "value": "C",
    "label": "C",
    "level": "atc1",
    "parent": null,
    "default": true,
    "selected": true,
    "flag": false
   }
  ],
  "atc2": [
   {
    "key": "C10",
    "value": "C10",
    "label": "C10",
    "level": "atc2",
    "parent": "C",
    "default": true,
    "selected": true,
    "flag": false
   }
  ],
  "atc3": [
   {
    "key": "C10A",
    "value": "C10A",
    "label": "C10A",
    "level": "atc3",
    "parent": "C10",
    "default": true,
    "selected": true,
    "flag": false
   },
   "...(2개 중 1개)"
  ],
  "atc4": [
   {
    "key": "C10A1",
    "value": "C10A1",
    "label": "C10A1",
    "level": "atc4",
    "parent": "C10A",
    "default": true,
    "selected": true,
    "flag": false
   },
   "...(2개 중 1개)"
  ],
  "selectable_levels": [
   "atc3",
   "...(2개 중 1개)"
  ]
 },
 "brand": "리바로",
 "brand_matched": {},
 "default_selections": {
  "atc1": [
   "C"
  ],
  "atc2": [
   "C10"
  ],
  "atc3": [
   "C10A",
   "...(2개 중 1개)"
  ],
  "atc4": [
   "C10A1",
   "...(2개 중 1개)"
  ]
 },
 "applied_selections": {}
}
```

## brand_option_check
- 요청: `GET /api/dynamic-market/brand-option-check?brand=%EB%A6%AC%EB%B0%94%EB%A1%9C&view=general&source=ubist`
- ts_utc: 2026-07-17T09:16:56.294981Z  | status: 200  | bytes: 1042894
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "view": "general",
 "source": "ubist",
 "market_id": null,
 "dimensions": [
  {
   "dimension_type": "molecule",
   "label": "성분",
   "values": [
    {
     "key": "1앰플(2㎖) 중 iloprost",
     "value": "1앰플(2㎖) 중 iloprost",
     "row_count": 1,
     "default": false,
     "selected": false,
     "flag": false
    },
    "...(1540개 중 1개)"
   ]
  },
  "...(8개 중 1개)"
 ],
 "atc": {
  "atc1": [
   {
    "key": "A",
    "value": "A",
    "label": "A",
    "level": "atc1",
    "parent": null,
    "default": false,
    "selected": false
   },
   "...(16개 중 1개)"
  ],
  "atc2": [
   {
    "key": "A01",
    "value": "A01",
    "label": "A01",
    "level": "atc2",
    "parent": "A",
    "default": false,
    "selected": false
   },
   "...(89개 중 1개)"
  ],
  "atc3": [
   {
    "key": "A01A",
    "value": "A01A",
    "label": "A01A",
    "level": "atc3",
    "parent": "A01",
    "default": false,
    "selected": false
   },
   "...(245개 중 1개)"
  ],
  "atc4": [
   {
    "key": "A10C1",
    "value": "A10C1",
    "label": "A10C1",
    "level": "atc4",
    "parent": "A10C",
    "default": false,
    "selected": false,
    "flag": false
   },
   "...(364개 중 1개)"
  ],
  "selectable_levels": [
   "atc3",
   "...(2개 중 1개)"
  ]
 },
 "brand": "리바로",
 "brand_matched": {
  "atc3": [
   "C10A"
  ],
  "atc4": [
   "C10A1"
  ],
  "form": [
   "정제, 저작정(TB)"
  ],
  "molecule": [
   "pitavastatin calcium"
  ],
  "molecule_strength": [
   "pitavastatin calcium 1mg [470902ATB]",
   "...(3개 중 1개)"
  ],
  "reimbursement": [
   "급여",
   "...(2개 중 1개)"
  ],
  "route": [
   "내복"
  ],
  "seller": [
   "JW중외제약"
  ]
 },
 "default_selections": {
  "atc1": [
   "C"
  ],
  "atc2": [
   "C10"
  ],
  "atc3": [
   "C10A"
  ],
  "atc4": [
   "C10A1"
  ]
 },
 "applied_selections": {}
}
```

## market_filter_atc
- 요청: `GET /api/market-filter/atc-options?brand=%EB%A6%AC%EB%B0%94%EB%A1%9C&view=general&source=ubist`
- ts_utc: 2026-07-17T09:16:56.489228Z  | status: 200  | bytes: 41480
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "brand_name": "",
 "view": "general",
 "source": "ubist",
 "market_id": null,
 "flagged_atc4": [],
 "atc": {
  "atc1": [
   {
    "key": "A",
    "level": "atc1",
    "parent": null,
    "flag": false
   },
   "...(16개 중 1개)"
  ],
  "atc2": [
   {
    "key": "A01",
    "level": "atc2",
    "parent": "A",
    "flag": false
   },
   "...(89개 중 1개)"
  ],
  "atc3": [
   {
    "key": "A01A",
    "level": "atc3",
    "parent": "A01",
    "flag": false
   },
   "...(245개 중 1개)"
  ],
  "atc4": [
   {
    "key": "A10C1",
    "level": "atc4",
    "parent": "A10C",
    "flag": false
   },
   "...(364개 중 1개)"
  ]
 }
}
```

## market_filter_atc_400
- 요청: `GET /api/market-filter/atc-options?brand=%EB%A6%AC%EB%B0%94%EB%A1%9C&view=badview`
- ts_utc: 2026-07-17T09:16:56.614510Z  | status: 422  | bytes: 173
- 응답(구조 보존 요약):
```json
{
 "detail": [
  {
   "type": "literal_error",
   "loc": [
    "query",
    "...(2개 중 1개)"
   ],
   "msg": "Input should be 'general' or 'strategic'",
   "input": "badview",
   "ctx": {
    "expected": "'general' or 'strategic'"
   }
  }
 ]
}
```

## market_scope_options
- 요청: `GET /api/market-scope/options?brand=%EB%A6%AC%EB%B0%94%EB%A1%9C&view_family=strategy`
- ts_utc: 2026-07-17T09:16:56.616336Z  | status: 200  | bytes: 1086
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "brand": "리바로",
 "view_family": "strategy",
 "source": null,
 "options": [
  {
   "option_id": "group:livalo_family",
   "label": "리바로 시장군",
   "option_type": "group_union",
   "view_family": "strategy",
   "source_markets": [
    "strategy_006"
   ],
   "atc4_set": [
    "C10A1",
    "...(2개 중 1개)"
   ],
   "members": [
    {
     "brand_name": "리바로",
     "source_market": "strategy_006",
     "atc4_set": [
      "C10A1"
     ],
     "member_status": "present",
     "reason": null
    },
    "...(2개 중 1개)"
   ],
   "member_status": "present",
   "available_sources": [
    "UBIST"
   ],
   "catalog_version": "GROUP_01_20260716"
  },
  "...(2개 중 1개)"
 ],
 "catalog_version": "GROUP_01_20260716"
}
```

## csd_presence
- 요청: `GET /api/brand-activity/csd-presence?brand=%EB%A6%AC%EB%B0%94%EB%A1%9C`
- ts_utc: 2026-07-17T09:16:56.620494Z  | status: 200  | bytes: 70
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "brand": "리바로",
 "resolved": true,
 "csd_present": true,
 "reason": null
}
```

## csd_presence_422
- 요청: `GET /api/brand-activity/csd-presence`
- ts_utc: 2026-07-17T09:16:56.630751Z  | status: 422  | bytes: 62
- 응답(구조 보존 요약):
```json
{
 "detail": {
  "error": "exactly_one_of_brand_or_brands_required"
 }
}
```

## ba_topics_debug
- 요청: `GET /api/brand-activity/topics`
- ts_utc: 2026-07-17T09:16:56.632253Z  | status: 200  | bytes: 85183
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "data": [
  {
   "scope": {
    "scope_id": "atc4:A02B2",
    "display_name": "PPI Market",
    "atc4_values": [
     "A02B2"
    ],
    "scope_type": "standalone",
    "quality_grade": "A",
    "avg_etc_pct": 0.0,
    "source_row_count": 10677
   },
   "axis": {
    "axis_version": "1.0",
    "source_row_count": 10677,
    "topics": [
     {
      "topic_id": "T1",
      "label": "질환치료",
      "definition": "위식도역류질환 및 미란성 식도염의 임상적 효능과 치료 적응증을 다룬다.",
      "keywords": [
       "GERD",
       "...(4개 중 1개)"
      ]
     },
     "...(7개 중 1개)"
    ]
   },
   "brands": [
    {
     "brand": "JAQBO",
     "is_jw": false,
     "etc_pct": 0.0,
     "topic_shares": [
      {
       "topic_id": "T3",
       "label": "복용편의성",
       "share_pct": 30.2,
       "row_count": 1291
      },
      "...(7개 중 1개)"
     ],
     "topics": [
      {
       "topic_id": "T3",
       "label": "복용편의성",
       "share_pct": 30.2,
       "row_count": 1291
      },
      "...(7개 중 1개)"
     ],
     "brand_specific_topics": [
      {
       "topic_id": "B1",
       "label": "국산신약성과",
       "definition": "",
       "share_pct": 6.9,
       "row_count": 296
      },
      "...(2개 중 1개)"
     ]
    },
    "...(7개 중 1개)"
   ],
   "quality": {
    "grade": "A",
    "avg_etc_pct": 0.0,
    "reasons": []
   }
  },
  "...(11개 중 1개)"
 ]
}
```

## ba_topic_by_scope
- 요청: `GET /api/brand-activity/topics/atc4%3AA02B2`
- ts_utc: 2026-07-17T09:17:47.730852Z  | status: 200  | bytes: 11138
- 응답(구조 보존 요약):
```json
{
 "data": {
  "scope": {
   "scope_id": "atc4:A02B2",
   "display_name": "PPI Market",
   "atc4_values": [
    "A02B2"
   ],
   "scope_type": "standalone",
   "quality_grade": "A",
   "avg_etc_pct": 0.0,
   "source_row_count": 10677
  },
  "axis": {
   "axis_version": "1.0",
   "source_row_count": 10677,
   "topics": [
    {
     "topic_id": "T1",
     "label": "질환치료",
     "definition": "위식도역류질환 및 미란성 식도염의 임상적 효능과 치료 적응증을 다룬다.",
     "keywords": [
      "GERD",
      "...(4개 중 1개)"
     ]
    },
    "...(7개 중 1개)"
   ]
  },
  "brands": [
   {
    "brand": "JAQBO",
    "is_jw": false,
    "etc_pct": 0.0,
    "topic_shares": [
     {
      "topic_id": "T3",
      "label": "복용편의성",
      "share_pct": 30.2,
      "row_count": 1291
     },
     "...(7개 중 1개)"
    ],
    "topics": [
     {
      "topic_id": "T3",
      "label": "복용편의성",
      "share_pct": 30.2,
      "row_count": 1291
     },
     "...(7개 중 1개)"
    ],
    "brand_specific_topics": [
     {
      "topic_id": "B1",
      "label": "국산신약성과",
      "definition": "",
      "share_pct": 6.9,
      "row_count": 296
     },
     "...(2개 중 1개)"
    ]
   },
   "...(7개 중 1개)"
  ],
  "quality": {
   "grade": "A",
   "avg_etc_pct": 0.0,
   "reasons": []
  }
 }
}
```

## dyn_general
- 요청: `POST /api/dynamic-market`
- request body: `{"view": "general", "filters": {"atc4": ["C10A1"]}, "source": "ubist", "measure": "sales"}`
- ts_utc: 2026-07-17T09:16:56.641051Z  | status: 200  | bytes: 2610403
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "status": "SUCCESS",
 "result": {
  "brand": "리피토",
  "brand_key": "리피토",
  "brand_name": "리피토",
  "data": {
   "analysis_level_market_status": {
    "levels": [
     "판매사",
     "...(6개 중 1개)"
    ],
    "channels": [
     "전체",
     "...(5개 중 1개)"
    ],
    "period_unit": "월",
    "periods_monthly": [
     "2021-06",
     "...(60개 중 1개)"
    ],
    "periods_quarterly": [],
    "data": {
     "판매사": {
      "segments": [
       "전체",
       "...(156개 중 1개)"
      ],
      "by_channel": {
       "전체": [
        {
         "name": "전체",
         "rank": 0,
         "is_overall": true,
         "value_series": [
          90209049371.0199,
          "...(60개 중 1개)"
         ]
        },
        "...(156개 중 1개)"
       ],
       "의원 IGF": [
        {
         "name": "전체",
         "rank": 0,
         "is_overall": true,
         "value_series": [
          45547063076.37,
          "...(60개 중 1개)"
         ]
        },
        "...(6개 중 1개)"
       ],
       "주요고객 종합병원 순환기": [
        {
         "name": "전체",
         "rank": 0,
         "is_overall": true,
         "value_series": [
          13855735204.54,
          "...(60개 중 1개)"
         ]
        },
        "...(6개 중 1개)"
       ],
       "병원": [
        {
         "name": "전체",
         "rank": 0,
         "is_overall": true,
         "value_series": [
          4914250730.4,
          "...(60개 중 1개)"
         ]
        },
        "...(6개 중 1개)"
       ],
       "주요고객 종합병원 신경": [
        {
         "name": "전체",
         "rank": 0,
         "is_overall": true,
         "value_series": [
          6345783090.84,
          "...(60개 중 1개)"
         ]
        },
        "...(6개 중 1개)"
       ]
      },
      "ms_by_channel": {
       "전체": [
        {
         "name": "비아트리스",
         "rank": 1,
         "recent_share_pct": 15.0643,
         "series_pct": [
          19.5475,
          "...(60개 중 1개)"
         ],
         "value_series": [
          17633642760.36,
          "...(60개 중 1개)"
         ]
        },
        "...(155개 중 1개)"
       ],
       "의원 IGF": [
        {
         "name": "비아트리스",
         "rank": 1,
         "recent_share_pct": 7.964,
         "series_pct": [
          9.4072,
          "...(60개 중 1개)"
         ],
         "value_series": [
          4284709122.0,
          "...(60개 중 1개)"
         ]
        },
        "...(5개 중 1개)"
       ],
       "주요고객 종합병원 순환기": [
        {
         "name": "비아트리스",
         "rank": 1,
         "recent_share_pct": 28.426,
         "series_pct": [
          31.5145,
          "...(60개 중 1개)"
         ],
         "value_series": [
          4366571553.39,
          "...(60개 중 1개)"
         ]
        },
        "...(5개 중 1개)"
       ],
       "병원": [
        {
         "name": "비아트리스",
         "rank": 1,
         "recent_share_pct": 14.969,
         "series_pct": [
          21.0801,
          "...(60개 중 1개)"
         ],
         "value_series": [
          1035928002.87,
          "...(60개 중 1개)"
         ]
        },
        "...(5개 중 1개)"
       ],
       "주요고객 종합병원 신경": [
        {
         "name": "비아트리스",
         "rank": 1,
         "recent_share_pct": 35.9018,
         "series_pct": [
          39.389,
          "...(60개 중 1개)"
         ],
         "value_series": [
          2499540410.38,
          "...(60개 중 1개)"
         ]
        },
        "...(5개 중 1개)"
       ]
      },
      "ms_segments": [
       {
        "name": "비아트리스",
        "rank": 1,
        "recent_share_pct": 15.0643,
        "series_pct": [
         19.5475,
         "...(60개 중 1개)"
        ],
        "value_series": [
         17633642760.36,
         "...(60개 중 1개)"
        ]
       },
       "...(155개 중 1개)"
      ]
     },
     "성분": {
      "segments": [
       "전체",
       "...(11개 중 1개)"
      ],
      "by_channel": {
       "전체": [
        {
         "name": "전체",
         "rank": 0,
         "is_overall": true,
         "value_series": [
          90209049371.0199,
          "...(60개 중 1개)"
         ]
        },
        "...(11개 중 1개)"
       ],
       "의원 IGF": [
        {
         "name": "전체",
         "rank": 0,
         "is_overall": true,
         "value_series": [
          45547063076.37,
          "...(60개 중 1개)"
         ]
        },
        "...(6개 중 1개)"
       ],
       "주요고객 종합병원 순환기": [
        {
         "name": "전체",
         "rank": 0,
         "is_overall": true,
         "value_series": [
          13855735204.54,
          "...(60개 중 1개)"
         ]
        },
        "...(6개 중 1개)"
       ],
       "병원": [
        {
         "name": "전체",
         "rank": 0,
         "is_overall": true,
         "value_series": [
          4914250730.4,
          "...(60개 중 1개)"
         ]
        },
        "...(6개 중 1개)"
       ],
       "주요고객 종합병원 신경": [
        {
         "name": "전체",
         "rank": 0,
         "is_overall": true,
         "value_series": [
          6345783090.84,
          "...(60개 중 1개)"
         ]
        },
        "...(6개 중 1개)"
       ]
      },
      "ms_by_channel": {
       "전체": [
        {
         "name": "atorvastatin calcium",
         "rank": 1,
         "recent_share_pct": 49.76,
         "series_pct": [
          54.1371,
          "...(60개 중 1개)"
         ],
         "value_series": [
          48836537882.38,
          "...(60개 중 1개)"
         ]
        },
        "...(10개 중 1개)"
       ],
       "의원 IGF": [
        {
         "name": "atorvastatin calcium",
         "rank": 1,
         "recent_share_pct": 47.3477,
         "series_pct": [
          51.2902,
          "...(60개 중 1개)"
         ],
         "value_series": [
          23361199693.77,
          "...(60개 중 1개)"
         ]
        },
        "...(5개 중 1개)"
       ],
       "주요고객 종합병원 순환기": [
        {
         "name": "atorvastatin calcium",
         "rank": 1,
         "recent_share_pct": 52.3325,
         "series_pct": [
          55.3467,
          "...(60개 중 1개)"
         ],
         "value_series": [
          7668693182.92,
          "...(60개 중 1개)"
         ]
        },
...(요약 절단)
```

## dyn_strategic_ml
- 요청: `POST /api/dynamic-market`
- request body: `{"view": "strategic_ml", "filters": {"focus_brand_key": "리바로"}, "source": "ubist", "measure": "sales"}`
- ts_utc: 2026-07-17T09:16:57.258065Z  | status: 200  | bytes: 2256951
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "status": "SUCCESS",
 "result": {
  "brand": "리바로",
  "brand_name": "리바로",
  "brand_key": "리바로",
  "market_id": "strategy_006",
  "view": "strategic_ml",
  "source": "UBIST",
  "measure": "sales",
  "unit_label": "KRW",
  "data": {
   "kpi": {
    "market_size_recent": 213925043319.3602,
    "market_cagr_5y_pct": 9.3677,
    "top3_share_pct": 20.37,
    "hhi_recent": 262.4174,
    "direct_competition_count": 566,
    "target_brand": "리바로",
    "target_company": "JW중외제약",
    "target_ei": 41.6922,
    "ei": 41.6922,
    "ei_basis": "endpoint_5y",
    "ei_period_years": 5,
    "ei_note": null,
    "brand_cagr_pct": 3.9056,
    "market_cagr_pct": 9.3677,
    "target_momentum": -0.0165,
    "target_rank": 6,
    "target_share_pct": 3.7577,
    "brand_value_recent": 8038598793.61,
    "brand_share_pct": 3.7577,
    "momentum_score": -0.0165
   },
   "sources_data": {
    "periods_unit": "월간",
    "periods_count": 65,
    "market_size_series": [
     {
      "period": "2021-01",
      "value": 127640243701.1299,
      "yoy_growth_pct": null,
      "mom_growth_pct": null,
      "sales_krw": 127640243701.1299
     },
     "...(65개 중 1개)"
    ],
    "market_yoy_series": {
     "2021-01": null,
     "2021-02": null,
     "2021-03": null,
     "2021-04": null,
     "2021-05": null,
     "2021-06": null,
     "2021-07": null,
     "2021-08": null,
     "2021-09": null,
     "2021-10": null,
     "2021-11": null,
     "2021-12": null,
     "2022-01": 16.6287,
     "2022-02": 10.9046,
     "2022-03": 7.9755,
     "2022-04": 9.7277,
     "2022-05": 13.1914,
     "2022-06": 6.841,
     "2022-07": 8.6149,
     "2022-08": 13.9455,
     "2022-09": 11.6859,
     "2022-10": 12.2422,
     "2022-11": 12.1226,
     "2022-12": 9.5857,
     "2023-01": 10.8629,
     "2023-02": 16.4942,
     "2023-03": 14.9287,
     "2023-04": 9.3178,
     "2023-05": 14.7817,
     "2023-06": 15.7687,
     "2023-07": 11.6095,
     "2023-08": 10.0462,
     "2023-09": 8.321,
     "2023-10": 11.4617,
     "2023-11": 9.6464,
     "2023-12": 4.479,
     "2024-01": 14.2211,
     "2024-02": 10.4332,
     "2024-03": 3.0914,
     "2024-04": 15.4846,
     "2024-05": 8.024,
     "2024-06": 2.2878,
     "2024-07": 15.5467,
     "2024-08": 9.8046,
     "2024-09": 11.6258,
     "2024-10": 15.441,
     "2024-11": 8.84,
     "2024-12": 17.2357,
     "2025-01": 4.8956,
     "2025-02": 11.867,
     "2025-03": 10.5948,
     "2025-04": 10.5205,
     "2025-05": 6.3235,
     "2025-06": 9.7876,
     "2025-07": 6.7422,
     "2025-08": 2.0217,
     "2025-09": 19.806,
     "2025-10": -2.4148,
     "2025-11": 2.7878,
     "2025-12": 8.1636,
     "2026-01": 10.1089,
     "2026-02": 0.0036,
     "2026-03": 13.5802,
     "2026-04": 7.4502,
     "2026-05": 4.86
    },
    "market_yoy_recent_pct": 4.86,
    "hhi_series_5y": [
     {
      "period": "2021",
      "period_full": "2021",
      "year": 2021,
      "hhi": 329.4089
     },
     "...(5개 중 1개)"
    ],
    "hhi_recent": 262.4174,
    "cagr_5y_pct": 9.3677
   },
   "market_size_series": [
    {
     "period": "2021-01",
     "value": 127640243701.1299,
     "yoy_growth_pct": null,
     "mom_growth_pct": null,
     "sales_krw": 127640243701.1299
    },
    "...(65개 중 1개)"
   ],
   "market_yoy_series": {
    "2021-01": null,
    "2021-02": null,
    "2021-03": null,
    "2021-04": null,
    "2021-05": null,
    "2021-06": null,
    "2021-07": null,
    "2021-08": null,
    "2021-09": null,
    "2021-10": null,
    "2021-11": null,
    "2021-12": null,
    "2022-01": 16.6287,
    "2022-02": 10.9046,
    "2022-03": 7.9755,
    "2022-04": 9.7277,
    "2022-05": 13.1914,
    "2022-06": 6.841,
    "2022-07": 8.6149,
    "2022-08": 13.9455,
    "2022-09": 11.6859,
    "2022-10": 12.2422,
    "2022-11": 12.1226,
    "2022-12": 9.5857,
    "2023-01": 10.8629,
    "2023-02": 16.4942,
    "2023-03": 14.9287,
    "2023-04": 9.3178,
    "2023-05": 14.7817,
    "2023-06": 15.7687,
    "2023-07": 11.6095,
    "2023-08": 10.0462,
    "2023-09": 8.321,
    "2023-10": 11.4617,
    "2023-11": 9.6464,
    "2023-12": 4.479,
    "2024-01": 14.2211,
    "2024-02": 10.4332,
    "2024-03": 3.0914,
    "2024-04": 15.4846,
    "2024-05": 8.024,
    "2024-06": 2.2878,
    "2024-07": 15.5467,
    "2024-08": 9.8046,
    "2024-09": 11.6258,
    "2024-10": 15.441,
    "2024-11": 8.84,
    "2024-12": 17.2357,
    "2025-01": 4.8956,
    "2025-02": 11.867,
    "2025-03": 10.5948,
    "2025-04": 10.5205,
    "2025-05": 6.3235,
    "2025-06": 9.7876,
    "2025-07": 6.7422,
    "2025-08": 2.0217,
    "2025-09": 19.806,
    "2025-10": -2.4148,
    "2025-11": 2.7878,
    "2025-12": 8.1636,
    "2026-01": 10.1089,
    "2026-02": 0.0036,
    "2026-03": 13.5802,
    "2026-04": 7.4502,
    "2026-05": 4.86
   },
   "market_yoy_recent_pct": 4.86,
   "hhi_series_5y": [
    {
     "period": "2021",
     "period_full": "2021",
     "year": 2021,
     "hhi": 329.4089
    },
    "...(5개 중 1개)"
   ],
   "hhi_recent": 262.4174,
   "brand_ranking": {
    "years": [
     2022,
     "...(5개 중 1개)"
    ],
    "yearly": [
     {
      "year": 2022,
      "rankings": [
       {
        "brand": "리피토",
        "company": "비아트리스",
        "is_target": false,
        "is_jw": false,
        "is_others": false,
        "value": 202784072212.55,
        "rank": 1,
        "ms_pct": 10.7666
       },
       "...(11개 중 1개)"
      ]
     },
     "...(5개 중 1개)"
    ],
    "brands": [
     {
      "brand": "리바로",
      "company": "JW중외제약",
      "is_target": true,
      "is_jw": true,
      "yearly_values": [
       {
        "year": 2022,
        "value": 86330525946.61,
        "ms_pct": 4.5836,
        "rank": 5
       },
       "...(5개 중 1개)"
      ]
     },
     "...(7개 중 1개)"
    ],
    "top_brands": [
     "리바로",
     "...(7개 중 1개)"
    ],
    "series": {
     "리피토": [
      202784072212.55,
      "...(5개 중 1개)"
     ],
     "로수젯": [
      149882765192.4999,
      "...(5개 중 1개)"
     ],
     "아토젯": [
      90766214242.2099,
      "...(5개 중 1개)"
     ],
     "크레스토": 
...(요약 절단)
```

## dyn_strategic_cd
- 요청: `POST /api/dynamic-market`
- request body: `{"view": "strategic_cd", "filters": {"focus_brand_key": "리바로"}, "source": "ubist", "measure": "sales"}`
- ts_utc: 2026-07-17T09:16:57.746176Z  | status: 200  | bytes: 2256962
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "status": "SUCCESS",
 "result": {
  "brand": "리바로",
  "brand_name": "리바로",
  "brand_key": "리바로",
  "market_id": "strategy_006",
  "view": "strategic_cd",
  "source": "UBIST",
  "measure": "sales",
  "unit_label": "KRW",
  "data": {
   "kpi": {
    "market_size_recent": 213925043319.3602,
    "market_cagr_5y_pct": 9.3677,
    "top3_share_pct": 20.37,
    "hhi_recent": 262.4174,
    "direct_competition_count": 566,
    "target_brand": "리바로",
    "target_company": "JW중외제약",
    "target_ei": 41.6922,
    "ei": 41.6922,
    "ei_basis": "endpoint_5y",
    "ei_period_years": 5,
    "ei_note": null,
    "brand_cagr_pct": 3.9056,
    "market_cagr_pct": 9.3677,
    "target_momentum": -0.0165,
    "target_rank": 6,
    "target_share_pct": 3.7577,
    "brand_value_recent": 8038598793.61,
    "brand_share_pct": 3.7577,
    "momentum_score": -0.0165
   },
   "sources_data": {
    "periods_unit": "월간",
    "periods_count": 65,
    "market_size_series": [
     {
      "period": "2021-01",
      "value": 127640243701.1299,
      "yoy_growth_pct": null,
      "mom_growth_pct": null,
      "sales_krw": 127640243701.1299
     },
     "...(65개 중 1개)"
    ],
    "market_yoy_series": {
     "2021-01": null,
     "2021-02": null,
     "2021-03": null,
     "2021-04": null,
     "2021-05": null,
     "2021-06": null,
     "2021-07": null,
     "2021-08": null,
     "2021-09": null,
     "2021-10": null,
     "2021-11": null,
     "2021-12": null,
     "2022-01": 16.6287,
     "2022-02": 10.9046,
     "2022-03": 7.9755,
     "2022-04": 9.7277,
     "2022-05": 13.1914,
     "2022-06": 6.841,
     "2022-07": 8.6149,
     "2022-08": 13.9455,
     "2022-09": 11.6859,
     "2022-10": 12.2422,
     "2022-11": 12.1226,
     "2022-12": 9.5857,
     "2023-01": 10.8629,
     "2023-02": 16.4942,
     "2023-03": 14.9287,
     "2023-04": 9.3178,
     "2023-05": 14.7817,
     "2023-06": 15.7687,
     "2023-07": 11.6095,
     "2023-08": 10.0462,
     "2023-09": 8.321,
     "2023-10": 11.4617,
     "2023-11": 9.6464,
     "2023-12": 4.479,
     "2024-01": 14.2211,
     "2024-02": 10.4332,
     "2024-03": 3.0914,
     "2024-04": 15.4846,
     "2024-05": 8.024,
     "2024-06": 2.2878,
     "2024-07": 15.5467,
     "2024-08": 9.8046,
     "2024-09": 11.6258,
     "2024-10": 15.441,
     "2024-11": 8.84,
     "2024-12": 17.2357,
     "2025-01": 4.8956,
     "2025-02": 11.867,
     "2025-03": 10.5948,
     "2025-04": 10.5205,
     "2025-05": 6.3235,
     "2025-06": 9.7876,
     "2025-07": 6.7422,
     "2025-08": 2.0217,
     "2025-09": 19.806,
     "2025-10": -2.4148,
     "2025-11": 2.7878,
     "2025-12": 8.1636,
     "2026-01": 10.1089,
     "2026-02": 0.0036,
     "2026-03": 13.5802,
     "2026-04": 7.4502,
     "2026-05": 4.86
    },
    "market_yoy_recent_pct": 4.86,
    "hhi_series_5y": [
     {
      "period": "2021",
      "period_full": "2021",
      "year": 2021,
      "hhi": 329.4089
     },
     "...(5개 중 1개)"
    ],
    "hhi_recent": 262.4174,
    "cagr_5y_pct": 9.3677
   },
   "market_size_series": [
    {
     "period": "2021-01",
     "value": 127640243701.1299,
     "yoy_growth_pct": null,
     "mom_growth_pct": null,
     "sales_krw": 127640243701.1299
    },
    "...(65개 중 1개)"
   ],
   "market_yoy_series": {
    "2021-01": null,
    "2021-02": null,
    "2021-03": null,
    "2021-04": null,
    "2021-05": null,
    "2021-06": null,
    "2021-07": null,
    "2021-08": null,
    "2021-09": null,
    "2021-10": null,
    "2021-11": null,
    "2021-12": null,
    "2022-01": 16.6287,
    "2022-02": 10.9046,
    "2022-03": 7.9755,
    "2022-04": 9.7277,
    "2022-05": 13.1914,
    "2022-06": 6.841,
    "2022-07": 8.6149,
    "2022-08": 13.9455,
    "2022-09": 11.6859,
    "2022-10": 12.2422,
    "2022-11": 12.1226,
    "2022-12": 9.5857,
    "2023-01": 10.8629,
    "2023-02": 16.4942,
    "2023-03": 14.9287,
    "2023-04": 9.3178,
    "2023-05": 14.7817,
    "2023-06": 15.7687,
    "2023-07": 11.6095,
    "2023-08": 10.0462,
    "2023-09": 8.321,
    "2023-10": 11.4617,
    "2023-11": 9.6464,
    "2023-12": 4.479,
    "2024-01": 14.2211,
    "2024-02": 10.4332,
    "2024-03": 3.0914,
    "2024-04": 15.4846,
    "2024-05": 8.024,
    "2024-06": 2.2878,
    "2024-07": 15.5467,
    "2024-08": 9.8046,
    "2024-09": 11.6258,
    "2024-10": 15.441,
    "2024-11": 8.84,
    "2024-12": 17.2357,
    "2025-01": 4.8956,
    "2025-02": 11.867,
    "2025-03": 10.5948,
    "2025-04": 10.5205,
    "2025-05": 6.3235,
    "2025-06": 9.7876,
    "2025-07": 6.7422,
    "2025-08": 2.0217,
    "2025-09": 19.806,
    "2025-10": -2.4148,
    "2025-11": 2.7878,
    "2025-12": 8.1636,
    "2026-01": 10.1089,
    "2026-02": 0.0036,
    "2026-03": 13.5802,
    "2026-04": 7.4502,
    "2026-05": 4.86
   },
   "market_yoy_recent_pct": 4.86,
   "hhi_series_5y": [
    {
     "period": "2021",
     "period_full": "2021",
     "year": 2021,
     "hhi": 329.4089
    },
    "...(5개 중 1개)"
   ],
   "hhi_recent": 262.4174,
   "brand_ranking": {
    "years": [
     2022,
     "...(5개 중 1개)"
    ],
    "yearly": [
     {
      "year": 2022,
      "rankings": [
       {
        "brand": "리피토",
        "company": "비아트리스",
        "is_target": false,
        "is_jw": false,
        "is_others": false,
        "value": 202784072212.55,
        "rank": 1,
        "ms_pct": 10.7666
       },
       "...(11개 중 1개)"
      ]
     },
     "...(5개 중 1개)"
    ],
    "brands": [
     {
      "brand": "리바로",
      "company": "JW중외제약",
      "is_target": true,
      "is_jw": true,
      "yearly_values": [
       {
        "year": 2022,
        "value": 86330525946.61,
        "ms_pct": 4.5836,
        "rank": 5
       },
       "...(5개 중 1개)"
      ]
     },
     "...(7개 중 1개)"
    ],
    "top_brands": [
     "리바로",
     "...(7개 중 1개)"
    ],
    "series": {
     "리피토": [
      202784072212.55,
      "...(5개 중 1개)"
     ],
     "로수젯": [
      149882765192.4999,
      "...(5개 중 1개)"
     ],
     "아토젯": [
      90766214242.2099,
      "...(5개 중 1개)"
     ],
     "크레스토": 
...(요약 절단)
```

## dyn_400_broad
- 요청: `POST /api/dynamic-market`
- request body: `{"view": "general", "filters": {}, "source": "ubist", "measure": "sales"}`
- ts_utc: 2026-07-17T09:16:58.228401Z  | status: 400  | bytes: 114
- 응답(구조 보존 요약):
```json
{
 "detail": {
  "error": "invalid_dynamic_market_request",
  "message": "at least one ATC4 or molecule filter is required"
 }
}
```

## market_scope_resolve
- 요청: `POST /api/market-scope/resolve`
- request body: `{"brand": "리바로", "view_family": "strategy", "source": "UBIST", "measure": "sales", "option_ids": ["group:livalo_family"]}`
- ts_utc: 2026-07-17T09:16:58.281444Z  | status: 200  | bytes: 567
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "scope_hash": "935e15fd9f233092b1b5eb817b8a97138def557642e4ad7ffc166ad14f0b2438",
 "view_family": "strategy",
 "selected_option_ids": [
  "group:livalo_family"
 ],
 "resolved_source_markets": [
  "strategy_006"
 ],
 "resolved_atc4_set": [
  "C10A1",
  "...(2개 중 1개)"
 ],
 "excluded_members": [],
 "dedup": {
  "dedup_strategy": "brand_key_disjoint_sum_v1",
  "dedup_key_version": "brand_key_market_guard_v1",
  "candidate_fact_count": 555,
  "deduped_fact_count": 555,
  "dropped_duplicate_count": 0,
  "disjoint": true,
  "overlap_brand_key_count": 0
 },
 "catalog_version": "GROUP_01_20260716",
 "algorithm_version": "strategy-union-recalc-v1"
}
```

## market_scope_cause
- 요청: `POST /api/market-scope/cause`
- request body: `{"brand": "리바로", "view_family": "strategy", "source": "UBIST", "measure": "sales", "option_ids": ["group:livalo_family"], "view": "market_landscape"}`
- ts_utc: 2026-07-17T09:16:58.531233Z  | status: 200  | bytes: 1666415
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "status": "SUCCESS",
 "result": {
  "brand": "리바로",
  "brand_key": "리바로",
  "brand_name": "리바로",
  "data": {
   "analysis_level_market_status": {
    "period_unit": "월간",
    "channels": [],
    "levels": [],
    "periods_monthly": [
     "2021-01",
     "...(65개 중 1개)"
    ],
    "periods_quarterly": [],
    "data": {}
   },
   "analysis_levels": {
    "period_unit": "월간",
    "channels": [],
    "levels": [],
    "periods_monthly": [
     "2021-01",
     "...(65개 중 1개)"
    ],
    "periods_quarterly": [],
    "data": {}
   },
   "brand_ranking": {
    "years": [
     2022,
     "...(5개 중 1개)"
    ],
    "yearly": [
     {
      "year": 2022,
      "rankings": [
       {
        "brand_key": "리바로",
        "rank": 5,
        "raw_value": 86330525946.61,
        "value": 86330525946.61,
        "ms": 4.5836,
        "ms_pct": 4.5836,
        "is_target": false,
        "is_jw": false,
        "is_others": false,
        "brand": "리바로",
        "ms_recent_pct": 4.5836,
        "share_pct": 4.5836
       },
       "...(7개 중 1개)"
      ]
     },
     "...(5개 중 1개)"
    ],
    "brands": [
     {
      "brand_key": "리바로",
      "yearly_values": [
       {
        "year": 2022,
        "value": 86330525946.61
       },
       "...(5개 중 1개)"
      ]
     },
     "...(6개 중 1개)"
    ],
    "top_brands": [
     "리바로",
     "...(7개 중 1개)"
    ],
    "series": {
     "리바로": [
      86330525946.61,
      "...(5개 중 1개)"
     ],
     "로수젯": [
      149882765192.4999,
      "...(5개 중 1개)"
     ],
     "리피토": [
      202784072212.55,
      "...(5개 중 1개)"
     ],
     "리바로젯": [
      31827300856.92,
      "...(5개 중 1개)"
     ],
     "아토젯": [
      90766214242.2099,
      "...(5개 중 1개)"
     ],
     "로수바미브": [
      67733398163.7499,
      "...(5개 중 1개)"
     ],
     "기타": [
      1254129942468.4185,
      "...(5개 중 1개)"
     ]
    },
    "rankings_by_year": {
     "2022": [
      {
       "brand_key": "리피토",
       "rank": 1,
       "raw_value": 202784072212.55,
       "value": 202784072212.55,
       "ms": 10.7666,
       "ms_pct": 10.7666,
       "is_target": false,
       "is_jw": false,
       "is_others": false,
       "brand": "리피토",
       "ms_recent_pct": 10.7666,
       "share_pct": 10.7666
      },
      "...(555개 중 1개)"
     ],
     "2023": [
      {
       "brand_key": "리피토",
       "rank": 1,
       "raw_value": 195727889979.3899,
       "value": 195727889979.3899,
       "ms": 9.3341,
       "ms_pct": 9.3341,
       "is_target": false,
       "is_jw": false,
       "is_others": false,
       "brand": "리피토",
       "ms_recent_pct": 9.3341,
       "share_pct": 9.3341
      },
      "...(555개 중 1개)"
     ],
     "2024": [
      {
       "brand_key": "로수젯",
       "rank": 1,
       "raw_value": 210273696327.0099,
       "value": 210273696327.0099,
       "ms": 9.0365,
       "ms_pct": 9.0365,
       "is_target": false,
       "is_jw": false,
       "is_others": false,
       "brand": "로수젯",
       "ms_recent_pct": 9.0365,
       "share_pct": 9.0365
      },
      "...(555개 중 1개)"
     ],
     "2025": [
      {
       "brand_key": "로수젯",
       "rank": 1,
       "raw_value": 227853719589.38,
       "value": 227853719589.38,
       "ms": 9.1137,
       "ms_pct": 9.1137,
       "is_target": false,
       "is_jw": false,
       "is_others": false,
       "brand": "로수젯",
       "ms_recent_pct": 9.1137,
       "share_pct": 9.1137
      },
      "...(555개 중 1개)"
     ],
     "2026": [
      {
       "brand_key": "로수젯",
       "rank": 1,
       "raw_value": 99468159905.87,
       "value": 99468159905.87,
       "ms": 9.1701,
       "ms_pct": 9.1701,
       "is_target": false,
       "is_jw": false,
       "is_others": false,
       "brand": "로수젯",
       "ms_recent_pct": 9.1701,
       "share_pct": 9.1701
      },
      "...(555개 중 1개)"
     ]
    },
    "period_count_by_year": {
     "2022": 12,
     "2023": 12,
     "2024": 12,
     "2025": 12,
     "2026": 5
    }
   },
   "brand_ranking_stacked": {
    "years": [
     2022,
     "...(5개 중 1개)"
    ],
    "yearly": [
     {
      "year": 2022,
      "rankings": [
       {
        "brand_key": "리바로",
        "rank": 5,
        "raw_value": 86330525946.61,
        "value": 86330525946.61,
        "ms": 4.5836,
        "ms_pct": 4.5836,
        "is_target": false,
        "is_jw": false,
        "is_others": false,
        "brand": "리바로",
        "ms_recent_pct": 4.5836,
        "share_pct": 4.5836
       },
       "...(7개 중 1개)"
      ]
     },
     "...(5개 중 1개)"
    ],
    "brands": [
     {
      "brand_key": "리바로",
      "yearly_values": [
       {
        "year": 2022,
        "value": 86330525946.61
       },
       "...(5개 중 1개)"
      ]
     },
     "...(6개 중 1개)"
    ],
    "top_brands": [
     "리바로",
     "...(7개 중 1개)"
    ],
    "series": {
     "리바로": [
      86330525946.61,
      "...(5개 중 1개)"
     ],
     "로수젯": [
      149882765192.4999,
      "...(5개 중 1개)"
     ],
     "리피토": [
      202784072212.55,
      "...(5개 중 1개)"
     ],
     "리바로젯": [
      31827300856.92,
      "...(5개 중 1개)"
     ],
     "아토젯": [
      90766214242.2099,
      "...(5개 중 1개)"
     ],
     "로수바미브": [
      67733398163.7499,
      "...(5개 중 1개)"
     ],
     "기타": [
      1254129942468.4185,
      "...(5개 중 1개)"
     ]
    },
    "rankings_by_year": {
     "2022": [
      {
       "brand_key": "리피토",
       "rank": 1,
       "raw_value": 202784072212.55,
       "value": 202784072212.55,
       "ms": 10.7666,
       "ms_pct": 10.7666,
       "is_target": false,
       "is_jw": false,
       "is_others": false,
       "brand": "리피토",
       "ms_recent_pct": 10.7666,
       "share_pct": 10.7666
      },
      "...(555개 중 1개)"
     ],
     "2023": [
      {
       "brand_key": "리피토",
       "rank": 1,
       "raw_value": 195727889979.3899,
       "value": 195727889979.3899,
       "ms": 9.3341,
       "ms_pct": 9.3341,
       "is_target": false,
       "is_jw": false,
       "is_others": false,
       "brand": "리피토",
       "ms_recent_pct": 9.3341,
       "shar
...(요약 절단)
```

## ba_topic_matrix
- 요청: `POST /api/brand-activity/topics`
- request body: `{"view": "general", "selected_brand": "리바로", "filters": {"atc": {"atc4": ["C10A1"]}}, "top_n": 5}`
- ts_utc: 2026-07-17T09:16:59.501150Z  | status: 200  | bytes: 1936
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "data": {
  "scope": {
   "view": "general",
   "market_id": "C10A1",
   "market_name": "STATINS (HMG-COA RED)",
   "selected_brand": "리바로",
   "applied_filter": {
    "atc4": [
     "C10A1"
    ]
   },
   "applied_filters": {
    "atc4": [
     "C10A1"
    ]
   },
   "resolved_market": {
    "type": "general",
    "market_id": "C10A1",
    "market_label": "STATINS (HMG-COA RED)",
    "source": "filters"
   },
   "visit_location": "전체",
   "specialty": "전체",
   "interest": "전체",
   "prescription_evolution": "전체",
   "period_start": "",
   "period_end": "",
   "top_n": 5,
   "sliced": false,
   "applied_topic_filters": {},
   "topic_set_version": null,
   "filter_effect": {
    "brand_set": "base",
    "payload": "row_topic_assignment_unfiltered"
   }
  },
  "brands": [
   {
    "brand_key": "리바로",
    "brand_name": "리바로",
    "is_jw": true,
    "is_selected": true,
    "sales_rank": 3,
    "event_count": 0,
    "topic_shares": [],
    "topics": [],
    "etc_pct": 100.0,
    "brand_specific_topics": []
   },
   "...(6개 중 1개)"
  ],
  "reason": "no_topic_scope"
 },
 "meta": {
  "period": {
   "start_date": "2023-06",
   "end_date": "2026-05",
   "available_start": "2023-06",
   "available_end": "2026-05"
  },
  "request_normalized": true
 }
}
```

## ba_csd_timeseries
- 요청: `POST /api/brand-activity/csd-timeseries`
- request body: `{"view": "general", "selected_brand": "리바로", "filters": {"atc": {"atc4": ["C10A1"]}}, "mode": "absolute"}`
- ts_utc: 2026-07-17T09:16:59.629297Z  | status: 200  | bytes: 36331
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "data": {
  "scope": {
   "view": "general",
   "market_id": "C10A1",
   "market_name": "STATINS (HMG-COA RED)",
   "csd_market": "LIVALO",
   "csd_markets": [
    "LIVALO"
   ],
   "selected_brand": {
    "brand_key": "리바로",
    "product_code": "LIVALO"
   },
   "ranking_measure": "sales",
   "ranking_quarter": "2026-Q1",
   "filter": {
    "atc4": [
     "C10A1"
    ]
   },
   "applied_filter": {
    "atc4": [
     "C10A1"
    ]
   },
   "applied_filters": {
    "atc4": [
     "C10A1"
    ]
   },
   "filter_effect": {
    "brand_set": "base",
    "activity": "csd_total_channel",
    "rx": "iqvia_nsa_public_measures"
   },
   "resolved_market": {
    "type": "general",
    "market_id": "C10A1",
    "market_label": "STATINS (HMG-COA RED)",
    "source": "filters"
   },
   "quarters": [
    "2023-Q3",
    "...(11개 중 1개)"
   ],
   "activity_months": [
    "2023-07",
    "...(33개 중 1개)"
   ],
   "measures": [
    "activity",
    "...(5개 중 1개)"
   ],
   "mode": "absolute"
  },
  "brands": [
   {
    "brand_key": "리바로",
    "brand_name": "리바로",
    "product_code": "LIVALO",
    "is_selected": true,
    "is_jw": true,
    "sales_rank": 3,
    "csd_matched": true,
    "series": {
     "activity": {
      "source": "csd",
      "absolute": {
       "2023-07": 2949.0,
       "2023-08": 1760.0,
       "2023-09": 3296.0,
       "2023-10": 2455.0,
       "2023-11": 2597.0,
       "2023-12": 2597.0,
       "2024-01": 3174.0,
       "2024-02": 2792.0,
       "2024-03": 2795.0,
       "2024-04": 2494.0,
       "2024-05": 2815.0,
       "2024-06": 2524.0,
       "2024-07": 2519.0,
       "2024-08": 2176.0,
       "2024-09": 1268.0,
       "2024-10": 2377.0,
       "2024-11": 2449.0,
       "2024-12": 2502.0,
       "2025-01": 2157.0,
       "2025-02": 2094.0,
       "2025-03": 2807.0,
       "2025-04": 2200.0,
       "2025-05": 2382.0,
       "2025-06": 1775.0,
       "2025-07": 1627.0,
       "2025-08": 1340.0,
       "2025-09": 1772.0,
       "2025-10": 2481.0,
       "2025-11": 2311.0,
       "2025-12": 1693.0,
       "2026-01": 2193.0,
       "2026-02": 1468.0,
       "2026-03": 1389.0
      },
      "ratio": {
       "2023-07": 17.09862584797356,
       "2023-08": 11.09919909188371,
       "2023-09": 21.833598304186538,
       "2023-10": 14.126244317854884,
       "2023-11": 16.37762502364886,
       "2023-12": 14.541687664482893,
       "2024-01": 18.682677026311143,
       "2024-02": 15.980768130044073,
       "2024-03": 17.269076305220885,
       "2024-04": 15.852030763363631,
       "2024-05": 19.920741631873188,
       "2024-06": 17.086379637151367,
       "2024-07": 14.731855664073922,
       "2024-08": 14.846148597939552,
       "2024-09": 9.489597365663823,
       "2024-10": 15.29994850669413,
       "2024-11": 15.631582306759432,
       "2024-12": 16.295427901524032,
       "2025-01": 12.297605473204106,
       "2025-02": 11.471458310507286,
       "2025-03": 15.017923064576536,
       "2025-04": 14.155192381932826,
       "2025-05": 14.754707631318137,
       "2025-06": 12.190096834008653,
       "2025-07": 10.286400708098881,
       "2025-08": 10.091121319376459,
       "2025-09": 11.314730860098333,
       "2025-10": 18.516307187103514,
       "2025-11": 17.960674593922437,
       "2025-12": 14.247243961962466,
       "2026-01": 14.402994877183763,
       "2026-02": 11.511017015604171,
       "2026-03": 8.755121336274819
      }
     },
     "sales": {
      "source": "iqvia_nsa",
      "absolute": {
       "2023-Q3": 15018218438.0,
       "2023-Q4": 15398409681.0,
       "2024-Q1": 15017898497.0,
       "2024-Q2": 15390571537.0,
       "2024-Q3": 15819290664.0,
       "2024-Q4": 16027034818.0,
       "2025-Q1": 15836189977.0,
       "2025-Q2": 15982743969.0,
       "2025-Q3": 16693319605.0,
       "2025-Q4": 16170853808.0,
       "2026-Q1": 17097247580.0
      },
      "ratio": {
       "2023-Q3": 8.314447610729198,
       "2023-Q4": 8.418365531875953,
       "2024-Q1": 8.419131090032264,
       "2024-Q2": 8.66493501803,
       "2024-Q3": 8.569375831196865,
       "2024-Q4": 8.684273629374612,
       "2025-Q1": 8.746984126737946,
       "2025-Q2": 8.765619733166925,
       "2025-Q3": 8.809260021940961,
       "2025-Q4": 8.874346488713382,
       "2026-Q1": 9.087931480166528
      }
     },
     "unit": {
      "source": "iqvia_nsa",
      "absolute": {
       "2023-Q3": 666903.0,
       "2023-Q4": 684619.0,
       "2024-Q1": 680388.0,
       "2024-Q2": 703694.0,
       "2024-Q3": 736261.0,
       "2024-Q4": 750443.0,
       "2025-Q1": 749375.0,
       "2025-Q2": 768200.0,
       "2025-Q3": 807407.0,
       "2025-Q4": 791677.0,
       "2026-Q1": 834804.0
      },
      "ratio": {
       "2023-Q3": 7.822008986414584,
       "2023-Q4": 7.895187206230728,
       "2024-Q1": 7.921440833512337,
       "2024-Q2": 8.158639530331047,
       "2024-Q3": 8.249578255366599,
       "2024-Q4": 8.43482113673833,
       "2025-Q1": 8.529206792589154,
       "2025-Q2": 8.676815068968933,
       "2025-Q3": 8.750825598663349,
       "2025-Q4": 8.829288438158668,
       "2026-Q1": 9.046535685421341
      }
     },
     "counting_unit": {
      "source": "iqvia_nsa",
      "absolute": {
       "2023-Q3": 28991590.0,
       "2023-Q4": 29746790.0,
       "2024-Q1": 29226950.0,
       "2024-Q2": 30108340.0,
       "2024-Q3": 31082060.0,
       "2024-Q4": 31564220.0,
       "2025-Q1": 31327430.0,
       "2025-Q2": 31798450.0,
       "2025-Q3": 33270270.0,
       "2025-Q4": 32377460.0,
       "2026-Q1": 34255000.0
      },
      "ratio": {
       "2023-Q3": 8.558016016019774,
       "2023-Q4": 8.604116851528975,
       "2024-Q1": 8.628587183238167,
       "2024-Q2": 8.885242469473157,
       "2024-Q3": 8.818087879324715,
       "2024-Q4": 8.935241734876909,
       "2025-Q1": 9.01792160299006,
       "2025-Q2": 9.066987563187794,
       "2025-Q3": 9.109310061526688,
       "2025-Q4": 9.177088344800246,
       "2026-Q1": 9.401619097410753
      }
     },
     "dosage_unit": {
      "source": "iqvia_nsa",
      "a
...(요약 절단)
```

## ba_csd_activity_series
- 요청: `POST /api/brand-activity/csd-activity-series`
- request body: `{"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}, "entity_level": "brand", "csd_channel": "TOTAL"}`
- ts_utc: 2026-07-17T09:16:59.863038Z  | status: 200  | bytes: 107240
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "data": {
  "scope": {
   "view": "general",
   "market_id": "C10A1",
   "market_name": "STATINS (HMG-COA RED)",
   "csd_market": "LIVALO",
   "csd_markets": [
    "LIVALO"
   ],
   "selected_brand": "리바로",
   "filter": {
    "atc4": [
     "C10A1"
    ]
   },
   "applied_filter": {
    "atc4": [
     "C10A1"
    ]
   },
   "quarters": [
    "2025-Q2",
    "...(4개 중 1개)"
   ]
  },
  "entity_level": "brand",
  "channel": "TOTAL",
  "period": {
   "quarters": [
    "2025-Q2",
    "...(4개 중 1개)"
   ],
   "months": [
    "2025-04",
    "...(12개 중 1개)"
   ],
   "max_quarters": 12,
   "default_quarters": 4
  },
  "entities": [
   {
    "key": "리바로",
    "display_name": "리바로",
    "is_selected": true,
    "is_jw": true,
    "activity": {
     "absolute": [
      {
       "period": "2025-04",
       "value": 2200.0
      },
      "...(12개 중 1개)"
     ],
     "share_pct": [
      {
       "period": "2025-04",
       "value": 14.155192381932826
      },
      "...(12개 중 1개)"
     ],
     "rank": [
      {
       "period": "2025-04",
       "value": 6
      },
      "...(12개 중 1개)"
     ]
    }
   },
   "...(6개 중 1개)"
  ],
  "series_by_csd_market": {
   "LIVALO": {
    "available": {
     "start": "2025-04",
     "end": "2026-03"
    },
    "market_totals": {
     "2025-04": 15542.0,
     "2025-05": 16144.0,
     "2025-06": 14561.0,
     "2025-07": 15817.0,
     "2025-08": 13279.0,
     "2025-09": 15661.0,
     "2025-10": 13399.0,
     "2025-11": 12867.0,
     "2025-12": 11883.0,
     "2026-01": 15226.0,
     "2026-02": 12753.0,
     "2026-03": 15865.0
    },
    "by_entity": {
     "APECOR": {
      "2025-04": 45.0,
      "2025-05": 0.0,
      "2025-06": 45.0,
      "2025-07": 100.0,
      "2025-08": 50.0,
      "2025-09": 48.0,
      "2025-10": 45.0,
      "2025-11": 46.0,
      "2025-12": 49.0,
      "2026-01": 59.0,
      "2026-02": 0.0,
      "2026-03": 0.0
     },
     "ARITO": {
      "2025-04": 38.0,
      "2025-05": 147.0,
      "2025-06": 0.0,
      "2025-07": 0.0,
      "2025-08": 0.0,
      "2025-09": 161.0,
      "2025-10": 119.0,
      "2025-11": 117.0,
      "2025-12": 286.0,
      "2026-01": 162.0,
      "2026-02": 91.0,
      "2026-03": 36.0
     },
     "ATOLOW": {
      "2025-04": 29.0,
      "2025-05": 0.0,
      "2025-06": 32.0,
      "2025-07": 0.0,
      "2025-08": 0.0,
      "2025-09": 0.0,
      "2025-10": 0.0,
      "2025-11": 0.0,
      "2025-12": 0.0,
      "2026-01": 127.0,
      "2026-02": 54.0,
      "2026-03": 91.0
     },
     "ATOREN": {
      "2025-04": 61.0,
      "2025-05": 62.0,
      "2025-06": 63.0,
      "2025-07": 60.0,
      "2025-08": 110.0,
      "2025-09": 59.0,
      "2025-10": 134.0,
      "2025-11": 122.0,
      "2025-12": 59.0,
      "2026-01": 143.0,
      "2026-02": 84.0,
      "2026-03": 112.0
     },
     "ATORSTA": {
      "2025-04": 61.0,
      "2025-05": 32.0,
      "2025-06": 32.0,
      "2025-07": 0.0,
      "2025-08": 34.0,
      "2025-09": 34.0,
      "2025-10": 76.0,
      "2025-11": 36.0,
      "2025-12": 34.0,
      "2026-01": 36.0,
      "2026-02": 0.0,
      "2026-03": 39.0
     },
     "ATORVA": {
      "2025-04": 364.0,
      "2025-05": 713.0,
      "2025-06": 924.0,
      "2025-07": 979.0,
      "2025-08": 471.0,
      "2025-09": 664.0,
      "2025-10": 414.0,
      "2025-11": 444.0,
      "2025-12": 259.0,
      "2026-01": 372.0,
      "2026-02": 422.0,
      "2026-03": 143.0
     },
     "ATORVASTATIN DWN": {
      "2025-04": 186.0,
      "2025-05": 0.0,
      "2025-06": 299.0,
      "2025-07": 152.0,
      "2025-08": 142.0,
      "2025-09": 0.0,
      "2025-10": 0.0,
      "2025-11": 236.0,
      "2025-12": 50.0,
      "2026-01": 769.0,
      "2026-02": 0.0,
      "2026-03": 338.0
     },
     "ATOSEN": {
      "2025-04": 55.0,
      "2025-05": 62.0,
      "2025-06": 0.0,
      "2025-07": 0.0,
      "2025-08": 34.0,
      "2025-09": 0.0,
      "2025-10": 34.0,
      "2025-11": 0.0,
      "2025-12": 0.0,
      "2026-01": 0.0,
      "2026-02": 0.0,
      "2026-03": 0.0
     },
     "ATOSTAR": {
      "2025-04": 161.0,
      "2025-05": 266.0,
      "2025-06": 77.0,
      "2025-07": 38.0,
      "2025-08": 0.0,
      "2025-09": 43.0,
      "2025-10": 115.0,
      "2025-11": 0.0,
      "2025-12": 41.0,
      "2026-01": 0.0,
      "2026-02": 0.0,
      "2026-03": 0.0
     },
     "CRERATO": {
      "2025-04": 39.0,
      "2025-05": 0.0,
      "2025-06": 0.0,
      "2025-07": 0.0,
      "2025-08": 0.0,
      "2025-09": 0.0,
      "2025-10": 0.0,
      "2025-11": 0.0,
      "2025-12": 0.0,
      "2026-01": 0.0,
      "2026-02": 0.0,
      "2026-03": 0.0
     },
     "CRESANTE": {
      "2025-04": 26.0,
      "2025-05": 0.0,
      "2025-06": 0.0,
      "2025-07": 0.0,
      "2025-08": 142.0,
      "2025-09": 0.0,
      "2025-10": 0.0,
      "2025-11": 0.0,
      "2025-12": 0.0,
      "2026-01": 0.0,
      "2026-02": 0.0,
      "2026-03": 0.0
     },
     "CRESTIN": {
      "2025-04": 48.0,
      "2025-05": 0.0,
      "2025-06": 0.0,
      "2025-07": 0.0,
      "2025-08": 0.0,
      "2025-09": 0.0,
      "2025-10": 0.0,
      "2025-11": 0.0,
      "2025-12": 0.0,
      "2026-01": 53.0,
      "2026-02": 51.0,
      "2026-03": 0.0
     },
     "CRESTOR": {
      "2025-04": 1040.0,
      "2025-05": 1212.0,
      "2025-06": 639.0,
      "2025-07": 754.0,
      "2025-08": 1038.0,
      "2025-09": 803.0,
      "2025-10": 889.0,
      "2025-11": 662.0,
      "2025-12": 1196.0,
      "2026-01": 878.0,
      "2026-02": 1356.0,
      "2026-03": 1017.0
     },
     "CREVATIN": {
      "2025-04": 39.0,
      "2025-05": 130.0,
      "2025-06": 0.0,
      "2025-07": 58.0,
      "2025-08": 50.0,
      "2025-09": 0.0,
      "2025-10": 0.0,
      "2025-11": 0.0,
      "2025-12": 0.0,
      "2026-01": 0.0,
      "2026-02": 0.0,
      "2026-03": 0.0
     },
     "HUSTAR": {
      "2025-04": 32.0,
      "2025-05": 0.0,
      "2025-06": 72.0,
      "2025-07": 0.0,
      "2025-08": 0.0,
      "2025-09": 0.0,
      "2025-10": 0.0,
      "2025-11": 0.0,
     
...(요약 절단)
```

## ba_interest_rx
- 요청: `POST /api/brand-activity/interest-rx-matrix`
- request body: `{"view": "general", "selected_brand": "리바로", "filters": {"atc": {"atc4": ["C10A1"]}}}`
- ts_utc: 2026-07-17T09:17:00.034630Z  | status: 200  | bytes: 5551
- resp headers: `{"content-type": "application/json"}`
- 응답(구조 보존 요약):
```json
{
 "data": {
  "scope": {
   "view": "general",
   "market_id": "C10A1",
   "market_name": "STATINS (HMG-COA RED)",
   "selected_brand": "리바로",
   "csd_market": "LIVALO",
   "ranking_quarter": "2026-Q1",
   "applied_filter": {
    "atc4": [
     "C10A1"
    ]
   },
   "applied_filters": {
    "atc4": [
     "C10A1"
    ]
   },
   "resolved_market": {
    "type": "general",
    "market_id": "C10A1",
    "market_label": "STATINS (HMG-COA RED)",
    "source": "filters"
   }
  },
  "filters_applied": {
   "visit_location": "전체",
   "specialty": "전체",
   "period_start": "2023-06",
   "period_end": "2026-05"
  },
  "period": {
   "start": "2023-06",
   "end": "2026-05",
   "default_start": "2023-06",
   "default_end": "2026-05",
   "source": "dynamic_overlap"
  },
  "levels": {
   "interest": [
    "VERY USEFUL",
    "...(3개 중 1개)"
   ],
   "rx_frequency": [
    "frequently",
    "...(5개 중 1개)"
   ],
   "prescription_evolution": [
    "increase (or will begin to prescribe)",
    "...(3개 중 1개)"
   ]
  },
  "weights": {
   "interest": {
    "VERY USEFUL": 1.0,
    "SOMEWHAT USEFUL": 0.5,
    "NOT AT ALL": 0.0
   },
   "rx_frequency": {
    "frequently": 1.0,
    "occasionally": 0.6,
    "lapsed user": 0.3,
    "never": 0.0,
    "new to me, thus never prescribed": 0.0
   },
   "prescription_evolution": {
    "increase (or will begin to prescribe)": 1.0,
    "remain unchanged": 0.5,
    "decrease": 0.0
   }
  },
  "brands": [
   {
    "brand_key": "리바로",
    "brand_name": "리바로",
    "product_code": "LIVALO",
    "is_selected": true,
    "is_jw": true,
    "sales_rank": 3,
    "detailing": 80554.0,
    "interest_distribution": {
     "VERY USEFUL": 266,
     "SOMEWHAT USEFUL": 1031,
     "NOT AT ALL": 32
    },
    "rx_frequency_distribution": {
     "frequently": 412,
     "occasionally": 799,
     "lapsed user": 33,
     "never": 72,
     "new to me, thus never prescribed": 13
    },
    "prescription_evolution_distribution": {
     "increase (or will begin to prescribe)": 678,
     "remain unchanged": 639,
     "decrease": 12
    },
    "event_count": 1329,
    "confidence": "sufficient",
    "interest_score": 0.5880361173814899,
    "rx_frequency_score": 0.6781790820165537,
    "prescription_evolution_score": 0.7505643340857788
   },
   "...(6개 중 1개)"
  ],
  "market_average": {
   "interest_distribution": {
    "VERY USEFUL": 1263,
    "SOMEWHAT USEFUL": 3809,
    "NOT AT ALL": 139
   },
   "rx_frequency_distribution": {
    "frequently": 2400,
    "occasionally": 2551,
    "lapsed user": 82,
    "never": 153,
    "new to me, thus never prescribed": 25
   },
   "prescription_evolution_distribution": {
    "increase (or will begin to prescribe)": 2974,
    "remain unchanged": 2196,
    "decrease": 41
   },
   "event_count": 5211,
   "confidence": "sufficient",
   "interest_score": 0.607848781423911,
   "rx_frequency_score": 0.7590097869890615,
   "prescription_evolution_score": 0.7814239109575897
  }
 },
 "meta": {
  "request_normalized": true
 }
}
```

## ba_market_not_found_404
- 요청: `POST /api/brand-activity/topics`
- request body: `{"view": "general", "selected_brand": "리바로", "filters": {}}`
- ts_utc: 2026-07-17T09:17:00.155886Z  | status: 400  | bytes: 123
- 응답(구조 보존 요약):
```json
{
 "detail": {
  "error": "invalid_brand_activity_topic_request",
  "message": "view, filters.atc4, and selected_brand are required"
 }
}
```

---

## ingest hook (별도 서비스 jw-ingest-hook, 배포됨 · 리허설 격리 모드) — read-only GET 캡처

- 재실측 2026-07-17T09:59:07Z. deployment `jw-ingest-hook`(ns llmops, 1/1, age ~36m), svc `jw-ingest-hook` ClusterIP 10.13.33.50:8080/TCP, 컨테이너 `trigger`, 이미지 `jw-pipeline-orchestrator@sha256:fea29685…`.
- env(발췌): `INGEST_S3_BUCKET=`(공란→로컬 INPUT_ROOT 모드), `MINIO_ENDPOINT=http://minio.llmops.svc.cluster.local:9000`, `MARIADB_DATABASE=jw_mart_d2_stage_20260630_r2`, `INGEST_REHEARSAL_ROOT=/tmp/ingest-rehearsal`(리허설 격리 모드), `INGEST_LEDGER_SQLITE` 미설정→mysql ledger.
- 호출: `kubectl exec deploy/jw-market-backend-api -- python3`(in-mesh) → `http://jw-ingest-hook.llmops:8080`. ★POST(webhook·reconcile)는 부작용으로 미호출.

### ingest_healthz
- 요청: `GET /healthz`
- status: 200
```json
{"ok": true}
```

### ingest_status_probe
- 요청: `GET /ingest/status?epoch=2026-W27&category=probe&manifest_sha=<64x0>` (존재하지 않는 probe 식별자)
- status: 404
```json
{"detail": "unknown submission identity"}
```
- 주: 500이 아닌 404 = mysql ledger 조회가 정상 실행됨(ingest_ledger 테이블 존재/질의 가능). 정상 조회 시 {epoch, category, manifest_sha, status, reason, job_name, uploaded_by, received_at, finished_at} 9필드 반환(app.py:107).
