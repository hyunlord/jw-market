# Changelog

## 2026-06-11

### 260518 correctness rebuild checkpoint

- MI Master 기준 파일을 2026-05-18 재공유본으로 맞추는 rebuild 작업을 문서화했다.
- 리바로/리바로젯 Class/Molecule, 제이클 Molecule, IQVIA 제형/strength recode,
  선택 브랜드 포함 ranking, level_top5_trend 구조를 하나의 correctness rebuild
  범위로 묶었다.
- Stage 1 residual audit 기준 G3 mart recode는 PASS했지만, blast radius에서
  의도 외 top-level key diff가 남아 live swap은 아직 보류 상태다.
- 이 항목은 문서화 checkpoint이며, 배포/commit/push를 의미하지 않는다.

### Stage 2 cache 배포 절차 문서화

- `docs/rebuild/DEPLOY_RUNBOOK.md`에 방식 (2) 배포 절차를 추가했다.
- `pipeline/scripts/deploy_cache_stage2_way2.sh`를 추가해 cache 3종
  (`cache_cause`, `cache_market_status`, `cache_brands`)만 logical dump/load와
  blue-green swap 대상으로 삼도록 했다.
- `cache_deep_analysis_ai_analysis`와 로컬 `cache_deep_analysis`는 Agent2 소유
  산출물로 명시해 전송·swap 대상에서 제외했다.
- GCP 운영 swap은 PL 승인 전 STOP하는 절차로 기록했다.
