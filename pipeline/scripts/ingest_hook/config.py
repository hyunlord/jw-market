"""Environment contract for the ingest hook (single place, fail-closed reads).

The Job image default deliberately equals the digest-pinned orchestrator image
from deploy/k8s/orchestrator/pipeline-orchestrator-poll-cronjob.yaml so the
ingest Job and the orchestrator poll chain execute the same code by construction.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_INPUT_ROOT = "INGEST_INPUT_ROOT"            # submission bucket/NFS mount root
ENV_LEDGER_SQLITE = "INGEST_LEDGER_SQLITE"      # set => sqlite ledger (rehearsal/tests)
ENV_JOB_IMAGE = "INGEST_JOB_IMAGE"
ENV_JOB_NAMESPACE = "INGEST_JOB_NAMESPACE"
ENV_REHEARSAL_ROOT = "INGEST_REHEARSAL_ROOT"    # set => job_runner isolation mode
ENV_UBIST_TARGET_DIR = "INGEST_UBIST_TARGET_DIR"  # existing full UBIST parquet root
# INGEST_S3_BUCKET (s3_input.ENV_BUCKET): set => submissions read from MinIO/S3

DEFAULT_NAMESPACE = "llmops"
DEFAULT_JOB_IMAGE = (
    "asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01/"
    "jw-pipeline-orchestrator@sha256:e79aa0986a4e2163849d97d4b3aafacd05fe4db7fc3db48dc36a96164b4c46d8"
)


def input_root() -> Path:
    value = os.environ.get(ENV_INPUT_ROOT, "")
    if not value:
        raise RuntimeError(f"{ENV_INPUT_ROOT} is required (submission root; no default on purpose)")
    return Path(value)


def ubist_target_dir() -> Path:
    value = os.environ.get(ENV_UBIST_TARGET_DIR, "")
    if not value:
        raise RuntimeError(
            f"{ENV_UBIST_TARGET_DIR} is required for real UBIST incremental loads"
        )
    return Path(value).resolve()


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
    """S3Input when INGEST_S3_BUCKET is set (MinIO submissions), else None (local root)."""
    from pipeline.scripts.ingest_hook.s3_input import S3Input

    return S3Input.from_env()
