from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pipeline.etl.mi_master_refresh.contracts import SUPPORTED_REFRESH_CACHE_TABLES
from pipeline.scripts.ingest_hook.ledger import STAGE_COMPLETE, StageEvent

CATEGORY = "mi_master_definition"
WORKFLOW_REF_URI = "workflow://mi-master-definition-refresh"
ALLOWED_CACHE_REFRESH_TABLES = SUPPORTED_REFRESH_CACHE_TABLES
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
    def __init__(self, missing: tuple[str, ...]):
        self.missing = missing
        super().__init__("missing MI Master definition refresh stages: " + ", ".join(missing))


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
        if not self.run_id.strip(): raise RuntimeError("run_id is required")

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
    target_db: str
    sync_catalog_db: str
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
            "target_db": self.target_db,
            "sync_catalog_db": self.sync_catalog_db,
        }
        for key in ("cache_dir", "inputs_dir", "ubist_dir", "iqvia_nsa_dir", "ingested_at"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = str(value)
        return payload


@dataclass(frozen=True, slots=True)
class ScopePlanRequest:
    affected_definitions: tuple[Mapping[str, object], ...]
    existing_general_atc4: tuple[str, ...]
    all_ml_ids: tuple[str, ...]
    all_cd_ids: tuple[str, ...]
    output_path: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "affected_definitions": [dict(item) for item in self.affected_definitions],
            "existing_general_atc4": list(self.existing_general_atc4),
            "all_ml_ids": list(self.all_ml_ids),
            "all_cd_ids": list(self.all_cd_ids),
            "output_path": str(self.output_path),
        }


@dataclass(frozen=True, slots=True)
class CandidateBuildRequest:
    target_db: str
    source_db: str
    general_source_db: str
    catalog_root: Path
    affected_ml_ids: tuple[str, ...]
    affected_cd_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "target_db": self.target_db,
            "source_db": self.source_db,
            "general_source_db": self.general_source_db,
            "catalog_root": str(self.catalog_root),
            "affected_ml_ids": list(self.affected_ml_ids),
            "affected_cd_ids": list(self.affected_cd_ids),
        }


@dataclass(frozen=True, slots=True)
class DefinitionRefreshRequest:
    identity: DefinitionRefreshIdentity
    workspace: PublishWorkspace
    market_ordinal: int | None = None
    catalog_sync: PipelineCatalogSync | None = None
    scope_plan: ScopePlanRequest | None = None
    candidate_build: CandidateBuildRequest | None = None
    validation: Mapping[str, object] | None = None
    post_gate: Mapping[str, object] | None = None
    publish_plan: Mapping[str, object] | None = None
    cache_refresh: Mapping[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "identity": self.identity.as_dict(),
            "workspace": self.workspace.as_dict(),
        }
        if self.market_ordinal is not None:
            payload["market_ordinal"] = self.market_ordinal
        for key in ("catalog_sync", "scope_plan", "candidate_build"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value.as_dict()
        for key in ("validation", "post_gate", "publish_plan", "cache_refresh"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = dict(value)
        return payload


@dataclass(frozen=True, slots=True)
class PublishReceipt:
    journal_path: Path
    backup_root: Path


def _path(payload: Mapping[str, object], key: str) -> Path:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"definition request {key} is required")
    return Path(value)


def _optional_path(payload: Mapping[str, object], key: str) -> Path | None:
    value = str(payload.get(key) or "").strip()
    return Path(value) if value else None


def _strs(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key, ())
    if not isinstance(value, list):
        raise RuntimeError(f"definition request {key} must be a list")
    return tuple(str(item) for item in value)


def _mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise RuntimeError(f"definition request {key} must be an object")
    return value


def _catalog_sync(payload: object) -> PipelineCatalogSync | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise RuntimeError("definition request catalog_sync must be an object")
    return PipelineCatalogSync(
        output_root=_path(payload, "output_root"),
        input_file=_path(payload, "input_file"),
        catalog_root=_path(payload, "catalog_root"),
        target_db=str(payload.get("target_db") or ""),
        sync_catalog_db=str(payload.get("sync_catalog_db") or ""),
        cache_dir=_optional_path(payload, "cache_dir"),
        inputs_dir=_optional_path(payload, "inputs_dir"),
        ubist_dir=_optional_path(payload, "ubist_dir"),
        iqvia_nsa_dir=_optional_path(payload, "iqvia_nsa_dir"),
        ingested_at=str(payload.get("ingested_at") or "") or None,
    )


def _scope_plan(payload: object) -> ScopePlanRequest | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise RuntimeError("definition request scope_plan must be an object")
    affected = payload.get("affected_definitions")
    if not isinstance(affected, list) or not all(isinstance(item, dict) for item in affected):
        raise RuntimeError("definition request affected_definitions must be object list")
    return ScopePlanRequest(
        affected_definitions=tuple(affected),
        existing_general_atc4=_strs(payload, "existing_general_atc4"),
        all_ml_ids=_strs(payload, "all_ml_ids"),
        all_cd_ids=_strs(payload, "all_cd_ids"),
        output_path=_path(payload, "output_path"),
    )


def _candidate_build(payload: object) -> CandidateBuildRequest | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise RuntimeError("definition request candidate_build must be an object")
    return CandidateBuildRequest(
        target_db=str(payload.get("target_db") or ""),
        source_db=str(payload.get("source_db") or ""),
        general_source_db=str(payload.get("general_source_db") or ""),
        catalog_root=_path(payload, "catalog_root"),
        affected_ml_ids=_strs(payload, "affected_ml_ids"),
        affected_cd_ids=_strs(payload, "affected_cd_ids"),
    )


def load_definition_request(path: Path) -> DefinitionRefreshRequest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"definition request JSON is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("definition request must be a JSON object")
    identity = _mapping(payload, "identity")
    workspace = _mapping(payload, "workspace")
    market_ordinal = payload.get("market_ordinal")
    if market_ordinal is not None and not isinstance(market_ordinal, int): raise RuntimeError("definition request market_ordinal must be an integer")
    request = DefinitionRefreshRequest(
        identity=DefinitionRefreshIdentity(
            mi_master_sha256=str(identity.get("mi_master_sha256") or ""),
            catalog_diff_hash=str(identity.get("catalog_diff_hash") or ""),
            run_id=str(identity.get("run_id") or ""),
        ),
        workspace=PublishWorkspace(
            candidate_root=_path(workspace, "candidate_root"),
            backup_root=_path(workspace, "backup_root"),
            journal_path=_path(workspace, "journal_path"),
        ),
        market_ordinal=market_ordinal,
        catalog_sync=_catalog_sync(payload.get("catalog_sync")),
        scope_plan=_scope_plan(payload.get("scope_plan")),
        candidate_build=_candidate_build(payload.get("candidate_build")),
        validation=payload.get("validation") if isinstance(payload.get("validation"), dict) else None,
        post_gate=payload.get("post_gate") if isinstance(payload.get("post_gate"), dict) else None,
        publish_plan=payload.get("publish_plan") if isinstance(payload.get("publish_plan"), dict) else None,
        cache_refresh=payload.get("cache_refresh") if isinstance(payload.get("cache_refresh"), dict) else None,
    )
    request.identity.validate()
    return request


def assert_complete_stage_contract(events: list[StageEvent]) -> None:
    completed = {event.stage for event in events if event.status == STAGE_COMPLETE}
    missing = tuple(stage for stage in STAGES if stage not in completed)
    if missing:
        raise MissingStageError(missing)
