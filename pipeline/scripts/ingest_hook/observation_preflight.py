"""Refuse to start an ingest whose ledger schema cannot record what happens.

Why this exists, concretely: `_DDL_SIGNAL_MYSQL` landed in `ledger.py` on 2026-07-22
23:26 KST. Every other observation DDL was applied to the serving schema within hours of
its commit; that one never was, because the runbook enumerated two of the four tables and
nothing checked. On 2026-07-25 an ingest of 814,221 rows therefore completed with its
completion signal silently dropped, and the ledger kept an older failure as the current
state of a run that had succeeded.

This gate is not a policy and has no flag. It is a precondition check: if a table the run
is about to write does not exist, or exists without the columns the code writes, the run
stops before touching anything and names the artifact to apply. A missing activation step
becomes a loud refusal instead of silently degraded evidence.

Scope of the check is deliberately narrow and stated rather than implied: table presence
and column NAMES. Types, nullability and indexes are not compared — see
`Ledger.ddl_column_names` for why a text comparison of full column definitions is not
decidable against MariaDB's own rewriting, and the A-3 fingerprint design for the
information_schema-based comparator that would cover them.
"""
from __future__ import annotations

from pipeline.scripts.ingest_hook.ledger import Ledger


class ObservationPreflightError(RuntimeError):
    """The configured ledger schema cannot durably record this run."""


def _describe(table: str, entry: dict) -> str:
    if entry["verdict"] == "absent":
        return f"  {table}: MISSING -> apply {entry['artifact']}"
    if entry["verdict"] == "unreadable":
        return (
            f"  {table}: SCHEMA UNREADABLE ({entry['error']})"
            " -> the schema state is unknown, which is not the same as usable"
        )
    detail = []
    if entry.get("missing_columns"):
        detail.append(f"missing columns {entry['missing_columns']}")
    if entry.get("unexpected_columns"):
        detail.append(f"unexpected columns {entry['unexpected_columns']}")
    return (
        f"  {table}: SCHEMA MISMATCH ({'; '.join(detail)})"
        f" -> reconcile against {entry['artifact']}"
    )


def verify(ledger: Ledger) -> dict[str, dict]:
    """Return the schema report, or raise ObservationPreflightError.

    Returns the full report on success so callers can log what they verified rather
    than asserting a silent pass.
    """
    report = ledger.observation_schema_report()
    problems = {table: entry for table, entry in report.items() if entry["verdict"] != "ok"}
    if problems:
        lines = [_describe(table, entry) for table, entry in sorted(problems.items())]
        raise ObservationPreflightError(
            "ingest observation schema is not activated for this run; "
            f"{len(problems)} of {len(report)} required tables are unusable:\n"
            + "\n".join(lines)
            + "\nActivation DDL is applied out-of-band (PL gate); the Job never creates "
              "tables itself. See pipeline/scripts/ingest_hook/README.md."
        )
    return report
