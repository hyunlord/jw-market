# Tier2 LLM Prompt Changelog

## 2026-07-10 — GA workflow rebuild

- Rebuilt tagging as workflow `334`, revision `5668`, deployment `1450`.
- Rebuilt scoring as workflow `337`, revision `5671`, deployment `1453`.
- Preserved the prior prompts and nodes; only `servingRevision` changed from
  preview revision `475` to GA revisions `586` and `589`, respectively.
- Kept historical workflows `317` and `324` untouched.

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

## 2026-07-06 — Tier2 scoring workflow registration

- Added `jw_tier2_brand_scoring_prompt.md` as the Tier2 scoring prompt.
- Registered `jw-tier2-brand-scoring` through the DB-backed GenOS workflow path.
- Workflow id `324`, revision `5442`, deployment `1377`, serving `163`.
- Backing Flowise chat_flow id `034e7ed0-92d3-47a1-8ff4-87207028a45d`.
- Verified repository prompt SHA, workflow revision step prompt SHA, and backing row prompt SHA all match:
  `a3eddd0b031614e33202cd326c883d638111a4ee7e53094e5df03c88aa67d321`.
- Smoke-tested contract and scale only. The workflow parsed 19/19 mixed calls and 15/15 direct comparison calls. Direct wf196 comparison showed compatible broad scale with boundary drift around the 45-59 band, so full replay remains gated on accepted tolerance and runner input context.

## 2026-07-07 — Tier2 scoring category-output revision

- Updated `jw_tier2_brand_scoring_prompt.md` so `jw-tier2-brand-scoring` emits both
  wf196-compatible score and wf196-compatible news classification.
- Output contract now requires `tag`, `category_label`, and `category_code`; `tag`
  and `category_label` must match, and `기타` maps to `category_code=external`.
- Copied the wf196 six-label classification rubric into the Tier2 scoring prompt.
- Registered workflow id `324`, revision `5490`, deployment `1389`, serving `163`.
- Backing Flowise chat_flow id remains `034e7ed0-92d3-47a1-8ff4-87207028a45d`
  and type `AGENTFLOW`.
- Verified repository prompt SHA, workflow revision step prompt SHA, and backing row
  prompt SHA all match:
  `4bc6bb852ed0d6bc2ef280e1a89a7a3d6f3ee3639129c526ee52abfddc97a8e1`.
- Runtime smoke passed on 15/15 Tier2 match samples. Each response returned valid
  JSON with integer `score`, valid `tag`, matching `category_label`, and the
  expected `category_code` mapping. Full replay remains gated on the separate bulk
  runner and event_brand_scores replacement procedure.

## 2026-07-07 — Tier2 scoring multi-brand revision

- Updated `jw_tier2_brand_scoring_prompt.md` from the single `target_brand`
  contract to the news-level `target_brands[]` contract.
- Output contract now requires one news-level classification plus
  `brand_scores[]`, with exactly one score object for each input brand and no
  out-of-candidate brands.
- Registered workflow id `324`, revision `5496`, deployment `1392`, serving
  `163`.
- Backing Flowise chat_flow id remains `034e7ed0-92d3-47a1-8ff4-87207028a45d`
  and type `AGENTFLOW`.
- Verified repository prompt SHA, workflow revision step prompt SHA, and backing row
  prompt SHA all match:
  `15074bf21e1e919d296fcc1da65f7f1f6a98b18a2062bb55981ea2fcf50050a3`.
- Runtime smoke passed on 15/15 Tier2 match samples, including 12 multi-brand
  news samples. Input and output brand-key sets matched exactly, with valid
  wf196-compatible classification and 0 out-of-candidate brands. Full replay is
  now shaped as 12,781 news-level calls rather than 23,964 brand-level calls.
