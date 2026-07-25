from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .backfill import BackfillManifest
from .contract import HiraWorkflowInput

ChunkExecutor = Callable[
    [HiraWorkflowInput, str],
    Awaitable[dict[str, object]],
]


@dataclass(frozen=True, slots=True)
class ChunkResult:
    chunk_index: int
    workflow_id: str
    run_id: str
    parsed_count: int
    partial_count: int
    failed_count: int


@dataclass(frozen=True, slots=True)
class BackfillProgress:
    manifest_sha256: str
    chunk_count: int
    completed_chunk_indexes: tuple[int, ...]
    chunk_results: tuple[ChunkResult, ...]

    @property
    def parsed_count(self) -> int:
        return sum(item.parsed_count for item in self.chunk_results)

    @property
    def partial_count(self) -> int:
        return sum(item.partial_count for item in self.chunk_results)

    @property
    def failed_count(self) -> int:
        return sum(item.failed_count for item in self.chunk_results)

    def to_json(self) -> str:
        return json.dumps(
            {
                "manifest_sha256": self.manifest_sha256,
                "chunk_count": self.chunk_count,
                "completed_chunk_indexes": list(self.completed_chunk_indexes),
                "chunk_results": [asdict(item) for item in self.chunk_results],
                "cumulative": {
                    "parsed_count": self.parsed_count,
                    "partial_count": self.partial_count,
                    "failed_count": self.failed_count,
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> BackfillProgress:
        payload = json.loads(value)
        return cls(
            manifest_sha256=str(payload["manifest_sha256"]),
            chunk_count=int(payload["chunk_count"]),
            completed_chunk_indexes=tuple(
                int(value) for value in payload["completed_chunk_indexes"]
            ),
            chunk_results=tuple(
                ChunkResult(
                    chunk_index=int(item["chunk_index"]),
                    workflow_id=str(item["workflow_id"]),
                    run_id=str(item["run_id"]),
                    parsed_count=int(item["parsed_count"]),
                    partial_count=int(item["partial_count"]),
                    failed_count=int(item["failed_count"]),
                )
                for item in payload["chunk_results"]
            ),
        )


def _write_progress(path: Path, progress: BackfillProgress) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(progress.to_json(), encoding="utf-8")
    temp.replace(path)


def _empty_progress(manifest: BackfillManifest) -> BackfillProgress:
    return BackfillProgress(
        manifest_sha256=manifest.manifest_sha256,
        chunk_count=manifest.chunk_count,
        completed_chunk_indexes=(),
        chunk_results=(),
    )


def _load_progress(
    path: Path,
    *,
    manifest: BackfillManifest,
) -> BackfillProgress:
    progress = (
        BackfillProgress.from_json(path.read_text(encoding="utf-8"))
        if path.is_file()
        else _empty_progress(manifest)
    )
    if (
        progress.manifest_sha256 != manifest.manifest_sha256
        or progress.chunk_count != manifest.chunk_count
    ):
        raise RuntimeError("backfill progress does not match the manifest")
    completed = progress.completed_chunk_indexes
    if completed != tuple(range(len(completed))):
        raise RuntimeError("completed chunks must form a contiguous prefix")
    if tuple(item.chunk_index for item in progress.chunk_results) != completed:
        raise RuntimeError("chunk results do not match completed chunk indexes")
    return progress


def _chunk_result(
    *,
    chunk_index: int,
    workflow_id: str,
    run_id: str,
    result: dict[str, object],
) -> ChunkResult:
    if result.get("status") != "complete":
        raise RuntimeError(f"chunk {chunk_index} workflow did not complete")
    stages = result.get("stages")
    if not isinstance(stages, list):
        raise RuntimeError(f"chunk {chunk_index} result has no stage receipts")
    collect = [
        item
        for item in stages
        if isinstance(item, dict) and item.get("stage") == "collect_details"
    ]
    if len(collect) != 1 or collect[0].get("status") != "complete":
        raise RuntimeError(f"chunk {chunk_index} collect receipt is not complete")
    return ChunkResult(
        chunk_index=chunk_index,
        workflow_id=workflow_id,
        run_id=run_id,
        parsed_count=int(collect[0]["parsed_count"]),
        partial_count=int(collect[0]["partial_count"]),
        failed_count=int(collect[0]["failed_count"]),
    )


async def run_backfill_sequentially(
    *,
    manifest: BackfillManifest,
    base_config: HiraWorkflowInput,
    progress_path: Path,
    execute: ChunkExecutor,
) -> BackfillProgress:
    """Execute one chunk at a time and persist a resumable prefix receipt."""

    if base_config.manifest_sha256 != manifest.manifest_sha256:
        raise RuntimeError("workflow input does not match the manifest")
    progress = _load_progress(progress_path, manifest=manifest)
    for chunk in manifest.chunks[len(progress.completed_chunk_indexes) :]:
        run_id = (
            f"hira-backfill-{manifest.manifest_sha256[:12]}-"
            f"{chunk.index + 1:03d}-of-{manifest.chunk_count:03d}"
        )
        config = replace(
            base_config,
            run_id=run_id,
            chunk_index=chunk.index,
        )
        result = await execute(config, run_id)
        chunk_result = _chunk_result(
            chunk_index=chunk.index,
            workflow_id=run_id,
            run_id=run_id,
            result=result,
        )
        progress = BackfillProgress(
            manifest_sha256=progress.manifest_sha256,
            chunk_count=progress.chunk_count,
            completed_chunk_indexes=(
                *progress.completed_chunk_indexes,
                chunk.index,
            ),
            chunk_results=(*progress.chunk_results, chunk_result),
        )
        _write_progress(progress_path, progress)
    return progress
