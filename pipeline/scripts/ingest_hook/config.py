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
ENV_LOAD_SHADOW_ROOT = "INGEST_LOAD_SHADOW_ROOT"    # set => full UBIST gates + isolated mart publish
ENV_SHADOW_LEDGER_SQLITE = "INGEST_SHADOW_LEDGER_SQLITE"  # shadow-only ledger on the RWX output volume
ENV_LOAD_TARGET_ROOT = "INGEST_LOAD_TARGET_ROOT"    # production load output root (live parquet root); refresh runs
ENV_LOG_ROOT = "INGEST_LOG_ROOT"                    # durable RWX PVC root for job logs + post_gate_report (survives pod GC)
ENV_COMPLETION_WEBHOOK_URL = "INGEST_COMPLETION_WEBHOOK_URL"
ENV_COMPLETION_WEBHOOK_ATTEMPTS = "INGEST_COMPLETION_WEBHOOK_ATTEMPTS"
ENV_REQUIRE_SIGNAL_LEDGER_STRICT = "REQUIRE_SIGNAL_LEDGER_STRICT"
ENV_REQUIRE_STAGE_LEDGER_STRICT = "REQUIRE_STAGE_LEDGER_STRICT"

DEFAULT_LOG_ROOT = "/market-output/ingest-logs"     # durable path on llmops-market-output RWX PVC
MARKET_OUTPUT_ROOT = Path("/market-output")
MARKET_OUTPUT_PVC = "llmops-market-output"

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


def log_root() -> Path:
    return Path(log_root_hint())


def completion_webhook() -> tuple[str, int]:
    endpoint = os.environ.get(ENV_COMPLETION_WEBHOOK_URL, "").strip()
    attempts = int(os.environ.get(ENV_COMPLETION_WEBHOOK_ATTEMPTS, "4"))
    return endpoint, min(max(attempts, 3), 5)


def _strict_flag(name: str) -> bool:
    value = os.environ.get(name, "1").strip()
    if value not in {"0", "1"}:
        raise RuntimeError(f"{name} must be 0 or 1, received {value!r}")
    return value == "1"


def require_signal_ledger_strict() -> bool:
    """Require durable signal recording, defaulting to fail-closed.

    Setting REQUIRE_SIGNAL_LEDGER_STRICT=0 restores the legacy best-effort
    behavior where a signal ledger write failure is logged and ignored.
    """

    return _strict_flag(ENV_REQUIRE_SIGNAL_LEDGER_STRICT)


def require_stage_ledger_strict() -> bool:
    """Require durable per-stage recording, defaulting to fail-closed.

    A lost stage row means the run's step-level evidence is unknown, not that the
    step succeeded, so forward-progress recording failures must surface. Setting
    REQUIRE_STAGE_LEDGER_STRICT=0 restores the legacy best-effort behavior where
    the failure is printed to stderr and the run continues — which is why the
    per-stage observation silently disappeared whenever ingest_stage_event was
    unavailable. Keep it at 1 unless an emergency needs the old behavior back.
    """

    return _strict_flag(ENV_REQUIRE_STAGE_LEDGER_STRICT)


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

    shadow_ledger = os.environ.get(ENV_SHADOW_LEDGER_SQLITE, "").strip()
    if load_mode(required=False) == "shadow":
        if not shadow_ledger:
            raise RuntimeError(
                f"shadow mode requires {ENV_SHADOW_LEDGER_SQLITE}; "
                "the operational mart ledger must remain untouched"
            )
        return open_sqlite_ledger(Path(shadow_ledger))

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


def configured_ledger_source() -> str:
    """Name the ledger open_configured_ledger() would return, without opening it.

    Values match the ``ledger_source`` field the status API reports:
      "shadow" — the isolated sqlite rehearsal ledger on the RWX volume
      "sqlite" — an explicitly configured sqlite path (INGEST_LEDGER_SQLITE)
      "d2"     — the operational mart ledger (mysql)
    """
    if load_mode(required=False) == "shadow":
        return "shadow"
    if ledger_sqlite_path() is not None:
        return "sqlite"
    return "d2"


def open_ledger_by_source(source: str):
    """Open one specific ledger regardless of which one this pod is bound to.

    The status API needs this because the ledger binding is a side effect of the
    *load output* env — INGEST_LOAD_SHADOW_ROOT decides both — so a pod can be
    bound to the rehearsal ledger while the operational record it must report
    lives in the mart ledger.  Opening the other one for a read changes no
    binding and writes nothing.
    """
    from pipeline.scripts.ingest_hook.ledger import Ledger, open_sqlite_ledger

    if source == "shadow":
        shadow_ledger = os.environ.get(ENV_SHADOW_LEDGER_SQLITE, "").strip()
        if not shadow_ledger:
            raise RuntimeError(f"{ENV_SHADOW_LEDGER_SQLITE} is not set")
        return open_sqlite_ledger(Path(shadow_ledger))
    if source == "sqlite":
        sqlite_path = ledger_sqlite_path()
        if sqlite_path is None:
            raise RuntimeError(f"{ENV_LEDGER_SQLITE} is not set")
        return open_sqlite_ledger(sqlite_path)
    if source != "d2":
        raise ValueError(f"unknown ledger source: {source!r}")

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


