# Master 16 시트 컬럼 사전 정의 v3 (★ 최종 — 사용자 결정 모두 반영)

**작성일**: 2026-05-07
**전제**: v10-phase-2-v2-json-staging 완료 후 Master 만 재설계
**작성자**: Claude (★ 사용자 24+ 결정 모두 통합)
**다음 단계**: 코덱스 적재 의뢰서 작성

---

## 0. ★ 사용자 추가 결정 (★ v2 → v3)

### ★ 결정 18 — 리바로 리바로젯 strength + 성분용량 별개 컬럼
> "리바로 리바로젯에서 strength와 성분용량은 별개의 column으로 넣어줘 strength를 strength로 넣어주고"

★ 표준 컬럼 추가:
- `strength` — UBIST raw 의 Strength 컬럼 그대로
- `formulation` — UBIST raw 의 성분용량 컬럼 그대로 (★ 강도 + 성분 결합 표현)

### ★ 결정 19 — 리바로하이 매핑 결과 모두 컬럼화
> "리바로하이 리바로브이에서 성분기반 질환 정의, 단일/복합, class, class recode 2 이런걸 column으로 넣어줘야 해"

★ 즉 ★ 매핑 테이블 (Q-V열, X-Z열) 의 ★ 결과를 ★ 약별 row 에 ★ 직접 컬럼으로 적용:
- 매핑 테이블이 ★ 별도 테이블에만 보존 X
- ★ 약별 row 에 ★ 매핑 결과 컬럼 ★ 추가

```
약 row 에:
  - molecule_disease_definition (성분기반 질환 정의)
  - composition_type (단일/복합)
  - class (Class Recode)
  - class_2 (Class Recode 분류2 — 리바로하이는 class_2 도 의미 있음)
```

### ★ 결정 20 — 시트별 description / memo 컬럼
> "각 시트별 description이나 memo같은 column으로 줄글로 상세 내용 적어놓는 곳도 있으면 좋을듯. 이걸 바탕으로 이후 시장 정의나 통계 계산할때 쓸수도 있으니"

★ 신규 컬럼:
- `stg_master_market_definition.description` (★ 시장별 줄글 메모)
- 향후 LLM 이 ★ 시장 정의 / 통계 계산 시 ★ 컨텍스트 활용

### ★ 결정 21 — 라베칸 Funnel = ★ 시장 분석 level 맞음
> "funnel은 시장정의 & target 시트 내용을 저장할때 시장 분석 level이 아닐까? <- 그거 맞을것 같아"

★ ★ funnel 은 ★ stg_master_drug 컬럼 X — ★ stg_master_market_definition 의 ★ analysis_level_funnel 또는 ★ analysis_level_etc.

★ 단 라베칸 시트 자체에 ★ Funnel 컬럼 (약별 값) 이 있음 — ★ 의미:
- 시장 분석 level 자체 = "Funnel 단계로 분석" (★ 메타)
- 약별 Funnel 값 = ★ 각 약이 어느 Funnel 단계에 속하는지 (★ raw)

★ 권고: ★ 둘 다 보존:
- stg_master_market_definition: 라베칸 시장의 ★ analysis_level_funnel = "O" (★ funnel level 로 분석)
- stg_master_drug: 라베칸 시장의 약별 ★ funnel 컬럼 = 각 약의 funnel 단계

### ★ 결정 22 — Master 가 raw 와 겹치는 컬럼이면 ★ Master 가 우선 (덮어쓰기)
> "일단 여기 시트에 있는게 기존에 있는 데이터와 겹치는거라면 여기 시트에 있는값으로 덮어씌워야함"

★ ★ ★ 매우 중요한 일반 정책:

**적용 우선 순위 (★ 향후 Phase 3 mart 단계)**:
```
Master 시트 값 > IQVIA / UBIST raw 값
```

★ 즉:
- Master 의 NHI TYPE → IQVIA raw NHI TYPE 덮어쓰기
- Master 의 ATC 4 CODE → IQVIA raw ATC 4 CODE 덮어쓰기 (★ 같으면 영향 X / 다르면 Master 우선)
- Master 의 MOLECULE DESC → raw MOLECULE DESC 덮어쓰기
- 등등

★ ★ Master 가 ★ "사람이 검토한 정확한 값" 이라는 의미 (★ 사용자 의도).

★ 이건 column_metadata_json 에 명시:
```json
{
  "nhi_type": {
    "type": "raw_with_master_override",
    "source_column": "NHI TYPE",
    "override_priority": "master_first"
  }
}
```

