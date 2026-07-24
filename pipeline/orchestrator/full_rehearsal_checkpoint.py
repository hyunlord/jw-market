"""Content-addressed, fail-closed s1 checkpoints for R-1 rehearsals."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from pipeline.orchestrator.full_rehearsal_checkpoint_census import (
    artifact_records,
    checkpoint_census,
    read_database_census,
)
from pipeline.orchestrator.full_rehearsal_checkpoint_contract import (
    CheckpointContractError,
    DatabaseCensus,
    build_checkpoint_identity,
    canonical_json,
    inventory_canonical_sha,
)

if TYPE_CHECKING:
    from pipeline.orchestrator.full_rehearsal import FullInputManifest


class S1CheckpointStore:
    """Publish immutable s1 artifacts and validate every read before reuse."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _checkpoint_dir(self, checkpoint_id: str) -> Path:
        if len(checkpoint_id) != 64 or any(char not in "0123456789abcdef" for char in checkpoint_id):
            raise CheckpointContractError("invalid checkpoint id")
        return self.root / checkpoint_id

    def publish(
        self,
        *,
        checkpoint_id: str,
        work_dir: Path,
        inventory_path: Path,
        input_manifest: FullInputManifest,
        database: DatabaseCensus,
        expected_sidecars: Iterable[object],
    ) -> dict[str, Any]:
        destination = self._checkpoint_dir(checkpoint_id)
        if destination.exists():
            raise CheckpointContractError(f"immutable checkpoint already exists: {checkpoint_id}")
        work = work_dir.resolve()
        census, source_artifacts = checkpoint_census(
            work=work,
            inventory_path=inventory_path,
            input_manifest=input_manifest,
            database=database,
            expected_sidecars=expected_sidecars,
        )

        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f".__tmp_{checkpoint_id}_{uuid.uuid4().hex}"
        s1 = temporary / "s1"
        try:
            for name in ("ubist", "iqvia-records", "iqvia-nsa"):
                shutil.copytree(work / name, s1 / name, copy_function=shutil.copy2)
            copied_artifacts = artifact_records(s1)
            if copied_artifacts != source_artifacts:
                raise CheckpointContractError("copied checkpoint artifact identity mismatch")
            temporary.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, destination)
            census.append(
                {
                    "check": "9-completion-publish",
                    "passed": True,
                    "detail": "immutable prefix promoted before completion marker",
                }
            )
            completion = {
                "artifacts": copied_artifacts,
                "census": census,
                "checkpoint_id": checkpoint_id,
                "input_inventory_canonical_sha": inventory_canonical_sha(inventory_path),
                "status": "complete",
            }
            marker_tmp = destination / ".__completion_tmp"
            marker_tmp.write_bytes(canonical_json(completion))
            os.replace(marker_tmp, destination / "completion.json")
            return completion
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            if destination.exists() and not (destination / "completion.json").exists():
                shutil.rmtree(destination, ignore_errors=True)
            raise

    def restore(self, *, checkpoint_id: str, work_dir: Path) -> dict[str, Any]:
        source = self._checkpoint_dir(checkpoint_id)
        completion_path = source / "completion.json"
        try:
            completion = json.loads(completion_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointContractError(f"checkpoint completion is missing or invalid: {exc}") from exc
        if completion.get("status") != "complete" or completion.get("checkpoint_id") != checkpoint_id:
            raise CheckpointContractError("checkpoint completion identity mismatch")
        s1 = source / "s1"
        try:
            actual = artifact_records(s1)
        except CheckpointContractError as exc:
            raise CheckpointContractError(
                f"checkpoint artifact identity mismatch: {exc}"
            ) from exc
        if actual != completion.get("artifacts"):
            raise CheckpointContractError("checkpoint artifact identity mismatch")
        target = work_dir.resolve()
        if target.exists():
            raise CheckpointContractError(f"restore work directory already exists: {target}")
        target.mkdir(parents=True)
        for name in ("ubist", "iqvia-records", "iqvia-nsa"):
            shutil.copytree(s1 / name, target / name, copy_function=shutil.copy2)
        return completion


__all__ = [
    "CheckpointContractError",
    "DatabaseCensus",
    "S1CheckpointStore",
    "build_checkpoint_identity",
    "read_database_census",
]
