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

## Required deployment environment

`env_presence_gate.py` is gate 3, after exact image/generation identity and
required pod-template annotations. It checks the complete tracked key census
without reading or printing values:

```bash
kubectl -n llmops get deployment jw-chat-agent-poc -o json \
  | python3 pipeline/scripts/gates/env_presence_gate.py \
      --required-file pipeline/scripts/gates/required_env/jw-chat-agent-poc.json
```

Use the corresponding `code-serving-235.json` set for the bridge. A missing
key, malformed Deployment JSON, or empty required set fails closed. The 235
required set intentionally excludes `FILE_SQL_ENABLED` until its policy is
decided separately.

## Environment manifest ownership

The tracked chat and 235 Deployment files are server-side-apply ownership
manifests for stable environment settings. They are not standalone Deployment
creation manifests. Image and release identity belong to the release pipeline;
replicas belong to the HPA or workload controller. Before any apply, fail closed
if either field has drifted back into a tracked manifest:

```bash
python3 pipeline/scripts/gates/manifest_field_ownership_gate.py \
  < chat/jw-chat-agent-poc/deploy/deployment.yaml

python3 pipeline/scripts/gates/manifest_field_ownership_gate.py \
  < chat/wf301-vdb-bridge/deploy/deployment.yaml
```

Use the dedicated `jw-chat-env-canonicalizer` server-side field manager against
an existing Deployment. First run `--dry-run=server` and verify that the merged
result preserves the live image and replica count. Do not use ordinary
client-side apply: historical `last-applied-configuration` ownership can delete
fields omitted from the new env-only manifest.

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
population from `mart_general_market_metric`, fixes one baseline for each selected
range, and compares every point with a live `/api/dynamic-market` response. The
independent calculation uses the latest numeric endpoint's exact five-year prior
period when present, otherwise the earliest numeric period. UBIST annualizes over
the actual elapsed months (`12/n`); IQVIA annualizes over elapsed quarters (`4/n`).
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

## Latency regression matrix

The `latency-matrix` command builds its population from the reference runtime's
default membership plus the required expanded edge brands. Search identities
are whitespace-normalized before context resolution. A default member with no
search context remains recorded as default-only, while context-dependent cases
are emitted only for search-resolved brands. The matrix discovers every
published general, strategic market-landscape, and strategic competitive-
dynamics context and every listed source. Reference deep-analysis combinations
that explicitly return `source_not_available` are recorded and excluded before
the candidate is called; every other non-200 remains a failure.

The matrix compares candidate and reference responses for dynamic-market,
cause, deep-analysis, filter-options, brand search, and all public Brand
Activity surfaces. Filter-options uses the public `general|strategic` view
contract, deduplicated per brand and source. Deep-analysis masks only the
top-level `generated_at` timestamp; every other response is compared as exact
bytes. The census runs serially (`max_workers=1`) so the gate itself cannot
manufacture single-flight or busy-guard 429 responses.

The competitive-dynamics population must include `악템라` and `가드렛`.
Brand Activity additionally probes the general-view groups
`group:livalo_family` and `group:gardlet_family` through topics, CSD timeseries,
CSD activity, and interest/Rx. A missing required brand/context, any non-200,
or any response difference fails closed. The two required group topic probes
must also contain at least one brand with measured topic activity; matching
empty responses do not pass.

The matrix also carries fixed general-view contract scenarios for ATC4 OR
scopes of 1, 2, 5, and 10 values, IQVIA `molecule_type` and `molecule_desc`
narrowing, and both sales and source-native quantity measures. The quantity
measure is `volume` for UBIST and `unit` for IQVIA; these are the runtime's
canonical measure names.

```bash
python3 pipeline/scripts/gates/release_acceptance.py latency-matrix \
  --candidate-url http://candidate-runtime:8000 \
  --reference-url http://production-runtime:8000 \
  --evidence-output /tmp/latency-matrix.json \
  --environment test2-vs-production
```

This is a census of the declared serving population discovered from the
reference API. It is not a claim about every distinct raw mart brand row.

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

Every entity/year pair must have contiguous displayed ranks, and the displayed
rows must equal the same-length prefix of `rankings_by_year`. Brand and company
rows, including `기타`, must independently reconcile to the same annual market
total. The expected years are explicit, so a year missing from both entity
payloads cannot disappear from the census. Missing years, block divergence,
null shares, and empty censuses fail closed.

## F-116 specialty, topic storage, and canonical precedence

The F-116 gate consumes one tracked or audit-packaged census document. It checks
that strategic specialty totals no longer include aggregate parents and match
both the corresponding dimension total and the independently read market-size
headline. It also checks that topic storage contains the complete measured brand
population while the API remains bounded to six brands, canonical dimensions use
field-level non-null fallback, and measured latency stays within 1.2x of baseline.

```bash
python3 pipeline/scripts/gates/release_acceptance.py f116-correctness \
  --evidence /tmp/f116_correctness.json \
  --environment test2
```

The evidence must come from independent mart SQL and live API observations.
Reusing one generated payload as both actual and expected is not acceptance
evidence.
