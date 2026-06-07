# Known Issues

## analysis_level_market_status specialty Molecule option over-count

- Status: resolved locally on 2026-06-07
- Scope: local baseline for the analysis-level market-status clone card.
- Symptom: `analysis_level_market_status` specialty-channel Molecule options can sum above the channel total.
- Fix: include legacy single-label dual-channel rows in the specialty dimension denominator so the `total_value` source matches the option numerator source.
- Before/after signal: local blue-green preserved `cache_cause_old_specialty_20260607_173848` with 940 severe rows; current `cache_cause` has 0 severe rows.
- Representative case: `strategy_006` / `리바로젯` / `UBIST` / `sales` / `market_landscape` / `Molecule` / `의원 IGF` moved from `total_value` ratio `1.0993269993` to `1.0000000000`.
- Verification note: checks must use the payload's `total_value` and nested `brands_in_value[].value_series_10pt`; checking only root `value_series` can produce a false negative because these clone-card values are not stored on the same field.
- Rollback note: the previous local cache table is preserved as `cache_cause_old_specialty_20260607_173848`.
