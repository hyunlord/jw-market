# Agent2 Tier Redesign

## Scope

The weekly Agent2/Agent3 workflow receives one immutable Agent2 worklist snapshot and executes these child jobs in order:

1. `agent2-plan`: resolve identities, event density, cohort, sales ordering, aliases, and exclusions once; write the snapshot and its SHA256.
2. `agent2-short`: consume the snapshot with the short analysis variant.
3. `agent2-long`: consume the same snapshot with the long analysis variant, regardless of short quality failures.
4. `agent2-finalize`: combine both durable manifests into one weekly verdict and cohort metrics manifest.
5. `agent3`: reuse the existing successful Agent3 artifact; it must not recompute Agent3 output.

Infrastructure failures stop the workflow. Brand-level validation or formatting failures remain visible in durable manifests but do not stop a later tier, variant, or Agent3 reuse step.

## Data Flow

`mart_general_brand_metric` and `event_brand_scores` feed `agent2-plan`. Active `catalog_strategic_brand` rows classify the resolved identities:

- Tier 0: the 25 JW brands.
- Tier 1: active strategic catalog brands that are not Tier 0.
- Tier 2: all other resolved brands.

Each tier is ordered by latest sales descending, then `brand_key` ascending. The immutable snapshot records the ordering inputs, route decision, cohort, exclusions, and source-event counts. Short and long jobs reject a missing or hash-mismatched snapshot.

The weekly-only canonical-key opt-in maps `종근당 자누비아` to `종근당자누비아` (`ml_003` / `cd_003`). Other unresolved non-JW, non-target event brands are recorded as `excluded_non_jw_market`; default ingestion remains fail-closed.

## Verdict Contract

- Any JW or strategic brand failure: `critical_failed`.
- Only nonstrategic brand failures: `completed_with_failures`.
- No brand failures: `complete`.

The global threshold value remains 5 and is reported. It no longer terminates the weekly global run. Each failure record contains brand, cohort, failure type, and reason.

For every cohort the final manifest records:

- `cohort_coverage`: reached / eligible.
- `template_zero_over_reached`.
- `validated_over_reached`.
- `validated_over_eligible`.

## Files

- `pipeline/scripts/ai_analysis/agent2_density_worklist.py`: cohort classification, alias/exclusion accounting, deterministic ordering, snapshot model.
- `pipeline/scripts/ai_analysis/agent2_regen_orchestrator.py`: snapshot input/output and weekly continue-on-quality-failure behavior.
- `pipeline/scripts/agent_refresh_weekly/contract.py`: child Job commands and shared snapshot paths.
- `pipeline/scripts/agent_refresh_weekly/activities.py`: independent Job execution and infrastructure-only stop behavior.
- `pipeline/scripts/agent_refresh_weekly/temporal_worker.py`: stage sequencing and final verdict aggregation.
- Focused tests under the corresponding `tests` directories.

No catalog description, selector, ingest pipeline, portal, BFF, threshold value, or schedule pause state changes are in scope.
