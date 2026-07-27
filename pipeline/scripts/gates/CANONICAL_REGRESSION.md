# Canonical regression

## Scope

The repository-wide regression scope is the root `tests/` tree. Run it from a
clean worktree at the exact commit under review:

```bash
python3 pipeline/scripts/gates/canonical_regression.py
```

The runner executes this command with the current Python interpreter:

```bash
PYTHONPATH=pipeline/scripts/ingest_hook:. \
python3 -m pytest -q -p no:randomly tests --junitxml=<temporary-path>/junit.xml
```

`chat/jw-chat-agent-poc` and `chat/wf301-vdb-bridge` are separate components.
They have independent dependency and test roots, so their tests are not added
to the root pytest process:

```bash
cd chat/jw-chat-agent-poc
PYTHONPATH=. python3 -m pytest -q -p no:randomly tests

cd chat/wf301-vdb-bridge
PYTHONPATH=. python3 -m pytest -q -p no:randomly tests
```

## Baseline contract

`pipeline/scripts/gates/canonical_regression_baseline.json` records:

- collected, passed, failed, error, and skipped counts;
- the complete expected failure node ID set;
- the complete expected collection-error node ID set.

The gate succeeds only when all counts and both node ID sets match exactly.
A replacement failure cannot be hidden by an old failure disappearing. Missing
JUnit output, unresolved test source paths, malformed baseline fields, a dirty
worktree, and unknown outcome counts all fail closed.

The current accepted baseline is not a waiver that the tests are healthy. It
is a measurement reference for detecting new regressions. Every baseline
change requires a review that explains each added or removed node ID.

## Audit evidence

Every audit that reports a full regression must preserve:

1. the literal command and Python executable;
2. the absolute clean-worktree path and full Git SHA;
3. the number of collected test files and test cases;
4. passed, failed, error, and skipped counts;
5. the complete failure and collection-error node ID sets;
6. `junit.xml`, pytest stdout/stderr, and the gate verdict.

Reporting only a pass/fail count is insufficient. A focused suite must be
labelled focused and must not be reported as the repository-wide regression.
