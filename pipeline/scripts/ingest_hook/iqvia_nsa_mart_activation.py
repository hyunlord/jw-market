"""IQVIA NSA full-reload build and atomic serving publication."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any

from pipeline.etl.io import iqvia_loader
from pipeline.etl.io.mart.general_config import IQVIA_RETENTION_PERIODS
from pipeline.scripts.deploy.mart_load_ops import (
    PublishAction,
    publish_table_group_atomically,
    quote_id,
    run_s4_general,
)
from pipeline.scripts.ingest_hook.iqvia_nsa_publication import (
    PublicationEvidence,
    record_publication_provenance,
    rollback_publication,
)
from pipeline.scripts.ingest_hook.ubist_mart_activation import (
    ENV_PROMOTION_APPROVED,
    GENERAL_TABLES,
)
from pipeline.scripts.rollback.recording import (
    PromotionIdentity,
    record_mysql_component,
)


ENV_SOURCE_DB = "INGEST_MART_SOURCE_DB"
ENV_TARGET_DB = "INGEST_MART_TARGET_DB"
ENV_BUILD_PREFIX = "INGEST_MART_BUILD_PREFIX"
ENV_BUILDER_COMMIT = "APP_VERSION"
ENV_IMAGE_DIGEST = "INGEST_IMAGE_DIGEST"
ENV_IMAGE_REF = "INGEST_JOB_IMAGE"
NSA_PUBLISH_TABLES = (iqvia_loader.NSA_TABLE, *GENERAL_TABLES)
_SCHEMA_RE = re.compile(r"^[A-Za-z0-9_]+$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class NsaMartActivation:
    source_db: str
    target_db: str
    build_db: str
    builder_commit: str
    image_digest: str
    image_ref: str


def from_env(*, run_id: str) -> NsaMartActivation:
    if os.environ.get(ENV_PROMOTION_APPROVED, "").strip() != "1":
        raise RuntimeError(
            f"production NSA activation requires {ENV_PROMOTION_APPROVED}=1 "
            "after the explicit PL gate"
        )
    source_db = os.environ.get(ENV_SOURCE_DB, "jw_mart").strip()
    target_db = os.environ.get(ENV_TARGET_DB, source_db).strip()
    prefix = os.environ.get(ENV_BUILD_PREFIX, "jw_ingest_nsa_build").strip()
    safe_run_id = re.sub(r"[^A-Za-z0-9_]", "_", run_id)
    config = NsaMartActivation(
        source_db=source_db,
        target_db=target_db,
        build_db=f"{prefix}_{safe_run_id}",
        builder_commit=os.environ.get(ENV_BUILDER_COMMIT, "").strip().lower(),
        image_digest=os.environ.get(ENV_IMAGE_DIGEST, "").strip().lower(),
        image_ref=os.environ.get(ENV_IMAGE_REF, "").strip(),
    )
    for label, value in (
        (ENV_SOURCE_DB, config.source_db),
        (ENV_TARGET_DB, config.target_db),
        (ENV_BUILD_PREFIX, prefix),
        ("build_db", config.build_db),
    ):
        if not _SCHEMA_RE.fullmatch(value):
            raise RuntimeError(f"{label} is not a safe schema identifier: {value!r}")
    if config.build_db in {config.source_db, config.target_db, "jw_mart"}:
        raise RuntimeError("NSA build schema must be isolated from source and serving schemas")
    _validate_provenance(config)
    return config


def require_production_mode(load_mode: str) -> None:
    """Refuse to route an NSA shadow run through the production publication path."""

    if load_mode == "shadow":
        raise RuntimeError(
            "IQVIA NSA shadow publication is not implemented; "
            "use rehearsal/staging or the explicitly approved production path"
        )


def _validate_provenance(config: NsaMartActivation) -> None:
    if not _COMMIT_RE.fullmatch(config.builder_commit):
        raise RuntimeError(
            f"publication provenance requires full 40-character {ENV_BUILDER_COMMIT}"
        )
    if not _DIGEST_RE.fullmatch(config.image_digest):
        raise RuntimeError(
            f"publication provenance requires pinned sha256 {ENV_IMAGE_DIGEST}"
        )
    if not config.image_ref.endswith(f"@{config.image_digest}"):
        raise RuntimeError(
            f"publication provenance requires immutable {ENV_IMAGE_REF} "
            f"matching {ENV_IMAGE_DIGEST}"
        )


def initialize_build_schema(config: NsaMartActivation) -> None:
    """Create a new empty raw table; never relax the loader's non-empty guard."""

    iqvia_loader.init_target_schema(config.build_db, config.source_db)


def trim_raw_retention(conn: Any, config: NsaMartActivation) -> tuple[str, ...]:
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT DISTINCT period_label FROM {quote_id(config.build_db)}."
            f"{quote_id(iqvia_loader.NSA_TABLE)} "
            "WHERE period_label IS NOT NULL ORDER BY period_label"
        )
        rows = cursor.fetchall()
        periods = tuple(
            str(row.get("period_label") if isinstance(row, dict) else row[0])
            for row in rows
        )
        retained = periods[-IQVIA_RETENTION_PERIODS:]
        expired = periods[:-IQVIA_RETENTION_PERIODS]
        if expired:
            marks = ", ".join(["%s"] * len(expired))
            cursor.execute(
                f"DELETE FROM {quote_id(config.build_db)}."
                f"{quote_id(iqvia_loader.NSA_TABLE)} WHERE period_label IN ({marks})",
                expired,
            )
            conn.commit()
        return retained
    finally:
        cursor.close()


def build_mart(config: NsaMartActivation, *, catalog_root: str | None = None) -> None:
    run_s4_general(
        build_db=config.build_db,
        source_db=config.source_db,
        input_db=config.build_db,
        catalog_root=None if catalog_root is None else Path(catalog_root),
        ubist_dir=None,
        input_mode="raw",
        sources=("iqvia_nsa",),
    )


def publish(
    conn: Any,
    config: NsaMartActivation,
    *,
    run_id: str,
    epoch: str,
    post_gate_verified: bool,
    publication_evidence: PublicationEvidence,
) -> tuple[PublishAction, ...]:
    _validate_provenance(config)
    if not post_gate_verified:
        raise RuntimeError(
            f"promotion blocked: post_gate was not verified for ingest_run_id={run_id}"
        )
    actions = publish_table_group_atomically(
        conn,
        build_db=config.build_db,
        target_db=config.target_db,
        run_id=run_id,
        tables=NSA_PUBLISH_TABLES,
    )
    component_recorded = False
    provenance_recorded = False
    try:
        record_mysql_component(
            conn,
            identity=PromotionIdentity(
                promotion_run_id=run_id,
                epoch=epoch,
                ingest_run_id=run_id,
                serving_db=config.target_db,
                generation_db=config.build_db,
            ),
            component="general",
            table_pairs=tuple(
                (action.table, action.backup_table)
                for action in actions
                if action.backup_table is not None
            ),
        )
        component_recorded = True
        record_publication_provenance(
            conn,
            config,
            run_id=run_id,
            epoch=epoch,
            evidence=publication_evidence,
        )
        provenance_recorded = True
    except Exception:
        rollback_publication(
            conn,
            config,
            actions=actions,
            run_id=run_id,
            provenance_recorded=provenance_recorded,
            component_recorded=component_recorded,
        )
        raise
    return actions
