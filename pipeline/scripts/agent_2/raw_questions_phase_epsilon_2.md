# Phase epsilon.2 Raw Questions

## 1. Backup table retention

Keep `news_raw_v1_backup`, `events_v1_backup`, and
`event_brand_scores_v1_backup` at least until production cutover is complete.
This phase does not drop backup tables.

## 2. watch_and_load start timing

`watch_and_load.sh` is implemented but not started by this phase. Starting the
watcher remains a PL operation because it creates a long-running background
process against a live local mart.

## 3. brand_canonical / brand_id / ml_id / cd_id backfill

`_catalog.json` only provides JW25 names and descriptions. The loader enriches
from local parquet catalogs when present, otherwise `brand_id`, `ml_id`, and
`cd_id` remain `NULL`.

## 4. score_tier mapping

This phase uses the requested five-bucket mapping:
`<30 very_weak`, `30-49 weak`, `50-69 moderate`, `70-84 strong`,
`85-100 very_strong`.

## 5. dashboard_cache refresh

No dashboard cache refresh is performed here. If dashboard reads are cached
outside `events` / `event_brand_scores`, refresh should be handled in a follow-up
phase after corpus replacement is accepted.
