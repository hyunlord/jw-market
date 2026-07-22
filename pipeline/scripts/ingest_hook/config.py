"""Environment contract for the ingest hook (single place, fail-closed reads).

The Job image default deliberately equals the digest-pinned orchestrator image
from deploy/k8s/orchestrator/pipeline-orchestrator-poll-cronjob.yaml so the
ingest Job and the orchestrator poll chain execute the same code by construction.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_INPUT_ROOT = "INGEST_INPUT_ROOT"            # submission bucket/NFS mount root
ENV_INPUT_BACKEND = "INGEST_INPUT_BACKEND"      # explicit source: s3 | local
ENV_LEDGER_SQLITE = "INGEST_LEDGER_SQLITE"      # set => sqlite ledger (rehearsal/tests)
ENV_JOB_IMAGE = "INGEST_JOB_IMAGE"
ENV_JOB_NAMESPACE = "INGEST_JOB_NAMESPACE"
ENV_REHEARSAL_ROOT = "INGEST_REHEARSAL_ROOT"    # set => job_runner isolation mode
# INGEST_S3_BUCKET (s3_input.ENV_BUCKET): set => submissions read from MinIO/S3
ENV_LOAD_STAGING_ROOT = "INGEST_LOAD_STAGING_ROOT"  # set => real load -> staging root, mart refresh SKIPPED (isolated verify)
ENV_LOAD_STAGING_DB = "INGEST_LOAD_STAGING_DB"      # required isolated DB for category table adapters (jw_ingest_*)
ENV_LOAD_TARGET_ROOT = "INGEST_LOAD_TARGET_ROOT"    # production load output root (live parquet root); refresh runs
ENV_LOG_ROOT = "INGEST_LOG_ROOT"                    # durable RWX PVC root for job logs + post_gate_report (survives pod GC)
ENV_COMPLETION_WEBHOOK_URL = "INGEST_COMPLETION_WEBHOOK_URL"
ENV_COMPLETION_WEBHOOK_ATTEMPTS = "INGEST_COMPLETION_WEBHOOK_ATTEMPTS"

DEFAULT_LOG_ROOT = "/ingest-logs"                   # container mountPath convention for the durable-log PVC

DEFAULT_NAMESPACE = "llmops"
DEFAULT_JOB_IMAGE = (
    "asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01/"
    "jw-pipeline-orchestrator@sha256:030f81837d05b8789b879fc04ddf0865a7953ddd2cb9d26fc8b707bf394e5e12"
)


def log_root_hint() -> str:
    """Durable-log root shown in /ingest/status.log_ref (mountPath convention).

    The Job tees stdout and writes post_gate_report.json under this RWX PVC path so
    logs survive pod GC (L-1/L-2). Purely a display hint here; the Job manifest owns
    the actual mount.
    """
    return os.environ.get(ENV_LOG_ROOT, DEFAULT_LOG_ROOT).rstrip("/")


def completion_webhook() -> tuple[str, int]:
    endpoint = os.environ.get(ENV_COMPLETION_WEBHOOK_URL, "").strip()
    attempts = int(os.environ.get(ENV_COMPLETION_WEBHOOK_ATTEMPTS, "4"))
    return endpoint, min(max(attempts, 3), 5)


def input_root() -> Path:
    value = os.environ.get(ENV_INPUT_ROOT, "")
    if not value:
        raise RuntimeError(f"{ENV_INPUT_ROOT} is required (submission root; no default on purpose)")
    return Path(value)


def job_image() -> str:
    return os.environ.get(ENV_JOB_IMAGE) or DEFAULT_JOB_IMAGE


def job_namespace() -> str:
    return os.environ.get(ENV_JOB_NAMESPACE) or DEFAULT_NAMESPACE


def ledger_sqlite_path() -> Path | None:
    value = os.environ.get(ENV_LEDGER_SQLITE, "")
    return Path(value) if value else None


def open_configured_ledger():
    """sqlite when INGEST_LEDGER_SQLITE is set, else the mart-DB mysql ledger.

    The mysql branch intentionally does NOT create the table implicitly; table
    creation in the production mart is an activation-time, PL-gated step.
    """
    from pipeline.scripts.ingest_hook.ledger import Ledger, open_sqlite_ledger

    sqlite_path = ledger_sqlite_path()
    if sqlite_path is not None:
        return open_sqlite_ledger(sqlite_path)

    import pymysql

    from pipeline.scripts.utils.mart_config import resolve_mart_db_name

    conn = pymysql.connect(
        host=os.environ.get("MARIADB_HOST") or os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT") or os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("MARIADB_USER") or os.environ.get("DB_USER", ""),
        password=os.environ.get("MARIADB_PASSWORD") or os.environ.get("DB_PASSWORD", ""),
        database=resolve_mart_db_name("MARIADB_DATABASE", "DB_NAME"),
        charset="utf8mb4",
        autocommit=False,
    )
    return Ledger(conn, dialect="mysql")


def open_mart_connection():
    """pymysql connection to the mart DB the Job env points at (MARIADB_* family)."""
    import os

    import pymysql

    from pipeline.scripts.utils.mart_config import resolve_mart_db_name

    return pymysql.connect(
        host=os.environ.get("MARIADB_HOST") or os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT") or os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("MARIADB_USER") or os.environ.get("DB_USER", ""),
        password=os.environ.get("MARIADB_PASSWORD") or os.environ.get("DB_PASSWORD", ""),
        database=resolve_mart_db_name("MARIADB_DATABASE", "DB_NAME"),
        charset="utf8mb4",
    )


def open_input_source():
    """Return the configured remote input source, or ``None`` for local/NFS.

    An explicit backend wins over legacy environment discovery so stale MinIO
    variables cannot redirect a deployment armed for the NFS input contract.
    When the selector is absent, retain the legacy behavior for compatibility.
    """
    from pipeline.scripts.ingest_hook.s3_input import S3Input

    backend = os.environ.get(ENV_INPUT_BACKEND, "").strip().lower()
    if not backend:
        return S3Input.from_env()
    if backend == "local":
        input_root()
        return None
    if backend == "s3":
        source = S3Input.from_env()
        if source is None:
            raise RuntimeError("INGEST_INPUT_BACKEND=s3 requires INGEST_S3_BUCKET")
        return source
    raise RuntimeError(
        f"unsupported {ENV_INPUT_BACKEND}={backend!r}; expected 's3' or 'local'"
    )


def load_output_root() -> tuple[Path, bool]:
    """Return (target_root, staging_verify) for the real load's parquet output.

    Precedence:
      * INGEST_LOAD_STAGING_ROOT set -> (that root, staging_verify=True):
        the load writes parquet under an isolated staging root and the
        mart-writing downstream refresh is SKIPPED. This is the J5 isolated
        verification mode (real loader, zero mart write).
      * else INGEST_LOAD_TARGET_ROOT set -> (that root, staging_verify=False):
        production output root (the live parquet root); refresh runs.
      * neither set -> fail closed. There is no implicit default so a
        mis-provisioned Job cannot silently write parquet to an image-local
        path that the refresh never reads.
    """
    staging = os.environ.get(ENV_LOAD_STAGING_ROOT, "").strip()
    if staging:
        return Path(staging), True
    target = os.environ.get(ENV_LOAD_TARGET_ROOT, "").strip()
    if target:
        return Path(target), False
    raise RuntimeError(
        f"neither {ENV_LOAD_STAGING_ROOT} nor {ENV_LOAD_TARGET_ROOT} is set; "
        "the real load has no output root (fail-closed to avoid a silently unread parquet path)"
    )