### ★ 결정 23 — 악템라 ★ 별도 brand_group 방법
> "저부분만 뭉쳐줄수 있는 걸 알수있는 별도 방법이있어야할듯, 테이블이더라도?"

★ 신규 stg_master_brand_consolidation 테이블:

```sql
CREATE TABLE stg_master_brand_consolidation (
  strategic_market_id VARCHAR(50),       -- 'strategy_011' 악템라
  brand_group VARCHAR(255),              -- '엔브렐' / '오렌시아' / '젤잔즈'
  member_drug_index INT,                 -- 멤버 약 (★ stg_master_drug.drug_index 참조)
  member_drug_name VARCHAR(255),         -- '엔브렐마이클릭' / '엔브렐' / 등
  PRIMARY KEY (strategic_market_id, brand_group, member_drug_index),
  source_remark TEXT,                    -- raw remark 원문
  ingested_at DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

★ 악템라 의 사례:
| brand_group | member_drug |
|---|---|
| 엔브렐 | 엔브렐마이클릭 |
| 엔브렐 | 엔브렐 |
| 오렌시아 | 오렌시아 |
| 오렌시아 | 오렌시아서브큐 |
| 젤잔즈 | 젤잔즈 |
| 젤잔즈 | 젤잔즈엑스알 |

### ★ 결정 24 — 위너프 strength_recode 표준 + raw 보존
> "위너프는 strength와 strength2가 큰 차인 없는듯? 그런데 막상 실제 통계내는건 규격 recode에 있는걸 낼거라 저걸 strength로 넣고 기존 strength, strenght2는 별개의 저장용 컬럼으로만 남겨두기"

★ 표준:
- `strength` ← ★ "규격 Recode" 값 (★ 통계 / mart 분석 사용)
- `strength_raw_1` ← raw "Strength"
- `strength_raw_2` ← raw "Strength2"

★ 이건 ★ 결정 14 (★ "strength는 그냥 규격 recode로 변경하고, 기존 strength는 따로 저장용으로만") 의 자세화. 단 v3 에서는 ★ "strength" 가 ★ 표준 통계 컬럼.

---

## 1. ★ 새 schema v3 (★ 24 결정 모두 반영)

### 1-1. stg_master_drug

```sql
CREATE TABLE stg_master_drug (
  -- 시장 단위
  strategic_market_id VARCHAR(50),
  market_name VARCHAR(255),
  source_type VARCHAR(20),                       -- 'IQVIA' | 'UBIST'
  -- 약 단위
  drug_index INT,
  -- ★ 표준 컬럼 (★ raw 또는 manual_overlay 또는 manual_added 의 ★ 최종 표준값)
  atc4_code VARCHAR(50),                         -- ATC4 CODE 또는 [코드] 형식
  atc4_desc VARCHAR(255),
  molecule VARCHAR(500),                         -- 성분 (★ raw 또는 overlay 결과)
  product_name VARCHAR(255),                     -- 제품명
  manufacturer VARCHAR(255),                     -- 제조사
  seller VARCHAR(255),                           -- 판매사 (UBIST)
  pack_desc VARCHAR(500),                        -- 팩 설명
  nhi_type VARCHAR(50),                          -- 보험 (★ Master overlay)
  -- ★ 분류 (수동 매핑)
  class VARCHAR(255),                            -- Class Recode 또는 Class
  class_2 VARCHAR(255),                          -- 추가 분류 (악템라 / 리바로하이 등)
  -- ★ 제형 / 강도
  dosage_form VARCHAR(100),                      -- 제형
  strength VARCHAR(255),                         -- ★ 통계 표준 (★ 규격 Recode 또는 raw Strength)
  strength_raw VARCHAR(255),                     -- raw Strength (보존용)
  strength_raw_2 VARCHAR(255),                   -- raw Strength2 (위너프 등)
  formulation VARCHAR(500),                      -- 성분용량 (리바로 시리즈)
  -- ★ 시장 분석 메타 (★ 약 단위)
  funnel VARCHAR(100),                           -- Funnel 단계 (라베칸 약별)
  -- ★ 매핑 결과 (★ 리바로하이 등 — 결정 19)
  molecule_disease_definition VARCHAR(255),      -- 성분기반 질환 정의
  composition_type VARCHAR(50),                  -- 단일/복합
  -- ★ 시트별 특수 정보
  drug_extra_json LONGTEXT,                      -- {"generation": "...", "fish_oil_yn": "Y", "fe_content_per_ml": 50, ...}
  -- ★ 컬럼 메타 (★ 사용자 결정 22 — overlay 우선순위)
  column_metadata_json LONGTEXT,                 -- {표준컬럼명: {source_column, type, overlay_target, override_priority}}
  -- 메타
  source_file_version VARCHAR(500),
  ingested_at DATETIME,
  PRIMARY KEY (strategic_market_id, drug_index),
  CHECK (JSON_VALID(drug_extra_json)),
  CHECK (JSON_VALID(column_metadata_json))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 1-2. stg_master_market_definition (★ 결정 3, 17, 20, 21)

```sql
CREATE TABLE stg_master_market_definition (
  strategic_market_id VARCHAR(50) PRIMARY KEY,
  market_name VARCHAR(255),
  team VARCHAR(50),                              -- 'MKT 1팀' 등
  -- 시장 메타
  primary_atc4_code VARCHAR(50),
  primary_atc4_desc VARCHAR(255),
  nhi_type VARCHAR(50),
  data_source VARCHAR(100),                      -- 'Ubist' / 'Iqvia' / 'Ubist / IQVIA'
  analysis_metrics VARCHAR(500),
  -- 시장 분석 Level
  analysis_level_class VARCHAR(50),              -- 'O' / null
  analysis_level_molecule VARCHAR(50),
  analysis_level_brand VARCHAR(50),
  analysis_level_dosage_form VARCHAR(50),
  analysis_level_strength VARCHAR(50),
  analysis_level_funnel VARCHAR(50),             -- ★ 라베칸 (결정 21)
  analysis_level_etc VARCHAR(255),               -- '급여/비급여' / 'FISH OIL 여부' / 'Biologics (Ox/Biosimilar)'
  -- 시장 정의 (★ 결정 3, 17)
  full_market_atc4_codes_json LONGTEXT,          -- 전체 치료 시장 ATC list
  brand_direct_competition_json LONGTEXT,        -- 브랜드 직접 경쟁 시장 정의
  target_customer_priority_json LONGTEXT,        -- Target Customer Priority
  analysis_constraint_json LONGTEXT,             -- 분석 제약 (예: 페린젝트 "iv iron only")
  -- ★ 시장 description / memo (★ 결정 20)
  description TEXT,                              -- 시장별 줄글 메모 (LLM 컨텍스트 활용)
  -- 메타
  source_file_version VARCHAR(500),
  ingested_at DATETIME,
  CHECK (JSON_VALID(full_market_atc4_codes_json)),
  CHECK (JSON_VALID(brand_direct_competition_json)),
  CHECK (JSON_VALID(target_customer_priority_json)),
  CHECK (JSON_VALID(analysis_constraint_json))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

★ row 수 = ★ 정확 16

### 1-3. stg_master_mapping_table (★ 결정 10)

★ 매핑 테이블 ★ 별도 보존 (★ 추적 + 향후 갱신용):

```sql
CREATE TABLE stg_master_mapping_table (
  strategic_market_id VARCHAR(50),
  mapping_table_index INT,                       -- 매핑 테이블 순번 (1, 2, ...)
  mapping_key_column VARCHAR(100),               -- 매핑 키 (예: '성분')
  mapping_index INT,                             -- entry 순번
  mapping_key_value VARCHAR(500),                -- (예: 'rosuvastatin')
  mapping_attributes_json LONGTEXT,              -- {target_column: target_value, ...}
  source_file_version VARCHAR(500),
  ingested_at DATETIME,
  PRIMARY KEY (strategic_market_id, mapping_table_index, mapping_index),
  CHECK (JSON_VALID(mapping_attributes_json))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 1-4. stg_master_brand_consolidation (★ 신규 — 결정 23)

```sql
CREATE TABLE stg_master_brand_consolidation (
  strategic_market_id VARCHAR(50),
  brand_group VARCHAR(255),                      -- '엔브렐' / '오렌시아' / '젤잔즈'
  member_drug_index INT,                         -- stg_master_drug.drug_index 참조
  member_drug_name VARCHAR(255),
  source_remark TEXT,                            -- raw remark 원문
  ingested_at DATETIME,
  PRIMARY KEY (strategic_market_id, brand_group, member_drug_index)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 1-5. stg_master_qa

```sql
CREATE TABLE stg_master_qa (
  qa_index INT PRIMARY KEY,
  question_type VARCHAR(50),
  market_name VARCHAR(255),
  strategic_market_id VARCHAR(50),
  question TEXT,
  answer TEXT,
  marketing_note TEXT,
  application_actions_json LONGTEXT,             -- 자동 적용 액션
  source_file_version VARCHAR(500),
  ingested_at DATETIME,
  CHECK (JSON_VALID(application_actions_json))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

---

## 2. ★ 16 시트 컬럼 사전 정의 v3 (★ 최종)

### Sheet 1 — 라베칸 라베칸듀오 (UBIST, header_row=4) — **strategy_001**

drug rows: 358

| raw 컬럼명 | 표준 컬럼 | type | 적용 |
|---|---|---|---|
| ATC | atc4_code | raw | |
| 성분 | molecule | raw | |
| 판매사 | seller | raw | |
| 제품 | product_name | raw | |
| 제조사 | manufacturer | raw | |
| Class Recode | class | manual_added | |
| 성분 Recode | molecule | manual_overlay | molecule 덮어쓰기 |
| Funnel | **funnel** | manual_added | ★ 약별 Funnel 단계 (결정 21) |

★ ★ stg_master_market_definition (라베칸 시장):
- analysis_level_funnel = 'O'

---

### Sheet 2 — 제이클 (IQVIA, header_row=4) — **strategy_002**

drug rows: 45

| raw 컬럼명 | 표준 컬럼 | type | 적용 |
|---|---|---|---|
| ATC 4 CODE | atc4_code | raw | |
| ATC 4 DESC | atc4_desc | raw | |
| MOLECULE DESC | molecule | raw | |
| PRODUCT NAME KOR | product_name | raw | |
| MFR NAME KOR | manufacturer | raw | |
| **NHI TYPE** | nhi_type | **raw_with_master_override** | ★ 결정 4-2 — Master 우선 |
| Recode 제형 | dosage_form | manual_added | |
| Recode Class(성분) | class | manual_added | |
| ~~Remark~~ | — | ★ 제거 | Q&A 적용 |

★ Q&A 적용 (수프렙미니에스 → class = "Trisulfate")

---

### Sheet 3 — 가드렛 가드메트 (IQVIA, header_row=4) — **strategy_003**

drug rows: 82

| raw 컬럼명 | 표준 컬럼 | type |
|---|---|---|
| ATC 4 CODE | atc4_code | raw |
| ATC 4 DESC | atc4_desc | raw |
| MOLECULE DESC | molecule | raw |
| Class Recode | class | manual_added |
| 제형 Recode | dosage_form | manual_added |
| ~~Remark~~ | — | ★ 제거 (Q&A 적용) |

★ Q&A 적용 (TIRZEPATIDE → class = "GLP-1RA")

---

### Sheet 4 — 타발리스 (IQVIA, header_row=4) — **strategy_004**

drug rows: 10

| raw 컬럼명 | 표준 컬럼 | type |
|---|---|---|
| ATC 4 CODE | atc4_code | raw |
| ATC 4 DESC | atc4_desc | raw |
| MOLECULE DESC | molecule | raw |
| PRODUCT NAME KOR | product_name | raw |
| MFR NAME KOR | manufacturer | raw |
| PACK DESC | pack_desc | raw |
| Class Recode | class | manual_added |

---

### Sheet 5 — 시그마트 (IQVIA, header_row=4) — **strategy_005**

drug rows: 294

| raw 컬럼명 | 표준 컬럼 | type | 적용 |
|---|---|---|---|
| ATC | drug_extra_json["ubist_atc"] | manual_supplementary | (★ IQVIA 에 없는 추가) |
| 성분 | drug_extra_json["ubist_molecule"] | manual_supplementary | |
| 판매사 | drug_extra_json["ubist_seller"] | manual_supplementary | |
| 제품 | product_name | raw | |
| 제조사 | manufacturer | raw | |
| ~~처방조제액(원) 2026년 3월~~ | — | ★ 제거 | |
| ~~처방량_P 2026년 3월~~ | — | ★ 제거 | |
| Class Recode | class | manual_added | |
| Molecule Recode | molecule | manual_overlay | |

---

### Sheet 6 — 리바로 리바로젯 (UBIST, header_row=3) — **strategy_006**

drug rows: 1,095

| raw 컬럼명 | 표준 컬럼 | type | 적용 |
|---|---|---|---|
| ATC | atc4_code | raw | |
| 성분 | molecule | raw | |
| 판매사 | seller | raw | |
| 제품 | product_name | raw | |
| 제조사 | manufacturer | raw | |
| **성분용량** | **formulation** | raw | ★ 결정 18 — 별도 컬럼 |
| ~~2026년 3월~~ | — | ★ 제거 | |
| Molecule | drug_extra_json["molecule_eng"] | raw_supplementary | |
| ~~Ox/Gx~~ | — | ★ 제거 | |
| **Strength** | **strength** | raw | ★ 결정 18 — 별도 컬럼 |
| **★ "제외" row** | — | ★ 시장에서 제거 | (전역) |

---

### Sheet 7 — 리바로페노 (UBIST, header_row=3) — **strategy_007**

drug rows: 611

| raw 컬럼명 | 표준 컬럼 | type | 적용 |
|---|---|---|---|
| ATC | atc4_code | raw | |
| 성분 | molecule | raw | |
| 판매사 | seller | raw | |
| 제품 | product_name | raw | |
| 제조사 | manufacturer | raw | |
| 성분용량 | formulation | raw | (결정 18 동일) |
| ~~처방조제액(원) 2026년 3월~~ | — | ★ 제거 | |
| Class | drug_extra_json["class_raw"] | raw_supplementary | |
| Molecule | drug_extra_json["molecule_eng"] | raw_supplementary | |
| ~~Ox/Gx~~ | — | ★ 제거 | |

---

### Sheet 8 — 리바로하이 리바로브이 (UBIST, header_row=4) — **strategy_008**

drug rows: 1,081

★ ★ ★ ★ ★ 결정 19: 매핑 결과 ★ 컬럼화

#### A-L 영역 (실제 데이터)

| raw 컬럼 | 표준 컬럼 | type |
|---|---|---|
| ATC | atc4_code | raw |
| 성분 | molecule | raw |
| 판매사 | seller | raw |
| 제품 | product_name | raw |
| 제조사 | manufacturer | raw |
| ~~처방조제액(원) 2026년 3월~~ | — | ★ 제거 |
| ~~처방량_P 2026년 3월~~ | — | ★ 제거 |
| TA | drug_extra_json["ta"] | raw_supplementary |
| Type | drug_extra_json["type_raw"] | raw_supplementary |
| Class | drug_extra_json["class_raw_lev"] | raw_supplementary |
| Molecule | drug_extra_json["molecule_eng"] | raw_supplementary |
| Ox/Gx | drug_extra_json["ox_gx"] | raw_supplementary |
| Strength | strength | raw |

#### Q-V 영역 매핑 결과 (★ 약 row 에 ★ 컬럼화)

| 표준 컬럼 | source (매핑 테이블 1) | 적용 |
|---|---|---|
| **molecule_disease_definition** | "질환 정의 Recode" 매핑 | ★ 결정 19 |
| **composition_type** | "단일/복합 Recode" 매핑 | ★ 결정 19 |
| **class** | "Class Recode" 매핑 | ★ 결정 19 |
| molecule | "성분 Recdoe" / "성분 Recode" 매핑 | manual_overlay |

#### X-Z 영역 매핑 결과 (★ 약 row 에 ★ 컬럼화)

| 표준 컬럼 | source (매핑 테이블 2) | 적용 |
|---|---|---|
| **class_2** | "Class Recode 분류2" 매핑 | ★ 결정 19 |

★ ★ 단 매핑 테이블 자체도 ★ stg_master_mapping_table 보존 (★ 향후 갱신 추적).

---

### Sheet 9 — 트루패스 피나스타 제이다트 (UBIST, header_row=4) — **strategy_009**

drug rows: 406

| raw 컬럼 | 표준 컬럼 | type |
|---|---|---|
| ATC | atc4_code | raw |
| 성분 | molecule | raw |
| 판매사 | seller | raw |
| 제품 | product_name | raw |
| 제조사 | manufacturer | raw |
| ~~처방조제액(원) 2026년 3월~~ | — | ★ 제거 |
| ~~처방량_P 2026년 3월~~ | — | ★ 제거 |
| Class | drug_extra_json["class_raw"] | raw_supplementary |
| Molecule | drug_extra_json["molecule_eng"] | raw_supplementary |

---

### Sheet 10 — 뉴트로진 모빌리아 (IQVIA, header_row=4) — **strategy_010**

drug rows: 10

| raw 컬럼 | 표준 컬럼 | type | 적용 |
|---|---|---|---|
| ATC 4 CODE | atc4_code | raw | |
| ATC 4 DESC | atc4_desc | raw | |
| MOLECULE DESC | molecule | raw | |
| NHI TYPE | nhi_type | raw_with_master_override | ★ 결정 4-2 |
| MFR NAME KOR | manufacturer | raw | |
| PRODUCT NAME KOR | product_name | raw | |
| Class | class | manual_added | |
| Generation | drug_extra_json["generation"] | manual_added | |
| TA | drug_extra_json["ta"] | manual_added | |
| ~~Remark~~ | — | ★ 제거 (Q&A) | |

★ Q&A 적용 (듀라스틴 → molecule = "TRIPEGFILGRASTIM")

---

### Sheet 11 — 악템라 (IQVIA, header_row=4) — **strategy_011**

drug rows: 26

| raw 컬럼 | 표준 컬럼 | type | 적용 |
|---|---|---|---|
| ATC 4 CODE | atc4_code | raw | |
| ATC 4 DESC | atc4_desc | raw | |
| MOLECULE DESC | molecule | raw | |
| PRODUCT NAME KOR | product_name | raw | |
| MFR NAME KOR | manufacturer | raw | |
| Class Recode 1 | class | manual_added | (★ 상세) |
| Class Recode 2 | class_2 | manual_added | (★ 묶음 — 시각화 권장) |
| Ox/Gx (바이오시밀러) | drug_extra_json["ox_gx_biosimilar"] | manual_added | |
| **Remark** | — | **★ 별도 stg_master_brand_consolidation** | ★ 결정 23 |

★ ★ Brand consolidation:
- 엔브렐 (엔브렐, 엔브렐마이클릭)
- 오렌시아 (오렌시아, 오렌시아서브큐)
- 젤잔즈 (젤잔즈, 젤잔즈엑스알)

---

### Sheet 12 — 페린젝트 베노훼럼 (IQVIA, header_row=4) — **strategy_012**

drug rows: 76

#### A-O 영역

| raw 컬럼 | 표준 컬럼 | type | 적용 |
|---|---|---|---|
| ATC 4 CODE | atc4_code | raw | |
| ATC 4 DESC | atc4_desc | raw | |
| MOLECULE DESC | molecule | raw | |
| NHI TYPE | nhi_type | raw_with_master_override | ★ 결정 4-2 |
| MFR NAME KOR | manufacturer | raw | |
| PRODUCT NAME KOR | product_name | raw | |
| PACK DESC | pack_desc | raw | |
| Dosage Form | dosage_form | manual_added | |
| Molecule | drug_extra_json["molecule_eng"] | manual_added | (★ iv iron 만 분석) |
| 1ml 당 Fe 함량 | drug_extra_json["fe_content_per_ml"] | manual_added | (★ iv iron 만) |
| Strength | strength | manual_added | (★ iv iron 만) |

★ ★ stg_master_market_definition:
- analysis_constraint_json: `{"iv_iron_only_columns": ["molecule_eng", "fe_content_per_ml", "strength"]}`

#### P-V 영역

★ stg_master_mapping_table 별도 (★ 컬럼 추가 X — 결정 13)

---

### Sheet 13 — 헴리브라 (IQVIA, header_row=4) — **strategy_013**

drug rows: 14

| raw 컬럼 | 표준 컬럼 | type |
|---|---|---|
| ATC 4 CODE | atc4_code | raw |
| ATC 4 DESC | atc4_desc | raw |
| MOLECULE DESC | molecule | raw |
| NHI TYPE | nhi_type | raw_with_master_override |
| MFR NAME KOR | manufacturer | raw |
| PRODUCT NAME KOR | product_name | raw |
| Class | class | manual_added |

---

### Sheet 14 — 위너프 위너프A+ (IQVIA, header_row=4) — **strategy_014**

drug rows: 331

★ 결정 24: strength = 규격 Recode (★ 통계 표준)

| raw 컬럼 | 표준 컬럼 | type | 적용 |
|---|---|---|---|
| ATC 4 CODE | atc4_code | raw | |
| ATC 4 DESC | atc4_desc | raw | |
| MOLECULE DESC | molecule | raw | |
| NHI TYPE | nhi_type | raw_with_master_override | |
| MFR NAME KOR | manufacturer | raw | |
| PRODUCT NAME KOR | product_name | raw | |
| PACK DESC | pack_desc | raw | |
| PRODUCT NAME KORPACK DESC | drug_extra_json["product_pack"] | manual_added | |
| Strength | **strength_raw** | raw | ★ 보존용 |
| Strength2 | **strength_raw_2** | raw | ★ 보존용 |
| Class | class | manual_added | |
| Fish oil 여부 | drug_extra_json["fish_oil_yn"] | manual_added | (Q&A: N/A 처리) |
| 투여 경로 | drug_extra_json["administration_route"] | manual_added | (Q&A: N/A 처리) |
| **규격 Recode** | **strength** | manual_added | ★ 통계 표준 (결정 24) |

---

### Sheet 15 — 엔커버 (IQVIA, header_row=**6**) — **strategy_015**

| raw 컬럼 | 표준 컬럼 | type |
|---|---|---|
| ATC 4 CODE | atc4_code | raw |
| ATC 4 DESC | atc4_desc | raw |
| MOLECULE DESC | molecule | raw |
| NHI TYPE | nhi_type | raw_with_master_override |
| MFR NAME KOR | manufacturer | raw |
| PRODUCT NAME KOR | product_name | raw |

★ stg_master_market_definition.description:
- "IQVIA 기준 하모닐란과 엔커버 2개의 PRODUCT NAME KOR 에 대해 PACK DESC 를 하위분류로 4가지로 분석"

---

### Sheet 16 — 플라주오피 (IQVIA, header_row=4) — **strategy_016**

drug rows: 52

| raw 컬럼 | 표준 컬럼 | type | 적용 |
|---|---|---|---|
| ATC 4 CODE | atc4_code | raw | |
| ATC 4 DESC | atc4_desc | raw | |
| MOLECULE DESC | molecule | raw | |
| NHI TYPE | nhi_type | raw_with_master_override | |
| MFR NAME KOR | manufacturer | raw | |
| PRODUCT NAME KOR | product_name | raw | |
| **PACK DESC** | **strength** | raw → 표준 | ★ 결정 15 — strength_recode 처럼 |
| Class | class | manual_added | |
| **★ "제외" row** | — | ★ 시장에서 제거 | ★ J열 |

---

## 3. ★ 전역 정책 (★ 16 시트 모두 적용)

### 3-1. ★ 제외 row 자동 제거 (★ 결정 16)

★ 모든 시트의 ★ "제외" 표시된 row → ★ stg_master_drug 적재 X.

★ 코덱스 적재 logic:
- 각 시트의 raw row 읽음
- "제외" 단어 (또는 시트의 명시 컬럼 — 예: J열) 검사
- ★ 제외 표시 → skip
- ★ raw_logs 에 제외된 row 기록 (★ 추적성)

### 3-2. ★ 표준 컬럼명 (★ 결정 1)

✓ 위 schema 참고

### 3-3. ★ Master 우선 정책 (★ 결정 22)

★ ★ 표준 컬럼이 raw 와 겹치면 ★ Master 시트 값으로 ★ 덮어쓰기:

```
nhi_type:
  1순위: Master 시트의 NHI TYPE
  2순위: IQVIA / UBIST raw NHI TYPE

atc4_code:
  1순위: Master 시트의 ATC 4 CODE
  2순위: IQVIA / UBIST raw ATC 4 CODE

molecule:
  1순위: 성분 Recode (Master 수동 매핑)
  2순위: Master 시트 의 raw 성분 / MOLECULE DESC
  3순위: IQVIA / UBIST raw 성분
```

★ 향후 mart 단계 (Phase 3) 가 column_metadata_json 의 `override_priority: "master_first"` 보고 ★ 자동 적용.

### 3-4. ★ raw_metric 자동 제거 (★ 결정 7~11)

★ "처방조제액(원) 2026년 3월" / "처방량_P 2026년 3월" / "2026년 3월" 등 ★ 시점 metric 자동 제거.

### 3-5. ★ Ox/Gx 제거 (★ 결정 8, 9)

★ 리바로 / 리바로페노의 Ox/Gx 컬럼 ★ 제거.

### 3-6. ★ Q&A 자동 적용 (★ 결정 5, 6, 12)

★ stg_master_qa.application_actions_json 의 ★ 액션 자동 처리:
```json
[
  {"type": "override_class", "target_drug": "수프렙미니에스", "new_value": "Trisulfate"},
  {"type": "override_class", "target_molecule": "TIRZEPATIDE", "new_value": "GLP-1RA"},
  {"type": "override_molecule", "target_drug": "듀라스틴", "current": "PEGFILGRASTIM", "new_value": "TRIPEGFILGRASTIM"}
]
```

### 3-7. ★ 시트별 description (★ 결정 20)

★ stg_master_market_definition.description ★ 자세히:
- 시장 분석 의도
- 특수 처리 사항
- LLM 컨텍스트 활용 가치

### 3-8. ★ 매핑 테이블 별도 (★ 결정 10, 19)

★ ★ 두 가지 작업:
1. **매핑 테이블 자체** → stg_master_mapping_table
2. **매핑 결과** → stg_master_drug 의 ★ 표준 컬럼 (★ 결정 19)

★ 즉 ★ 적재 시 ★ 매핑 적용 → 약별 row 에 ★ 표준 컬럼 채움 + ★ 매핑 테이블 보존.

---

## 4. ★ column_metadata_json 예시

### 라베칸 약 row 의 column_metadata_json:
```json
{
  "atc4_code": {"type": "raw", "source_column": "ATC", "overlay_target": null},
  "molecule": {"type": "manual_overlay", "source_column": "성분 Recode", "overlay_target": "성분"},
  "seller": {"type": "raw", "source_column": "판매사", "overlay_target": null},
  "product_name": {"type": "raw", "source_column": "제품", "overlay_target": null},
  "manufacturer": {"type": "raw", "source_column": "제조사", "overlay_target": null},
  "class": {"type": "manual_added", "source_column": "Class Recode", "overlay_target": null},
  "funnel": {"type": "manual_added", "source_column": "Funnel", "overlay_target": null}
}
```

### 제이클 약 row 의 column_metadata_json:
```json
{
  "atc4_code": {"type": "raw_with_master_override", "source_column": "ATC 4 CODE", "override_priority": "master_first"},
  "atc4_desc": {"type": "raw_with_master_override", "source_column": "ATC 4 DESC", "override_priority": "master_first"},
  "molecule": {"type": "raw_with_master_override", "source_column": "MOLECULE DESC", "override_priority": "master_first"},
  "product_name": {"type": "raw_with_master_override", "source_column": "PRODUCT NAME KOR", "override_priority": "master_first"},
  "manufacturer": {"type": "raw_with_master_override", "source_column": "MFR NAME KOR", "override_priority": "master_first"},
  "nhi_type": {"type": "raw_with_master_override", "source_column": "NHI TYPE", "override_priority": "master_first"},
  "dosage_form": {"type": "manual_added", "source_column": "Recode 제형", "overlay_target": null},
  "class": {"type": "manual_added", "source_column": "Recode Class(성분)", "overlay_target": null}
}
```

### 리바로하이 약 row 의 column_metadata_json:
```json
{
  "atc4_code": {"type": "raw", "source_column": "ATC", "overlay_target": null},
  "molecule": {"type": "manual_overlay_via_mapping", "source_column": "성분 Recode", "overlay_target": "성분", "mapping_table_index": 1},
  "molecule_disease_definition": {"type": "manual_added_via_mapping", "source_column": "질환 정의 Recode", "mapping_table_index": 1},
  "composition_type": {"type": "manual_added_via_mapping", "source_column": "단일/복합 Recode", "mapping_table_index": 1},
  "class": {"type": "manual_added_via_mapping", "source_column": "Class Recode", "mapping_table_index": 1},
  "class_2": {"type": "manual_added_via_mapping", "source_column": "Class Recode 분류2", "mapping_table_index": 2}
}
```

---

## 5. ★ 코덱스 적재 의뢰서 작성 준비 완료

★ 본 v3 자료가 ★ 최종 사전 정의. ★ 사용자 ★ 마지막 검토 후 ★ 코덱스 적재 의뢰서 작성:

### 5-1. 적재 대상 (★ 5 staging table)

| Table | row 수 | 비고 |
|---|---:|---|
| stg_master_drug | ★ 약 4,500+ (★ "제외" row 제거 후) | 약 단위 |
| stg_master_market_definition | 16 | 시장 메타 |
| stg_master_mapping_table | 약 100+ (★ 리바로하이 2 + 페린젝트 1) | 매핑 테이블 |
| stg_master_brand_consolidation | 약 6+ (★ 악템라 등) | 브랜드 통합 |
| stg_master_qa | 14 | Q&A 액션 |

### 5-2. 의뢰서 핵심 포인트

- ★ 표준 컬럼명 (★ 결정 1)
- ★ Master 우선 정책 (★ 결정 22)
- ★ "제외" row 자동 제거 (★ 결정 16)
- ★ raw_metric 자동 제거 (★ 결정 7~11)
- ★ Q&A 자동 적용 (★ 결정 5, 6, 12)
- ★ 매핑 적용 (★ 리바로하이 — 결정 19)
- ★ 매핑 테이블 별도 (★ 결정 10)
- ★ 시장 메타 별도 (★ 결정 3, 17, 20, 21)
- ★ 브랜드 통합 별도 (★ 결정 23)
- ★ HTML 0건 (Anti-HTML)
- ★ 19 phase 산출물 보존

---

**v3 끝.**

★ ★ 사용자 ★ 마지막 검토 후 코덱스 의뢰서 작성합니다.

★ 추가 의문 / 수정 사항 있으면 알려주세요.
