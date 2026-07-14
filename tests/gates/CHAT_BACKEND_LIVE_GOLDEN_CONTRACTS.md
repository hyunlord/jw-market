# Chat Consumer Backend Live Golden Contracts

This tracked registry replaces the five ownerless hashes that were copied into
the all-four `/tmp` audit. A hash without its exact request and independent
truth basis is not a golden contract.

## Scope And Ownership

All five requests are jw-market backend API requests, not Agent2, Agent3, or
chat response contracts. `tests/api/API_GOLDEN_CONTRACTS.md` is the existing
jw-market-owned contract registry. This file is a transitional release-consumer
gate so the historical all-four check no longer owns untracked literals.

Whether this consumer gate should later be deleted in favor of directly
referencing the market-owned registry is a PL decision. This change does not
make that ownership decision and does not alter the market registry.

## Canonicalization

The response JSON is encoded as UTF-8 using:

```python
json.dumps(
    payload,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
).encode()
```

The gate hashes those bytes with SHA-256. It never hashes a retained response
snapshot as the current observation.

## Measurement Context

- Measured: `2026-07-14 18:41 KST`
- Database: `jw_mart_d2_stage_20260630_r2`
- Build SHA: `UNKNOWN`. The runtime image contains no Git/REVISION metadata;
  four runtime source hashes match multiple repository commits, so no commit is
  promoted from inference.
- Runtime image digest measured from the live deployment and pod:
  `sha256:937facd0d30c70e3852d1e57cb0d0a4ba1716a6779865d8a9a7b85f039e7d7b6`
- Request headers: no contract-specific headers. The collector adds only
  `Accept: application/json` and a diagnostic `User-Agent`.

## Enabled Current-Live Contracts

| ID | Exact request | Expected canonical SHA-256 | Independent truth basis |
|---|---|---|---|
| `brands` | `GET /jw-market-backend-api/api/brands` | `e7d41deb857031b7e03ee0880cadca95f1690def1512188aa3beedea92c1832b` | JW market sixth owner response confirmed the current 25-brand pre-F-080 contract. |
| `market_status` | `GET /jw-market-backend-api/api/market-status` | `ed7fd426f9c9094a2bbeb3ce37e57b7baafd886f8696e1679afc928d43c5ec2d` | JW market confirmed the corrected catalog population and rounding after 28 mart-source observations were independently reconciled. |
| `cause_livalo` | `GET /jw-market-backend-api/api/cause/리바로?view=market_landscape&source=UBIST&measure=sales&market_id=ml_006` | `69f98134668ab965e3bd4617ca344be820072e2acb07203a99e4e7ddd2dea327` | JW market sixth owner response confirmed this exact current-live request contract. |

`brands` is expected to become `472b4c5c...` after F-080 is deployed.
`cause_livalo` is expected to become `e7dcc96e...` after F-079 is deployed,
but that candidate currently omits `market_id=ml_006`; request identity must be
reviewed before promoting the successor hash.

## Observed But Not Golden

| ID | Exact request | Last observed SHA-256 | Gate status |
|---|---|---|---|
| `cause_aktemra` | `GET /jw-market-backend-api/api/cause/악템라?view=market_landscape&source=IQVIA&measure=sales&market_id=ml_011` | `50c68d942d878ac24afb41104c0a4f1a780dc0976e2e518e88e0e052559e8802` | Excluded: owner-approved truth basis unconfirmed. |
| `cause_guardlet` | `GET /jw-market-backend-api/api/cause/가드렛?view=market_landscape&source=IQVIA&measure=sales&market_id=ml_003` | `1b590a691279c4253e966d6a6fbfb63f5b30cbec3c5e5fcf90158c24ed970182` | Excluded: owner-approved truth basis unconfirmed. |

These values remain observations. They are deliberately stored as
`last_observed_sha256`, while `canonical_sha256` is null and
`gate_enabled=false`.

## Running The Gate

```bash
python3 pipeline/scripts/gates/release_acceptance.py live-goldens \
  --repo-root . \
  --contracts tests/gates/chat_backend_live_goldens.json \
  --base-url https://jwai-dev.jwhealthcare.com \
  --environment production
```

The command rejects contract files outside the repository or absent from the
Git index. Each enabled endpoint is called at run time; HTTP status, response
byte count, actual hash, and expected hash are emitted before the acceptance
record.
