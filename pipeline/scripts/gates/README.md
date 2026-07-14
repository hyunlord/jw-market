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

For a live deployment, use `collect_strict_logs.sh`. It derives the deployment
selector, captures every non-deleting pod, and lets the tracked runner
distinguish an empty pod census, a log retrieval failure, no matches, and real
strict matches. Historical one-pod fail-open grep scripts are evidence, not
reusable gates.

## Live backend goldens

The all-four wrapper calls each backend endpoint at gate time and compares the
canonical response hash with the tracked contract registry:

```bash
pipeline/scripts/gates/all_four_v2_verify_audit.sh \
  https://jwai-dev.jwhealthcare.com production
```

Expected hashes are read only from
`tests/gates/chat_backend_live_goldens.json`, which must be tracked in the same
Git repository. Historical observation files are evidence only and cannot be
used as either expected values or live observations. Contracts without an
owner-confirmed truth basis remain disabled and are reported as observations.

## Independent population census

The candidates JSON is an array. The census JSON must come from an independent
query path, such as direct mart SQL, and has this shape:

```json
{"population":24789,"source":"direct mart SQL: ..."}
```

An empty candidate set always fails, including when the expected count is
zero.

Use a distinct `--gate-id` for each parity path. F-062 molecule and corpus
parity are separate gates even though both use the same independent-census
contract. Their census evidence must declare `source_kind=direct_db_count` and
preserve the independent `COUNT(...)` query; an API-derived candidate count is
rejected as a census.
Use `f062_population_gate.sh` for both named F-062 paths.

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
