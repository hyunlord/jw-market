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

## Measurement Context

- Approval date: `2026-07-14`
- Database: `jw_mart_d2_stage_20260630_r2`
- Reference runtime image digest for unchanged live payloads:
  `sha256:937facd0d30c70e3852d1e57cb0d0a4ba1716a6779865d8a9a7b85f039e7d7b6`
- Runtime Python: `3.11.15`
- Candidate commits: F-079 `a9cc0ca2dec974d7af29e106b43e11c2c304f962`,
  F-080 `09b26f213dc9eeef88c8d1b63fc69370dd5fe46a`, and F-092 v2
  `91187f8f420c9179e213857b49e1ddd4e5f60d27`

The changed candidate hashes were generated without deploying those commits.
The candidate code ran against the database above, while unchanged payloads
were re-read from the reference runtime. Do not describe all four as having
been measured from one deployed image.

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
  `ed7fd426f9c9094a2bbeb3ce37e57b7baafd886f8696e1679afc928d43c5ec2d`
- Request: `GET /jw-market-backend-api/api/market-status`
- Headers: no custom request headers
- Truth basis: 28 source observations were independently recalculated from
  mart source values and matched the response. Neither F-079 nor F-080 changes
  the market-status calculation path.

## `/api/cause/리바로`

- Canonical SHA-256:
  `e7dcc96e4e5390ca55ae78aff28c04a28956e68cb098d25ac171e7878c81ec3b`
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

The Class and Molecule share sums can differ from exactly 100 by the sum of
individually rounded four-decimal shares. They must not be renormalized to hide
that rounding residue.

## Dynamic General View

- Canonical SHA-256:
  `78b8bc3ba22db9bd4216e4b9879be8e5b3feb8e213d2e70b1cb4c416da5d28ca`
- Request: `POST /jw-market-backend-api/api/dynamic-market`
- Header: `Content-Type: application/json`
- Body:

```json
{"view":"general","source":"ubist","measure":"sales","filters":{"atc4":["C10A1"],"focus_brand_key":"리바로"}}
```

- Truth basis: this payload was re-read unchanged from the reference runtime;
  F-079 affects strategic cause partitions and F-080 affects brands metadata,
  so neither approved diff changes this request path.

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
