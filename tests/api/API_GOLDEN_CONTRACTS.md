# API Golden Contracts

This registry records approved canonical JSON hashes together with the facts
that make each payload a valid reference. A hash without its request and truth
basis is not a golden contract.

## Canonicalization

All hashes in this file use UTF-8 bytes from:

```python
json.dumps(
    payload,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
).encode()
```

The SHA-256 digest is calculated from those bytes. Raw wire bytes must not be
used because whitespace and object-key order are not part of the API contract.

## Latest Measurement Context

- Approval date: `2026-07-22`
- Database: `jw_mart_d2_stage_20260630_r2`
- test2 runtime: generation `725`, commit
  `339cc3fdf06500d369483c69a43f4afb2c711094`, image digest
  `sha256:e75cca401ee8fb8f49f835c587ea7ee78b000a9476cf9ef8c116f967d553bcb7`
- Runtime Python: `3.11.15`
- Candidate commits: F-079 `a9cc0ca2dec974d7af29e106b43e11c2c304f962`,
  F-080 `09b26f213dc9eeef88c8d1b63fc69370dd5fe46a`, and F-092 v2
  `91187f8f420c9179e213857b49e1ddd4e5f60d27`

The market-status, cause-Livaro, and dynamic-general hashes below were measured
from that exact test2 runtime. The brands hash was re-read and remained byte-
canonically unchanged.

## `/api/brands`

- Canonical SHA-256:
  `77917362f9ca356bc6a596abcb59d5b7b8e418c45ca7599d5c618852866fa6ab`
- Request: `GET /jw-market-backend-api/api/brands`
- Headers: no custom request headers
- Truth basis:
  - The response contains exactly the 25 brands in
    `cache_brands.response_json` for `query_key='default'`.
  - Each member's `atc_codes` equals
    `catalog_ml_market.atc_codes_json`; 25 of 25 matched.
  - `general_sources` and `strategic_sources` match the independently probed
    data-serving contexts in both directions for all 100 brand/view/source
    cells.
  - Seventeen of 25 brands have source metadata changes from the prior
    single-list payload; the two view-specific source keys are intentional.

`cache_brands.default` is the serving membership authority. `is_jw` and
`is_target` happen to describe the same 25 names in the recorded database
generation, but they must not replace the default-cache membership contract.

## `/api/market-status`

- Canonical SHA-256:
  `dc98c7ce1f7ee9e27b2cea7e89c69975811860fbe524e10dc896e06b3ff47ccf`
- Request: `GET /jw-market-backend-api/api/market-status`
- Headers: no custom request headers
- Truth basis: the existing 25 cards and legacy `back` values are unchanged.
  `back_extended` additively carries mutually-exclusive brand 5Y/3Y keys from
  the mart row selected by brand, ML market, and declared/default source.
  Livaro is 5Y `3.9056`; Livarozet is 3Y `23.3769`; Actemra uses its exact
  19-quarter IQVIA endpoint with exponent `4.75` and is 5Y `2.1052`.

## `/api/cause/리바로`

- Canonical SHA-256:
  `4e528de0c19ef2728e99b2f8285d7c8d673d23160bdae32a2760ee5b77725d08`
- Request:
  `GET /jw-market-backend-api/api/cause/%EB%A6%AC%EB%B0%94%EB%A1%9C?view=market_landscape&source=UBIST&measure=sales`
- Headers: no custom request headers
- Truth basis:
  - Class, Molecule, and Ox/Gx segment values each reconcile to their market
    total using the same complete denominator for values and shares.
  - The previously omitted six-brand value, `898614483.36`, is exposed as
    `미분류` for both Class and Molecule.
  - The total market value, competitor count, and Ox/Gx payload remain
    unchanged.
  - `data.kpi.brand_cagr_5y_pct=3.9056` and
    `brand_cagr_3y_pct=null` are additive selected-brand keys.
  - Existing market CAGR remains `market_cagr_5y_pct=9.37` and
    `market_cagr_3y_pct=null`; the two contracts are not substituted.
  - F-123 preserves strategic ranking continuity across years. This expands
    the first yearly ranking from 7 to 11 entries and exposes the additional
    `로바젯`, `리피로우`, `아토르바`, and `크레스토` series. PL re-approved
    the resulting canonical payload on `2026-07-15`.

The Class and Molecule share sums can differ from exactly 100 by the sum of
individually rounded four-decimal shares. They must not be renormalized to hide
that rounding residue.

## Dynamic General View

- Canonical SHA-256:
  `e8d218449b35a24523afcd0a42c3f05840e57fc22c15b26c7647828e57b2ba7c`
- Request: `POST /jw-market-backend-api/api/dynamic-market`
- Header: `Content-Type: application/json`
- Body:

```json
{"view":"general","source":"ubist","measure":"sales","filters":{"atc4":["C10A1"],"focus_brand_key":"리바로"}}
```

- Truth basis: the independently defined ATC4 scope and ranking series are
  unchanged. `result.data.kpi` additively exposes selected-brand 5Y/3Y keys;
  Livaro is 5Y `3.9056` and 3Y null. General, strategic ML, and strategic CD
  responses returned the same selected-brand CAGR values.

## Verification Ownership

The repository tests enforce the calculation invariants that justify the two
changed goldens:

- F-079 tests require complete Class and Molecule partitions, preserve Ox/Gx,
  and disclose missing dimension labels as `미분류`.
- F-080 tests require all 25 default brand rows to receive their ATC lists from
  the market catalog and reject missing catalog ATCs.

This file is the tracked hash registry. The release acceptance gate reads the
expected hashes from `tests/api/api_golden_contracts.json`, calls every request
against the supplied runtime URL, and hashes each JSON response immediately.
It does not accept a captured-response file or an alternate contract path.
Clean-clone tests have no candidate API or d2 credentials and therefore must
not report an unmeasured live PASS.
