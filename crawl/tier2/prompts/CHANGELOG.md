# Tier2 LLM Prompt Changelog

## 2026-07-05 — tier2_llm_v1 initial contract

- Added the `jw-tier2-brand-tagging` prompt contract for article-level candidate confirmation.
- The workflow must use GenOS serving 163 and accept only deterministic exact-rule candidates as the candidate universe.
- Processor policy: LLM-confirmed `(brand, article)` rows are written as `tier2_llm_v1`; rule-only rows remain `tier2_exact_rule_v1` and stay outside Agent2 evidence.
