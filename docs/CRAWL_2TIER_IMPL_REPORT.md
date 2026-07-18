# 2-tier Crawl Pipeline Implementation Report

Date: 2026-06-30
Scope: code/workflow implementation, small dry-run only. Large crawl was not executed.

## Executive Summary

- Tier1 remains the existing JW 25 workflow: expanded queries and workflow-196 LLM scoring.
- Tier2 is added as a zero-LLM path: weekly brand slices, exact brand-name search, deterministic rule scoring, and common false-positive safeguards.
- Storage metadata is added through migration SQL: `tier` and `collected_at` on `news_raw`, `events`, and `event_brand_scores`.
- Retention is separated by tier: Tier1 keeps five years by policy, Tier2 has a 365-day rolling cleanup helper.
- GKE CronJob manifests were added with low resources and `suspend: true`; the first large run still requires PL approval.

## STAGE2. Tier2 exact match, scoring, and false-positive policy

Implemented files:

- `pipeline/scripts/crawler/tier2_catalog.py`
- `pipeline/scripts/crawler/tier2_match_score.py`
- `pipeline/scripts/crawler/crawl_2tier.py`

Tier2 brand universe:

- Source: `mart_general_brand_metric` where `measure='sales'`.
- Sales criterion: recent 12 periods total >= KRW 3,000,000,000.
- New-brand criterion: first non-zero sales in the latest 6 periods and latest 6-period sales >= KRW 100,000,000.
- JW dedupe: optional catalog input removes JW 25 names from Tier2.
- Week slicing: stable SHA256 hash of `brand_key` modulo 7.

Read-only test2 observation:

| Metric | Value |
|---|---:|
| mart rows read for sales sources | 35,471 |
| period range | 2020-Q3 to 2026-04 |
| selected without JW dedupe, simple 12-period rule | 2,435 before materiality tuning |
| weekday slice spread before JW dedupe | 313-376/day |

Tier2 matching:

- Search keyword is brand name only.
- A crawled article maps to a brand only when the brand appears as an exact phrase in title/body or matches the search keyword.
- Brand-specific exceptions: 0.
- Common false-positive rule: short or generic names require pharma context terms such as `제약`, `처방`, `임상`, `약`, `의약`, `식약처`, `병원`, `환자`, `치료`, `급여`.
- Medical Times (`메디칼타임즈`) is excluded from Tier2 site list; Tier2 uses the remaining 11 sites.

Tier2 scoring:

- LLM calls: 0.
- Score is deterministic:
  - title exact match bonus,
  - body exact mention count bonus,
  - searched-brand bonus,
  - pharma-context bonus.
- Score tiers: `tier2_primary`, `tier2_relevant`, `tier2_contextual`, `tier2_mention`, `excluded`.

Small dry-run:

| Case | Result |
|---|---|
| `가드렛` title/body exact + pharma context | mapped, score 95, `tier2_primary` |
| ambiguous `큐` without pharma context | excluded |

## STAGE1. Tier and retention schema

Migration SQL:

- `pipeline/scripts/crawler/sql/001_news_tier_retention.sql`

Added metadata columns:

- `news_raw.tier`, `news_raw.collected_at`
- `events.tier`, `events.collected_at`
- `event_brand_scores.tier`, `event_brand_scores.collected_at`

Existing rows default to Tier1 via `DEFAULT 1`. The loader now accepts:

```bash
--tier 1|2
--collected-at "YYYY-MM-DD HH:MM:SS"
```

Retention helper:

- `pipeline/scripts/crawler/crawl_retention.py`
- Default is dry-run.
- `--apply` deletes expired Tier2 rows older than 365 days in dependency order: `event_brand_scores`, orphaned `events`, orphaned `news_raw`.

No retention delete was executed.

## STAGE3. Tier1 regression boundary

Tier1 path is preserved:

- existing `crawl_news_v2.py` and `crawl_news_full_orchestrator.py`,
- existing workflow-196 scoring through `score_v2.py`,
- existing loader path with added `tier=1` metadata.

No workflow-196 prompt, GenOS workflow, or Flowise graph changes were made.

## STAGE4. GKE CronJob workflow

Added manifests:

- `deploy/k8s/crawler/crawl-tier1-cronjob.yaml`
- `deploy/k8s/crawler/crawl-tier2-cronjob.yaml`

Resource policy:

| Job | Schedule | Resources | Concurrency |
|---|---|---|---|
| Tier1 JW 25 | daily | request 500m/1Gi, limit 1 CPU/2Gi | Forbid |
| Tier2 daily slice | daily, one stable 1/7 slice | request 500m/1Gi, limit 1 CPU/2Gi | Forbid |

Both manifests are committed with `suspend: true` because a full crawl is a large external run and requires PL go. The image tag is intentionally marked `crawl-2tier-REPLACE_WITH_TAG` until the crawl image is built.

## STAGE5. Verification

Commands run:

```bash
python3 -m py_compile \
  pipeline/scripts/agent_2/corpus_loader.py \
  pipeline/scripts/crawler/crawl_2tier.py \
  pipeline/scripts/crawler/crawl_retention.py \
  pipeline/scripts/crawler/tier2_catalog.py \
  pipeline/scripts/crawler/tier2_match_score.py

python3 -m pytest \
  tests/crawler/test_tier2_match_score.py \
  tests/crawler/test_tier2_catalog.py -q
```

Result:

- 4 tests passed.
- CronJob YAML parsed successfully with `kind=CronJob` and `suspend=True`.
- Small Tier2 dry-run scoring produced expected exact-match mapping and ambiguous-name exclusion.

Large crawl:

- Not executed.
- CronJobs not applied/unsuspended.

## STAGE6. Cost

Tier1 LLM cost estimate:

- Prior Agent1 proxy: 351 scored articles cost about USD 0.878, about KRW 1,354.
- Unit proxy: about USD 0.0025 per article.
- If Tier1 produces about 252 articles/day, estimate is about USD 0.63/day, about KRW 970/day.
- This is an estimate because workflow-196 run-level token logging remains incomplete.

Tier2 cost:

- Full LLM classification: skipped.
- Tier2 rule scoring LLM cost: USD 0 / KRW 0.
- Marginal compute/network cost is GKE/runtime dependent; the implemented path uses low resource requests and no model calls.

## Remaining Gates

1. Apply migration SQL after backup/review.
2. Build an image containing the new crawl files and replace `crawl-2tier-REPLACE_WITH_TAG`.
3. PL approval before unsuspending CronJobs or running the large crawl.
4. Confirm final Tier2 count after JW catalog dedupe in the target runtime DB.

## Canonical Branch Policy

The crawl and short/long producers were recovered by extracting their reviewed
dependency closure onto current `develop`. The historical branches
`codex/crawl-2tier` and `codex/short-long-lineage-bulk` are retained for
provenance but must never be merged into `develop`: they contain workflow
revision 5365 and stale Agent3 and API contracts.

The reviewed extraction lineage is canonical. Imports from API, Agent3, and
forecast modules must continue to resolve to the current `develop` versions.
The crawl image still requires a separately gated rebuild from the approved
extraction commit; neither historical branch is a valid image build base.
