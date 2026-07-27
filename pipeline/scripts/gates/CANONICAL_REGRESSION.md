# Stage E canonical regression runner

## Scope

The canonical regression scope is the repository root `tests/` tree. The runner
is intentionally read-only with respect to the baseline: it compares the current
JUnit output to the supplied baseline and writes only audit output under the
chosen output directory.

```bash
python3 pipeline/scripts/gates/canonical_regression.py \
  --baseline pipeline/scripts/gates/canonical_regression_baseline.json
```

The runner executes:

```bash
PYTHONPATH=pipeline/scripts/ingest_hook:. \
python3 -m pytest -q -p no:randomly tests --junitxml=<output-dir>/junit.xml
```

`chat/jw-chat-agent-poc` and `chat/wf301-vdb-bridge` remain separate test roots
and are not included in this canonical root regression process.

## Baseline contract

Stage E uses baseline schema version `2`. A valid baseline records:

- `baseline_commit`: the exact 40-character commit digest that produced the
  baseline measurement;
- `baseline_tree_digest`: the exact tree digest for that baseline commit;
- collected, passed, failed, error, and skipped counts;
- the complete collected node-ID list;
- the complete expected failure node-ID list;
- the complete expected collection-error node-ID list.

Before running pytest, the gate verifies that the current `HEAD` descends from
`baseline_commit` and that `baseline_commit^{tree}` still resolves to
`baseline_tree_digest`. A lineage mismatch or tree mismatch fails closed.

## Comparison contract

The verdict compares counts and node-ID sets exactly. This catches:

- removed collected tests, named in `missing_collected`;
- newly collected tests, named in `unexpected_collected`;
- same-count failure substitutions, reported as both `missing_failures` and
  `unexpected_failures`;
- collection-error substitutions through the same missing/unexpected model.

The runner never updates baseline values. Any baseline change must be prepared
outside this runner and reviewed as a separate artifact.

## Audit output

Each run writes:

- `junit.xml`;
- `pytest_stdout.txt`;
- `pytest_stderr.txt`;
- `verdict.json`.

`verdict.json` preserves the command, worktree path, Git metadata, full actual
summary, full baseline payload, and structured verdict.
