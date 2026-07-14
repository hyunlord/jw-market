# Release Acceptance Gates

`release_acceptance.py` is the tracked, fail-closed replacement for ad hoc
release checks. Every command emits the machine-readable acceptance fields and
returns a non-zero status for missing input, an empty census, mismatched API
goldens, strict log matches, or incomplete segment levels.

The runner consumes evidence captured by deployment automation. It does not
open cluster or database connections itself, which keeps it runnable in a
clean clone and prevents a local test from claiming an unmeasured live pass.

## API goldens

Capture each response as an item in a JSON array:

```json
[{"id":"brands","payload":{"data":[]}}]
```

Then run:

```bash
python3 pipeline/scripts/gates/release_acceptance.py goldens \
  --contracts tests/api/api_golden_contracts.json \
  --observations /path/to/api_observations.json \
  --environment test2
```

The observation identity set must exactly equal the four tracked contracts.

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
