# 260518 Correctness Rebuild 설계 기록

작성일: 2026-06-11

이 문서는 260518 MI Master 전환과 correctness fix를 audit zip 없이도
코드베이스 안에서 추적할 수 있게 남기는 checkpoint다. 아직 live swap은
PL 게이트 전이며, Stage 1 잔여 검증에서는 blast radius의 의도 외 key diff가
STOP 상태로 남아 있다.

## 목표

- MI Master 입력을 2026-05-18 재공유본으로 고정한다.
- 리바로/리바로젯 strategy_006의 Class/Molecule 기준을 260518 시트와 맞춘다.
- 제이클 strategy_002의 Molecule==Class 문제를 제거한다.
- IQVIA 제형/strength dimension이 mart부터 catalog recode 기준으로 집계되게 한다.
- ranking과 level trend payload가 선택 브랜드 + 경쟁 top5 + 기타 계약을 지키게 한다.
- ml_003 UBIST 브랜드 확장을 579개로 재현한다.

## 정식 시퀀스

정식 rebuild는 `pipeline/scripts/run_market_pipeline.sh --all`의 순서를 따른다.
손으로 prototype 일부만 골라 돌리면 canonical/raw 브랜드 확장 단계가 빠져
ml_003이 82개 정의 행 수준으로 축소될 수 있다.

큰 흐름:

1. MI Master 원본 로드와 Layer0 prototype 실행
2. postfix/catalog 확장
3. general mart 생성
4. strategic ML/CD mart 생성
5. cause/market-status cache 생성

ml_003 브랜드 단위 산출은 `fix_ml_003_catalog_brands.py`와
`rebuild_strategic_brand_catalog.py` 계열 postfix가 만든 `sb_*_raw_*` 및
`sb_canonical_*` overlay를 통해 general 브랜드로 확장된다. 이 확장 없이
MI Master 정의 행만 쓰는 대안은 live mart의 브랜드 universe를 재현하지 못해
기각했다.

## 적용 Fix

### 1. ATC 5글자 Alias

파일: `pipeline/scripts/etl/layer3_compute_strategic_ml_v3.py`

IQVIA 정의에는 `A10H0`처럼 5글자 ATC4가 있고, UBIST general/raw에는 `A10H`
형태가 있다. 이 둘을 연결하지 않으면 가드렛/가드메트 ml_003 UBIST에서
SU/AGI 제네릭 97개가 누락된다. `...0` 꼬리만 4글자로 alias하는 좁은 규칙을
사용한다.

### 2. 260518 MI Master Migration

파일:

- `pipeline/scripts/etl/storage.py`
- `pipeline/scripts/prototype_12_*` ~ `prototype_19_*`
- `catalog/market_metadata.yaml`

리바로/리바로젯 시트에서 4/22와 5/18의 Class/Molecule 의미가 달라졌기 때문에
원본 파일명을 260518로 고정한다. 시장별 임시 매핑으로 덮는 방식은 Excel에서
ETL로 재현되는 원칙을 깨므로 쓰지 않는다.

### 3. prototype_08 Q&A Guard

파일: `pipeline/scripts/prototype_08_master_qa_to_parquet.py`

260518 원본에는 Q&A 시트가 없다. Q&A sheet loader는 시트가 없으면 빈 산출로
skip하고, 실제 override는 별도 config/market override 경로에서 검증한다.

### 4. prototype_09/10/11 260518 형식 변화

파일:

- `pipeline/scripts/prototype_09_master_brand_consolidation_to_parquet.py`
- `pipeline/scripts/prototype_10_master_mapping_table_to_parquet.py`
- `pipeline/scripts/prototype_11_master_drug_to_parquet.py`

260518 시트에는 formatting tail과 row count 변화가 있다. raw scan exact count가
아니라 staging row, row identity, column-width consistency를 검증한다.
mapping table은 5932에서 5956으로 늘어난 24행을 정상 추가분으로 보고 strict
count를 새 기준에 맞춘다.

### 5. Worklist 006 Guard

파일: `pipeline/scripts/etl/apply_molecule_worklist.py`

과거 worklist가 ml_006/cd_006 molecule을 `Statin/Statin-EZE` class 라벨로
되돌리는 경로를 막는다. 260518 catalog가 이미 molecule truth를 갖고 있으므로
006만 UPDATE를 skip한다. worklist 전체 삭제는 다른 시장 보정 기능을 잃기
때문에 기각했다.

### 6. A1 제이클 Molecule

파일:

- `pipeline/scripts/prototype_20_strategic_brand_to_parquet.py`
- `pipeline/scripts/prototype_21_strategic_product_to_parquet.py`

제이클 strategy_002는 `Recode Class(성분)`이 class grouping이고,
Molecule은 raw `MOLECULE DESC`여야 한다. class recode를 molecule에 넣으면
Molecule==Class가 되어 분석 level이 무너진다. raw molecule은 metadata로도
보존한다.

### 7. A2 IQVIA 제형/Strength Recode

파일:

- `pipeline/scripts/etl/layer3_compute_strategic_ml_v3.py`
- `pipeline/scripts/etl/layer3_compute_strategic_cd_v3.py`

