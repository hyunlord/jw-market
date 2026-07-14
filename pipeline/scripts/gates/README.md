# Release Acceptance Gates

`release_acceptance.py` is the tracked, fail-closed replacement for ad hoc
release checks. Every command emits the machine-readable acceptance fields and
returns a non-zero status for missing input, an empty census, mismatched API
goldens, strict log matches, or incomplete segment levels.

The golden, segment-sum, market-growth, and brand-source commands call the
supplied runtime directly. Segment and growth expectations come from mart SQL
inside an explicitly read-only transaction. Optional `--evidence-output`
files are audit output only; none of these commands accepts caller-produced
observations as gate input.

## Safe push

`safe_push.py` fetches the target remote, requires `origin/develop` and every
commit in `approved_shas.txt` to be ancestors of `HEAD`, rejects force
refspecs, performs the push itself without force flags, and verifies the
remote SHA.

```bash
python3 pipeline/scripts/gates/safe_push.py \
  --refspec HEAD:refs/heads/develop \
  --environment local
```

Update `approved_shas.txt` only when a commit has been explicitly approved as
required release ancestry.

## API goldens

Run the four tracked requests directly against the candidate runtime:

```bash
python3 pipeline/scripts/gates/release_acceptance.py goldens \
  --base-url http://candidate-runtime:8000 \
  --environment test2
```

Expected hashes come only from `tests/api/api_golden_contracts.json`. The
command does not accept observation or alternate-contract files. Connection
errors, HTTP errors, malformed JSON, and empty responses all fail the gate.

## Strict logs

The caller must enumerate every pod belonging to the deployed digest and
capture one log file per pod. Replay/diagnostic logs are separate inputs and
must not be mixed into this strict error scan.

```bash
python3 pipeline/scripts/gates/release_acceptance.py strict-logs \
  --expected-pod pod-a --expected-pod pod-b \
  --pod-log pod-a=/path/pod-a.log --pod-log pod-b=/path/pod-b.log \
  --environment test2
```

## Independent population census

The candidates JSON is an array. The census JSON must come from an independent
query path, such as direct mart SQL, and has this shape:

```json
{"population":24789,"source":"direct mart SQL: ..."}
```

An empty candidate set always fails, including when the expected count is
zero.

## Segment sums

The segment-sum command reads Class, Molecule, and Ox/Gx from the live
`/api/cause/리바로` response and independently reads the ml_006 UBIST sales
total from `mart_strategic_ml_market_metric`. All three levels must reconcile
with `abs_tol=0.01, rel_tol=0`; one correct level cannot hide a missing or
incorrect level, and non-finite values fail before tolerance is evaluated.

```bash
DB_HOST=... DB_PORT=3306 DB_USER=... DB_PASSWORD=... DB_NAME=... \
python3 pipeline/scripts/gates/release_acceptance.py segment-sum \
  --base-url http://candidate-runtime:8000 \
  --evidence-output /tmp/segment-sum.json \
  --environment runtime
```

The initial tracked runner supports representative samples. A full mart census
should be scheduled as a separate nightly job because clean-clone CI has no d2
credentials and the full cell scan is intentionally not disguised as a local
unit test.

## Market growth census

The market-growth command enumerates the complete UBIST and IQVIA sales
population from `mart_general_market_metric`, derives the expected fixed-period
growth from each mart series (`n=60` monthly or `n=20` quarterly), and compares
it with a live `/api/dynamic-market` response. The independent calculation uses
the latest numeric endpoint and the exact five-year baseline when present,
otherwise the earliest numeric prior period, without shortening the exponent.
An empty or incomplete 902-cell population, unavailable independent expected
value, endpoint mismatch, request error, non-finite value, `-100` sentinel,
extreme value, or formula mismatch fails the command.

```bash
DB_HOST=... DB_PORT=3306 DB_USER=... DB_PASSWORD=... DB_NAME=... \
python3 pipeline/scripts/gates/release_acceptance.py market-growth \
  --base-url http://candidate-runtime:8000 \
  --evidence-output /tmp/market-growth.json \
  --environment runtime
```

## Growth contribution windows

The `growth-windows` command checks every declared 1y through 5y window for
period identity, an independently supplied market start, contribution sums,
brand/company agreement, and distinct non-truncated payloads. Short histories
are accepted only when the response exposes `reason=earliest_available` and a
matching `period_start_actual`.

```bash
python3 pipeline/scripts/gates/release_acceptance.py growth-windows \
  --evidence /path/to/growth_window_evidence.json \
  --abs-tol 0.01 \
  --environment runtime
```

## Brand source census

The brand-source gate reads the 25 live default brands and probes all 100
brand/view/source combinations. The listed side comes from `/api/brands`; the
data-presence side uses each exact search context and the formal
`/api/deep-analysis/{brand}` contract (`view_kind`, `market_id`, and `source`).
Every combination must be present exactly once and `listed` must equal the
formal response's boolean `market_meta.has_market_data`.

```bash
python3 pipeline/scripts/gates/release_acceptance.py brand-sources \
  --base-url http://candidate-runtime:8000 \
  --evidence-output /tmp/brand-sources.json \
  --environment production-read-only
```

The command fails on an empty or incomplete population, duplicate identities,
probe errors, and either direction of `listed != has_data`.

## Cause assembly equivalence

The cause-assembly gate compares before/after payload files byte-for-byte for
the declared cold/warm and canonical/expanded brand census. It also requires
every measured after time to improve and stay below the evidence document's
runtime threshold. If an optimization expands a cache, the evidence must set
`invalidation_verified=true`; request-local memoization declares
`cache_expanded=false`.

```bash
python3 pipeline/scripts/gates/release_acceptance.py cause-assembly \
  --evidence /path/to/cause_assembly_evidence.json \
  --environment local-runtime
```

## Competition ranking census

Capture the complete `brand_ranking_stacked` and `company_ranking_stacked`
objects under `brand` and `company`, then run:

```bash
python3 pipeline/scripts/gates/release_acceptance.py competition-ranking \
  --observations /path/to/rankings.json \
  --expected-year 2021 --expected-year 2022 --expected-year 2023 \
  --expected-year 2024 --expected-year 2025 --expected-year 2026 \
  --environment test2
```

Every entity/year pair must have contiguous displayed ranks. Brand and company
rows, including `기타`, must independently reconcile to the same annual market
total. The expected years are explicit, so a year missing from both entity
payloads cannot disappear from the census. Missing years, null shares, and
empty censuses fail closed.

## F-116 specialty, topic storage, and canonical precedence

The F-116 gate consumes one tracked or audit-packaged census document. It checks
that strategic specialty totals no longer include aggregate parents, topic
storage contains the complete measured brand population while the API remains
bounded to six brands, canonical dimensions use field-level non-null fallback,
and measured latency stays within 1.2x of baseline.

```bash
python3 pipeline/scripts/gates/release_acceptance.py f116-correctness \
  --evidence /tmp/f116_correctness.json \
  --environment test2
```

The evidence must come from independent mart SQL and live API observations.
Reusing one generated payload as both actual and expected is not acceptance
evidence.