def counterpart_ledger_source() -> str | None:
    """The ledger this pod is NOT bound to but may still have to report on.

    None means there is no configured counterpart.  That is a different answer
    from "the counterpart exists but could not be read", and the status API
    keeps the two apart rather than collapsing both into "no rows".
    """
    primary = configured_ledger_source()
    if primary == "d2":
        return "shadow" if os.environ.get(ENV_SHADOW_LEDGER_SQLITE, "").strip() else None
    return "d2"


def open_mart_connection(database: str | None = None):
    """pymysql connection to the mart DB the Job env points at (MARIADB_* family)."""
    import os

    import pymysql

    from pipeline.scripts.utils.mart_config import resolve_mart_db_name

    return pymysql.connect(
        host=os.environ.get("MARIADB_HOST") or os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT") or os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("MARIADB_USER") or os.environ.get("DB_USER", ""),
        password=os.environ.get("MARIADB_PASSWORD") or os.environ.get("DB_PASSWORD", ""),
        database=database or resolve_mart_db_name("MARIADB_DATABASE", "DB_NAME"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
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


def _configured_load_roots() -> dict[str, str]:
    roots = {
        "staging": os.environ.get(ENV_LOAD_STAGING_ROOT, "").strip(),
        "shadow": os.environ.get(ENV_LOAD_SHADOW_ROOT, "").strip(),
        "production": os.environ.get(ENV_LOAD_TARGET_ROOT, "").strip(),
    }
    enabled = {mode: root for mode, root in roots.items() if root}
    if len(enabled) > 1:
        names = {
            "staging": ENV_LOAD_STAGING_ROOT,
            "shadow": ENV_LOAD_SHADOW_ROOT,
            "production": ENV_LOAD_TARGET_ROOT,
        }
        raise RuntimeError(
            f"{', '.join(names[mode] for mode in enabled)} are mutually exclusive"
        )
    return enabled


def load_mode(*, required: bool = True) -> str | None:
    """Return staging, shadow, or production after enforcing exclusivity."""

    enabled = _configured_load_roots()
    if enabled:
        return next(iter(enabled))
    if required:
        raise RuntimeError(
            f"neither {ENV_LOAD_STAGING_ROOT}, {ENV_LOAD_SHADOW_ROOT}, nor "
            f"{ENV_LOAD_TARGET_ROOT} is set; the real load has no output root"
        )
    return None


def load_output_root() -> tuple[Path, bool]:
    """Return (target_root, staging_verify) for the real load's parquet output.

    Exactly one mode must be configured:
      * INGEST_LOAD_STAGING_ROOT -> (that root, staging_verify=True):
        the load writes parquet under an isolated staging root and the
        mart-writing downstream refresh is SKIPPED. This is the J5 isolated
        verification mode (real loader, zero mart write).
      * INGEST_LOAD_SHADOW_ROOT -> (that root, staging_verify=False):
        the complete UBIST gate/publish path runs against an isolated corpus,
        shadow-prefixed schemas, and a separate ledger. Serving refresh is
        structurally unavailable.
      * INGEST_LOAD_TARGET_ROOT -> (that root, staging_verify=False):
        production output root (the live parquet root); refresh runs.
      * neither set -> fail closed. There is no implicit default so a
        mis-provisioned Job cannot silently write parquet to an image-local
        path that the refresh never reads.
    """
    enabled = _configured_load_roots()
    if not enabled:
        load_mode()
    mode, root = next(iter(enabled.items()))
    return Path(root), mode == "staging"


def load_target_mount_root() -> Path | None:
    """Return the RWX mount root for shadow/production, else None."""

    enabled = _configured_load_roots()
    if not enabled:
        return None
    mode, root = next(iter(enabled.items()))
    if mode == "staging":
        return None
    if mode == "shadow":
        shadow_root = Path(root)
        try:
            shadow_root.relative_to(MARKET_OUTPUT_ROOT)
        except ValueError as exc:
            raise RuntimeError(
                f"{ENV_LOAD_SHADOW_ROOT} must be below {MARKET_OUTPUT_ROOT}"
            ) from exc
        if shadow_root == MARKET_OUTPUT_ROOT:
            raise RuntimeError(
                f"{ENV_LOAD_SHADOW_ROOT} must be a child of {MARKET_OUTPUT_ROOT}"
            )
        return MARKET_OUTPUT_ROOT
    return Path(root)
