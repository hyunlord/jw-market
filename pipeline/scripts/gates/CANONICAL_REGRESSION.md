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

### 2026-07-27 catalog provisioning baseline change

Catalog provisioning added nine environment, integrity, CLI, and ingest
preflight tests. It also supplied the minimum manifest-backed catalog fixture
needed by the strategic reload publisher tests. The following failures were
removed only after their original atomic rename, dry-run no-swap, and rollback
assertions executed and passed unchanged:

```text
tests/deploy/test_strategic_reload_publish.py::test_dry_run_checks_rows_without_swapping
tests/deploy/test_strategic_reload_publish.py::test_publish_calls_atomic_rename_for_each_reload_table
tests/deploy/test_strategic_reload_publish.py::test_publish_restores_successful_backups_after_later_failure
```

Runtime materialization added five more fail-closed cases without changing the
failure set. The measured baseline changed from `2218 collected / 2205 passed /
8 failed / 5 skipped` to `2232 collected / 2222 passed / 5 failed / 5
skipped`. The five remaining failures are the unchanged
`brand_activity/test_auto_topic_core.py` node IDs recorded in the baseline
JSON; this change does not waive or modify them.

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