IQVIA 제형/strength는 cache에서만 label을 고치는 것이 아니라 mart
`dimension_data` 집계 시점부터 catalog recode label을 사용한다. 이렇게 해야
mart와 cache가 같은 single source를 본다. UBIST와 다른 dimension은 불변이다.

### 8. B1 선택 브랜드 무조건 포함

파일: `pipeline/scripts/etl/build_cache_cause.py`

ranking payload는 선택 브랜드가 top5 밖이거나 값이 0이어도
선택 + 경쟁 top5 + 기타 구조를 지켜야 한다. 선택 브랜드를 기타에 남기는 방식은
double counting을 만들기 때문에 금지한다.

### 9. B2 Level Top5 Trend 구조

파일: `pipeline/scripts/etl/build_cache_cause.py`

`level_top5_trend`에서 전체 옵션은 전체 시장 기준이므로 선택 브랜드를 포함한다.
개별 segment 옵션은 그 segment의 top5 + 기타만 보여주며, 선택 브랜드를 강제로
끼우지 않는다.

## 게이트 정의

- G1: ml_003 UBIST 579, IQVIA 568, 전 ML/CD 시장 brand count가 live와 일치
- G2: strategy_006 Class=Statin/Statin-EZE, Molecule=성분 코드, 교집합 0
- G3: IQVIA raw NFC/strength hit 0, ml_002/cd_002 Molecule==Class 0
- G4: ranking 선택 브랜드 누락 0
- G5: level trend 전체=선택 포함, segment=top5+기타, 합산 정합
- G6: 260518 원본 version, ml 16/cd 19, 제외 46/51 정합
- Stage 1 validation: live cache와 byte-for-byte 일치가 아니라 stage 자체의
  내부정합, 260518 사양 정확성, 파생값 sanity로 판단

### Stage 1 검증 기준 재정의

초기 blast radius 게이트는 staging cache와 live cache의 top-level key diff를
직접 비교했다. 그러나 현재 로컬 live cache는 260518 clean rebuild 결과가 아니라
핫픽스, 이전 스크립트, 옛 입력 데이터가 누적된 patchwork 상태다. 따라서
clean stage가 live와 광범위하게 다른 것은 그 자체로 실패가 아니다.

앞으로 Stage 2 진행 판단은 다음 순서로 한다.

1. Stage cache와 stage mart가 같은 입력 universe를 보는지 확인한다.
2. G1~G6, B1, B2가 260518 사양에 맞게 통과하는지 확인한다.
3. HHI, market size, KPI, growth contribution, EI/MS matrix, sources_data 같은
   파생값은 계산식 변경이 아니라 입력 universe 변화로만 달라졌는지 확인한다.
4. 파생값 sanity를 전수 확인한다. 음수 market size, NaN, 빈 KPI, HHI 범위 이탈,
   누락된 source metadata가 있으면 STOP한다.
5. 이전 live와의 diff는 배포 영향 설명 자료로만 사용하고, byte equality를
   correctness gate로 쓰지 않는다.

직접 의도된 변경 key는 `brand_ranking`, `brand_ranking_stacked`,
`company_ranking`, `company_ranking_stacked`, `level_top5_trend`,
`analysis_levels`, `analysis_level_market_status`, `by_dimension`이다. `kpi`,
`market_size_series`, `hhi_series_5y`, `hhi_recent`, `growth_contribution`,
`growth_contribution_ms_matrix`, `ei_ms_matrix`, `sources_data`,
`target_customer_competition`, `target_customer_competition_by_channel`은
멤버십, recode, 선택 브랜드 포함 규칙이 바뀌면 다시 계산되는 ripple key다.

Stage 1 재검증(2026-06-11)은 staging `cache_cause` 13,334행에서 파생값 sanity
이상 0, stage cache와 stage mart 대표 140샘플 mismatch 0을 확인했다. 또한
strategy_001과 strategy_016 대표 샘플은 stage/live period coverage와 recent
market size/HHI가 동일해, 미변경 시장의 대표 파생값에는 설명되지 않는 잔차가
보이지 않았다. 이 기준은 이후 clean rebuild 검증의 기본 gate로 유지한다.

## Audit 추적

- Stage 1 staging audit: `/tmp/deploy_stage1_staging_20260611_015247_audit.zip`
  - SHA256: `e89e794f5ab501db0945fbc6899d28c94b32e87a49c4ebc385380570300f5032`
- Stage 1 residual audit: `/tmp/deploy_stage1_residual_20260611_073116.zip`
  - SHA256: `53d5c16c54af90c943151dc799edd4e967044ac45b1d76c6f40cdb287d7c4f27`

## 현재 열린 항목

Stage 1 residual 검증에서 G3는 PASS했다. 이후 게이트를 재정의해, live와의
top-level key diff는 clean stage 실패 조건이 아니라 배포 영향 설명 자료로
분리했다. strategy_003 brand-key 불일치는 normalize 후 동일 브랜드로 판정되어
cosmetic/name-format drift로 분류됐다.
