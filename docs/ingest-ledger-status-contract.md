# Ingest ledger status contract

`pipeline/scripts/ingest_hook/ledger.py` is the executable source of truth.
Consumers must reject unknown values instead of assigning them a fallback
meaning.

| Status | Meaning | Terminal | Retryable | Producer |
| --- | --- | --- | --- | --- |
| `queued` | Accepted and waiting for category promotion | No | No | webhook/sweep receipt |
| `running` | Kubernetes Job or inline runner owns the submission | No | No | job submission |
| `complete` | Load and required publication steps completed | Yes | No | job runner/reconciler |
| `failed` | Runtime, loader, permission, or terminal Job failure | Yes | Yes | job runner/reconciler |
| `gate_failed` | Data or publication safety gate failed | Yes | Yes | job runner |
| `rejected` | Policy rejected a corrected copy before loading | Yes | No | correction reject gate |

`reconcile_terminal()` accepts only `complete` and `failed` because it maps
terminal Kubernetes observations for an existing `running` row. That input
restriction does not change the wider terminal set above.

## Consumer checklist

When adding a status:

1. Update `KNOWN_STATUSES`, `TERMINAL_STATUSES`, and `RETRYABLE_STATUSES`.
2. Audit `Ledger.receive()`, `sweep.sweep()`, and `job_runner.run()`.
3. Verify `/ingest/webhook` and `/ingest/status` expose both status and reason.
4. Verify force-stop and terminal reconciliation do not select the new value.
5. Inject the new and an unknown value in SQLite and MariaDB contract tests.
