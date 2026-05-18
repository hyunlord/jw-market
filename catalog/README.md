# catalog/README.md

JW Mart Pipeline · catalog yaml (v0.8) — **ml 16 + cd 19 분리**

본 catalog 는 STEP 2 enrichment 의 자동 처리 rule + 시장별 metadata + customer dictionary 를 정의합니다.

---

## 갯수 (★ source-level · Phase 14)

| 항목 | count | 정의 |
|---|---|---|
| `ml_market` | **16** | market_landscape 용 (★ MI Master detail 시트 단위) |
| `cd_market` | **19** | competitive_dynamics 용 (★ brand 직접 경쟁시장 단위) |
| `cd_brand` | 2,379 | cd_id 별 brand list |
| `cd_filter` | 19 | cd 별 raw 필터 조건 |
| `strategic_brand` | 4,495 | detail 시트 entries (brand-level) |
| `strategic_product` | 11,865 | detail 시트 entries (product-level) |
| MI Master sheets | 19 (★ 3 non-detail + 16 detail) | |

---

## 폴더 구조

```
catalog/
├── enrichment_rules.yaml      # column 별 behavior (★ 6 behavior + exclude rule)
├── customer_dictionary.yaml   # UBIST 종별/진료과 매핑 + IQVIA AUDIT CODE
├── market_metadata.yaml       # ★ ml 16 + cd 19 분리 정의 + ml↔cd 매핑
├── market_overrides.yaml      # ★ 16 ml detail 시트 column + cd 분리 logic
├── compute_functions/         # Python — compute behavior 의 실제 logic
│   ├── strength_tier.py
│   ├── brand_age.py
│   ├── mat_period.py
│   ├── derive_channel.py
│   └── derive_specialty.py
└── README.md
```

---

## ml ↔ cd 매핑 요약

| Phase 14 ml | label | → | cd 분리 |
|---|---|---|---|
| ml_001 ~ ml_007 | 라베칸~리바로페노 | → | 1:1 (cd_001~cd_007) |
| **ml_008** 리바로하이/브이 | → | **cd_008 + cd_009** ★ |
| **ml_009** 트루패스/피나스타/제이다트 | → | **cd_010 + cd_011** ★ |
| **ml_010** 뉴트로진/모빌리아 | → | **cd_012 + cd_013** ★ |
| ml_011 악템라 | → | cd_014 |
| ml_012 페린젝트/베노훼럼 | → | cd_015 (★ collapse) |
| ml_013 헴리브라 | → | cd_016 |
| **ml_014** 위너프 | → | **cd_018** (★ cd_id swap) |
| **ml_015** 엔커버 | → | **cd_017** (★ cd_id swap, analyze_class=False) |
| ml_016 플라주오피 | → | cd_019 |

---

## file 별 역할

### enrichment_rules.yaml — column 처리 logic

6 behavior:
- `add_only` — raw 에 없는 column. canonical 만 추가
- `overwrite_if_canonical_present` — canonical 있으면 덮어씌움
- `preserve_raw` — canonical 무시
- `compute` — function 으로 derive
- `undefined` — 해당 axis 분석 안 함 (★ analyze_* = False)
- `column_level_null` — "제외" 라벨 처리 (★ v7 NEW)

### customer_dictionary.yaml — channel/specialty 매핑

UBIST raw (★ 한국어 종별/진료과) → MI팀 표기 변환:
- 종별: 상급종합병원→TH / 종합병원→GH / 병원→Semi / 의원→CL / 보건소·기타→기타
- 진료과: 가정의학과·내과·일반의→IGF (★ 1:3) / 순환기→Cardio / etc

IQVIA: AUDIT CODE prefix 기반 자동 추적 (★ KHPA/KCPA/KPA)

### market_metadata.yaml — ml 16 + cd 19 분리 정의

- `markets`: ml_001 ~ ml_016 (★ 16 row, Phase 14 column 그대로)
- `competitive_dynamics`: cd_001 ~ cd_019 (★ 19 row)
- `ml_cd_mapping`: ml ↔ cd 매핑 표
- `detail_sheets`: MI Master 260518 의 16 detail 시트 이름 list
- `metric_definitions`: Market Share / MoM / QoQ / YoY / G/R / MAT

### market_overrides.yaml — detail 시트 column 매핑

- 시장별 raw column 매핑 (★ Class, Molecule, Strength 등)
- `exclude_check` 적용 column 명시
- `cd_split`: 한 ml 안의 cd 분리 logic (★ 리바로하이/브이 등 3 시장)
- `cd_collapse`: 페린젝트/베노훼럼 한 cd 유지 logic
- `cd_id_swap`: Phase 14 의 cd_017↔ml_015, cd_018↔ml_014 swap 명시

---

## 사용 흐름 (★ STEP 2 enrichment)

1. **catalog 로드** — enrichment_rules + customer_dictionary + market_metadata + market_overrides
2. **시장 식별** — raw row 의 atc_code · brand 로 ml_id 식별
3. **column enrichment** — `column_rules` 따라 raw + canonical + compute
4. **"제외" 처리** — `exclude_check: true` column 에 "제외" 있으면 NULL
5. **customer 변환** — UBIST 종별/진료과 raw → MI팀 표기 변환
6. **cd 매핑** — ml_id 안의 cd_id 식별 (★ brand_filter)

---

## 변경 이력

- **v0.7** (★ deprecated): 19 ml_market 으로 잘못 작성 → ml + cd 혼동
- **v0.8** (★ current): ml 16 + cd 19 분리 (★ source-level audit 기반)

Source: `cd_market_19_audit_20260518_1437.zip` (Codex)
