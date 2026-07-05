# Tier2 LLM Prompt Changelog

## 2026-07-05 — tier2_llm_v1 initial contract

- Added the `jw-tier2-brand-tagging` prompt contract for article-level candidate confirmation.
- The workflow must use GenOS serving 163 and accept only deterministic exact-rule candidates as the candidate universe.
- Processor policy: LLM-confirmed `(brand, article)` rows are written as `tier2_llm_v1`; rule-only rows remain `tier2_exact_rule_v1` and stay outside Agent2 evidence.

## 2026-07-06 — GenOS workflow registration

- Registered `jw-tier2-brand-tagging` through the DB-backed GenOS workflow path.
- Workflow id `317`, revision `5366`, deployment `1325`, serving `163`.
- Backing Flowise chat_flow id `b7dbe513-3879-4ec8-8baa-05a2d161500c`.
- Verified repository prompt SHA, workflow revision step prompt SHA, and backing row prompt SHA all match:
  `aab7790a4d03d05cb6147c029a7783aea744b386e2694aa50b2e5fbeb3f0c43f`.
- Smoke-tested three multi-brand articles, including `프랄런트`/`레파타`; parser validation passed with no missing or out-of-candidate brands.
