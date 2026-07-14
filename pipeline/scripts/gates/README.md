# Release Acceptance Gates

`release_acceptance.py` is the tracked, fail-closed replacement for ad hoc
release checks. Every command emits the machine-readable acceptance fields and
returns a non-zero status for missing input, an empty census, mismatched API
goldens, strict log matches, or incomplete segment levels.

The golden command calls the supplied runtime directly. Other commands consume
evidence captured by deployment automation and do not open cluster or database
connections themselves.

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

The segment-sum command compares observed `segment_sum` and `market_total`
values using `abs_tol=0.01, rel_tol=0`. Its expected identity file declares
whether the check is a `sample` or full `census`. Every expected combination of
market, period, source, measure, and level must be present; a correct value at
one level cannot hide a missing level.

The initial tracked runner supports representative samples. A full mart census
should be scheduled as a separate nightly job because clean-clone CI has no d2
credentials and the full cell scan is intentionally not disguised as a local
unit test.

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

The brand-source gate compares the complete Cartesian population declared by
the expectations file with independently captured API observations. Every
brand, view, and source combination must be present exactly once, and each
observation must report whether the source was listed and whether the API
actually returned market data.

```bash
python3 pipeline/scripts/gates/release_acceptance.py brand-sources \
  --expectations /path/to/source_census_expectations.json \
  --observations /path/to/source_census_observations.json \
  --environment production-read-only
```

The command fails on empty input, missing or unexpected identities, duplicate
identities, and either direction of `listed != has_data`.

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
