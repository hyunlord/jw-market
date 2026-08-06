"""Typed MI Master definition-refresh request contract."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pipeline.scripts.ingest_hook.ledger import STAGE_COMPLETE, StageEvent

CATEGORY = "mi_master_definition"
WORKFLOW_REF_URI = "workflow://mi-master-definition-refresh"
ALLOWED_CACHE_REFRESH_TABLES = ("cache_brands", "cache_market_status")
STAGES = (
    "catalog_sync",
    "scope_plan",
    "candidate_build",
    "sigma",
    "post_gate",
    "awaiting_approval",
    "mart_publish",
    "cache_refresh",
    "catalog_invalidate",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MissingStageError(RuntimeError):
    """The source-specific MI Master progress contract is incomplete."""

    def __init__(self, missing: tuple[str, ...]):
        self.missing = missing
        super().__init__(
            "missing MI Master definition refresh stages: " + ", ".join(missing)
        )


@dataclass(frozen=True, slots=True)
class DefinitionRefreshIdentity:
    mi_master_sha256: str
    catalog_diff_hash: str
    run_id: str

    @property
    def ledger_epoch(self) -> str:
        return f"mi-master-{self.mi_master_sha256[:12]}"

    def validate(self) -> None:
        for label, value in (
            ("mi_master_sha256", self.mi_master_sha256),
            ("catalog_diff_hash", self.catalog_diff_hash),
        ):
            if _SHA256_RE.fullmatch(value) is None:
                raise RuntimeError(f"{label} must be a lowercase sha256 hex digest")
        if not self.run_id.strip():
            raise RuntimeError("run_id is required")

    def as_dict(self) -> dict[str, str]:
        return {
            "mi_master_sha256": self.mi_master_sha256,
            "catalog_diff_hash": self.catalog_diff_hash,
            "run_id": self.run_id,
        }


@dataclass(frozen=True, slots=True)
class PublishWorkspace:
    candidate_root: Path
    backup_root: Path
    journal_path: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "candidate_root": str(self.candidate_root),
            "backup_root": str(self.backup_root),
            "journal_path": str(self.journal_path),
        }


@dataclass(frozen=True, slots=True)
class PipelineCatalogSync:
    output_root: Path
    input_file: Path
    catalog_root: Path
    cache_dir: Path | None = None
    inputs_dir: Path | None = None
    ubist_dir: Path | None = None
    iqvia_nsa_dir: Path | None = None
    ingested_at: str | None = None

    def as_dict(self) -> dict[str, str]:
        payload = {
            "output_root": str(self.output_root),
            "input_file": str(self.input_file),
            "catalog_root": str(self.catalog_root),
        }
        for key in ("cache_dir", "inputs_dir", "ubist_dir", "iqvia_nsa_dir", "ingested_at"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = str(value)
        return payload


@dataclass(frozen=True, slots=True)
class DefinitionRefreshRequest:
    identity: DefinitionRefreshIdentity
    workspace: PublishWorkspace
    market_ordinal: int | None = None
    catalog_sync: PipelineCatalogSync | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "identity": self.identity.as_dict(),
            "workspace": self.workspace.as_dict(),
        }
        if self.market_ordinal is not None:
            payload["market_ordinal"] = self.market_ordinal
        if self.catalog_sync is not None:
            payload["catalog_sync"] = self.catalog_sync.as_dict()
        return payload


@dataclass(frozen=True, slots=True)
class PublishReceipt:
    journal_path: Path
    backup_root: Path


class PrepareAdapters(Protocol):
    def catalog_sync(self, request: DefinitionRefreshRequest) -> None: ...
    def scope_plan(self, request: DefinitionRefreshRequest) -> None: ...
    def candidate_build(self, request: DefinitionRefreshRequest) -> None: ...
    def sigma(self, request: DefinitionRefreshRequest) -> None: ...
    def post_gate(self, request: DefinitionRefreshRequest) -> None: ...


class AtomicPublishOrchestrator(Protocol):
    def publish(
        self, workspace: PublishWorkspace, identity: DefinitionRefreshIdentity
    ) -> PublishReceipt: ...


class CacheRefresher(Protocol):
    def refresh_tables(self, tables: tuple[str, ...]) -> tuple[str, ...]: ...


class RuntimeCatalogInvalidator(Protocol):
    def invalidate(self, identity: DefinitionRefreshIdentity) -> None: ...


def _parse_path(payload: dict[str, object], key: str) -> Path:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"request workspace.{key} is required")
    return Path(value)


def _parse_optional_path(payload: dict[str, object], key: str) -> Path | None:
    value = str(payload.get(key) or "").strip()
    return Path(value) if value else None


def _parse_catalog_sync(payload: object) -> PipelineCatalogSync | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise RuntimeError("definition request catalog_sync must be an object")
    return PipelineCatalogSync(
        output_root=_parse_path(payload, "output_root"),
        input_file=_parse_path(payload, "input_file"),
        catalog_root=_parse_path(payload, "catalog_root"),
        cache_dir=_parse_optional_path(payload, "cache_dir"),
        inputs_dir=_parse_optional_path(payload, "inputs_dir"),
        ubist_dir=_parse_optional_path(payload, "ubist_dir"),
        iqvia_nsa_dir=_parse_optional_path(payload, "iqvia_nsa_dir"),
        ingested_at=str(payload.get("ingested_at") or "") or None,
    )


def load_definition_request(path: Path) -> DefinitionRefreshRequest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"definition request JSON is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("definition request must be a JSON object")
    identity_payload = payload.get("identity")
    workspace_payload = payload.get("workspace")
    if not isinstance(identity_payload, dict) or not isinstance(workspace_payload, dict):
        raise RuntimeError("definition request requires identity and workspace objects")
    market_ordinal = payload.get("market_ordinal")
    if market_ordinal is not None and not isinstance(market_ordinal, int):
        raise RuntimeError("definition request market_ordinal must be an integer")
    request = DefinitionRefreshRequest(
        identity=DefinitionRefreshIdentity(
            mi_master_sha256=str(identity_payload.get("mi_master_sha256") or ""),
            catalog_diff_hash=str(identity_payload.get("catalog_diff_hash") or ""),
            run_id=str(identity_payload.get("run_id") or ""),
        ),
        workspace=PublishWorkspace(
            candidate_root=_parse_path(workspace_payload, "candidate_root"),
            backup_root=_parse_path(workspace_payload, "backup_root"),
            journal_path=_parse_path(workspace_payload, "journal_path"),
        ),
        market_ordinal=market_ordinal,
        catalog_sync=_parse_catalog_sync(payload.get("catalog_sync")),
    )
    request.identity.validate()
    return request


def assert_complete_stage_contract(events: list[StageEvent]) -> None:
    completed = {event.stage for event in events if event.status == STAGE_COMPLETE}
    missing = tuple(stage for stage in STAGES if stage not in completed)
    if missing:
        raise MissingStageError(missing)
