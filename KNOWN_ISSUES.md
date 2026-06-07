# Known Issues

## analysis_level_market_status specialty Molecule option over-count

- Status: open
- Scope: local baseline for the analysis-level market-status clone card.
- Symptom: `analysis_level_market_status` specialty-channel Molecule options can sum above the channel total.
- Current measured signal: 940 severe rows remain in `cache_cause`.
- Representative case: `strategy_006` / `리바로젯` / `UBIST` / `sales` / `market_landscape` / `Molecule` / `의원 IGF` has `total_value` ratio about `1.0993`.
- Verification note: checks must use the payload's `total_value` and nested `brands_in_value[].value_series_10pt`; checking only root `value_series` can produce a false negative because these clone-card values are not stored on the same field.
- Next step: fix the specialty Molecule aggregation path before treating this payload as production-ready.
