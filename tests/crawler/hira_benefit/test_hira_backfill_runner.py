from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from pipeline.scripts.crawler.hira_benefit.backfill import build_backfill_manifest
from pipeline.scripts.crawler.hira_benefit.backfill_runner import (
    BackfillProgress,
    run_backfill_sequentially,
)
from pipeline.scripts.crawler.hira_benefit.contract import HiraWorkflowInput
from pipeline.scripts.crawler.hira_benefit.models import NoticeListItem


def _items(count: int) -> tuple[NoticeListItem, ...]:
    return tuple(
        NoticeListItem.create(
            source_notice_id=str(10_000 - index),
            title=f"notice {index}",
            notice_date=date(2026, 7, 25),
            source_url=(
                "https://www.hira.or.kr/rc/drug/insuadtcrtr/"
                f"bbsView.do?brdBltNo={10_000 - index}"
            ),
        )
        for index in range(count)
    )


def _base_config(tmp_path: Path, manifest_path: Path) -> HiraWorkflowInput:
    return HiraWorkflowInput(
        run_id="replaced-by-runner",
        state_root=str(tmp_path / "state"),
        repo_root=str(tmp_path),
        first_run_mode="backfill_all",
        manifest_path=str(manifest_path),
        manifest_sha256="a" * 64,
        chunk_index=0,
    )


def _result(config: HiraWorkflowInput) -> dict[str, object]:
    parsed = 500 if config.chunk_index != 2 else 77
    return {
        "run_id": config.run_id,
        "status": "complete",
        "stages": [
            {
                "stage": "collect_details",
                "status": "complete",
                "parsed_count": parsed,
                "partial_count": 2,
                "failed_count": 1,
            }
        ],
    }


def test_runner_executes_chunks_strictly_sequentially_and_accumulates_status(
    tmp_path: Path,
) -> None:
    manifest = build_backfill_manifest(_items(1_077), chunk_size=500)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    base_config = replace(
        _base_config(tmp_path, manifest_path),
        manifest_sha256=manifest.manifest_sha256,
    )
    active = 0
    maximum_active = 0
    observed: list[int] = []

    async def execute(config: HiraWorkflowInput, workflow_id: str) -> dict[str, object]:
        nonlocal active, maximum_active
        assert workflow_id.endswith(f"{config.chunk_index + 1:03d}-of-003")
        active += 1
        maximum_active = max(maximum_active, active)
        observed.append(config.chunk_index)
        await asyncio.sleep(0)
        active -= 1
        return _result(config)

    progress = asyncio.run(
        run_backfill_sequentially(
            manifest=manifest,
            base_config=base_config,
            progress_path=tmp_path / "progress.json",
            execute=execute,
        )
    )

    assert observed == [0, 1, 2]
    assert maximum_active == 1
    assert progress.completed_chunk_indexes == (0, 1, 2)
    assert progress.parsed_count == 1_077
    assert progress.partial_count == 6
    assert progress.failed_count == 3


def test_runner_resumes_from_first_incomplete_chunk_after_failure(tmp_path: Path) -> None:
    manifest = build_backfill_manifest(_items(1_077), chunk_size=500)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    base_config = replace(
        _base_config(tmp_path, manifest_path),
        manifest_sha256=manifest.manifest_sha256,
    )
    progress_path = tmp_path / "progress.json"
    first_observed: list[int] = []

    async def fail_second(
        config: HiraWorkflowInput,
        _workflow_id: str,
    ) -> dict[str, object]:
        first_observed.append(config.chunk_index)
        if config.chunk_index == 1:
            raise RuntimeError("injected chunk failure")
        return _result(config)

    with pytest.raises(RuntimeError, match="injected chunk failure"):
        asyncio.run(
            run_backfill_sequentially(
                manifest=manifest,
                base_config=base_config,
                progress_path=progress_path,
                execute=fail_second,
            )
        )

    persisted = BackfillProgress.from_json(progress_path.read_text(encoding="utf-8"))
    assert first_observed == [0, 1]
    assert persisted.completed_chunk_indexes == (0,)

    resumed: list[int] = []

    async def succeed(
        config: HiraWorkflowInput,
        _workflow_id: str,
    ) -> dict[str, object]:
        resumed.append(config.chunk_index)
        return _result(config)

    final = asyncio.run(
        run_backfill_sequentially(
            manifest=manifest,
            base_config=base_config,
            progress_path=progress_path,
            execute=succeed,
        )
    )

    assert resumed == [1, 2]
    assert final.completed_chunk_indexes == (0, 1, 2)


def test_runner_rejects_non_prefix_completion_receipts(tmp_path: Path) -> None:
    manifest = build_backfill_manifest(_items(1_077), chunk_size=500)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    base_config = replace(
        _base_config(tmp_path, manifest_path),
        manifest_sha256=manifest.manifest_sha256,
    )
    progress_path = tmp_path / "progress.json"
    progress_path.write_text(
        BackfillProgress(
            manifest_sha256=manifest.manifest_sha256,
            chunk_count=3,
            completed_chunk_indexes=(0, 2),
            chunk_results=(),
        ).to_json(),
        encoding="utf-8",
    )

    async def execute(
        _config: HiraWorkflowInput,
        _workflow_id: str,
    ) -> dict[str, object]:
        raise AssertionError("must fail before execution")

    with pytest.raises(RuntimeError, match="contiguous prefix"):
        asyncio.run(
            run_backfill_sequentially(
                manifest=manifest,
                base_config=base_config,
                progress_path=progress_path,
                execute=execute,
            )
        )
