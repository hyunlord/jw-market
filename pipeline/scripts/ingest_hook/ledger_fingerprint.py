"""Read-only MariaDB ledger fingerprint gate for CronJob activation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from pipeline.scripts.ingest_hook.ledger import require_known_status

_REQUIRED_ENV = (
    "MARIADB_HOST",
    "MARIADB_PORT",
    "MARIADB_DATABASE",
    "MARIADB_USER",
    "MARIADB_PASSWORD",
)
_SQLITE_ENV = ("INGEST_LEDGER_SQLITE", "INGEST_SHADOW_LEDGER_SQLITE")


class LedgerFingerprintError(RuntimeError):
    """The activation preflight cannot prove the expected MariaDB ledger."""


@dataclass(frozen=True)
class MariaDBTarget:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)

    def public_dict(self) -> dict[str, str | int]:
        return {
            "engine": "mariadb",
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "credential_env": "MARIADB_USER,MARIADB_PASSWORD",
        }


@dataclass(frozen=True)
class LedgerFingerprint:
    target: MariaDBTarget
    total: int
    status_counts: dict[str, int]
    identity_fingerprint: str

    def public_dict(self) -> dict:
        return {
            "storage": self.target.public_dict(),
            "total": self.total,
            "status_counts": self.status_counts,
            "identity_fingerprint": self.identity_fingerprint,
        }


def target_from_env(environ: Mapping[str, str]) -> MariaDBTarget:
    configured_sqlite = [key for key in _SQLITE_ENV if environ.get(key, "").strip()]
    if configured_sqlite:
        raise LedgerFingerprintError(
            "SQLite ledger configuration is forbidden for the CronJob MariaDB preflight: "
            + ", ".join(configured_sqlite)
        )
    missing = [key for key in _REQUIRED_ENV if not environ.get(key, "").strip()]
    if missing:
        raise LedgerFingerprintError(
            "explicit CronJob MariaDB environment is required; missing: "
            + ", ".join(missing)
        )
    try:
        port = int(environ["MARIADB_PORT"])
    except ValueError as exc:
        raise LedgerFingerprintError("MARIADB_PORT must be an integer") from exc
    return MariaDBTarget(
        host=environ["MARIADB_HOST"].strip(),
        port=port,
        database=environ["MARIADB_DATABASE"].strip(),
        user=environ["MARIADB_USER"].strip(),
        password=environ["MARIADB_PASSWORD"],
    )


def _default_connect(**kwargs):
    import pymysql

    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **kwargs)


def collect_fingerprint(
    target: MariaDBTarget,
    *,
    connect: Callable = _default_connect,
) -> LedgerFingerprint:
    connection = connect(
        host=target.host,
        port=target.port,
        database=target.database,
        user=target.user,
        password=target.password,
        charset="utf8mb4",
        autocommit=False,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT epoch, category, manifest_sha, status"
                " FROM ingest_ledger"
                " ORDER BY epoch, category, manifest_sha"
            )
            rows = cursor.fetchall()
        canonical_rows = [
            {
                "epoch": str(row["epoch"]),
                "category": str(row["category"]),
                "manifest_sha": str(row["manifest_sha"]),
                "status": require_known_status(row["status"]),
            }
            for row in rows
        ]
        payload = json.dumps(
            canonical_rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        counts = Counter(row["status"] for row in canonical_rows)
        return LedgerFingerprint(
            target=target,
            total=len(canonical_rows),
            status_counts=dict(sorted(counts.items())),
            identity_fingerprint=hashlib.sha256(payload).hexdigest(),
        )
    finally:
        connection.rollback()
        connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.scripts.ingest_hook.ledger_fingerprint"
    )
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--expected-host")
    parser.add_argument("--expected-database")
    parser.add_argument("--expected-fingerprint")
    return parser


def _decision(
    fingerprint: LedgerFingerprint,
    *,
    report_only: bool,
    expected_host: str | None,
    expected_database: str | None,
    expected_fingerprint: str | None,
) -> tuple[bool, str]:
    if report_only:
        return False, "report-only dry-run; activation remains blocked"
    if not expected_host or not expected_database or not expected_fingerprint:
        raise LedgerFingerprintError(
            "activation gate requires expected host, database, and fingerprint"
        )
    if fingerprint.target.host != expected_host:
        return False, "MariaDB host mismatch"
    if fingerprint.target.database != expected_database:
        return False, "MariaDB database mismatch"
    if fingerprint.identity_fingerprint != expected_fingerprint:
        return False, "ledger identity fingerprint mismatch"
    return True, "MariaDB target and ledger identity fingerprint match"


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] = os.environ,
    connect: Callable = _default_connect,
) -> int:
    args = _parser().parse_args(argv)
    try:
        target = target_from_env(environ)
        fingerprint = collect_fingerprint(target, connect=connect)
        allowed, reason = _decision(
            fingerprint,
            report_only=args.report_only,
            expected_host=args.expected_host,
            expected_database=args.expected_database,
            expected_fingerprint=args.expected_fingerprint,
        )
        payload = {
            **fingerprint.public_dict(),
            "activation_allowed": allowed,
            "reason": reason,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if (allowed or args.report_only) else 3
    except LedgerFingerprintError as exc:
        print(
            json.dumps(
                {
                    "activation_allowed": False,
                    "reason": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary must fail closed.
        print(
            json.dumps(
                {
                    "activation_allowed": False,
                    "reason": (
                        "MariaDB fingerprint query failed: "
                        f"{type(exc).__name__}"
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
