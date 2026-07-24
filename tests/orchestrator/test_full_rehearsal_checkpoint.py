import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from pipeline.orchestrator.full_rehearsal_checkpoint import (
    CheckpointContractError,
    DatabaseCensus,
    S1CheckpointStore,
    build_checkpoint_identity,
)
from pipeline.orchestrator.full_rehearsal import FullInputManifest


def _write_parquet(path: Path, rows: list[dict[str, object]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)
    return len(rows)


def _work_tree(root: Path) -> tuple[Path, DatabaseCensus]:
    work = root / "work"
    ubist = work / "ubist"
    ubist_rows = _write_parquet(
        ubist / "year=2026" / "month=05" / "data.parquet",
        [{"value": 1}, {"value": 2}],
    )
    (ubist / "_manifest.json").write_text(
        json.dumps(
            {
                "partitions": [
                    {
                        "period_yyyymm": "2026-05",
                        "path": "year=2026/month=05/data.parquet",
                        "row_count": ubist_rows,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    record_rows = _write_parquet(
        work / "iqvia-records" / "nsa" / "2026-Q1.parquet",
        [{"period_label": "2026-Q1"}, {"period_label": "2026-Q1"}],
    )
    _write_parquet(
        work / "iqvia-nsa" / "2026-Q1.parquet",
        [{"period_label": "2026-Q1"}],
    )
    return work, DatabaseCensus(record_rows, {"2026-Q1": record_rows})


def _source_identity(tmp_path: Path) -> tuple[Path, FullInputManifest]:
    sources = tmp_path / "sources"
    ubist = sources / "ubist"
    iqvia = sources / "iqvia"
    ubist.mkdir(parents=True)
    iqvia.mkdir()
    ubist_file = ubist / "raw.xlsx"
    iqvia_file = iqvia / "raw.xlsx"
    master = sources / "master.xlsx"
    for path, content in (
        (ubist_file, b"ubist"),
        (iqvia_file, b"iqvia"),
        (master, b"master"),
    ):
        path.write_bytes(content)

    inventory = tmp_path / "input_inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "classification": "census",
                "missing": "fail",
                "objects": [
                    {
                        "bucket": bucket,
                        "key": path.name,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "size": path.stat().st_size,
                    }
                    for bucket, path in (
                        ("raw-ubist", ubist_file),
                        ("raw-iqvia", iqvia_file),
                        ("repository", master),
                    )
                ],
                "population": 3,
                "raw_buckets": {
                    "iqvia": "raw-iqvia",
                    "ubist": "raw-ubist",
                },
                "schema_version": 2,
            }
        ),
        encoding="utf-8",
    )
    return inventory, FullInputManifest(ubist, iqvia, master)


def _identity(tmp_path: Path) -> tuple[str, Path, FullInputManifest]:
    inventory, input_manifest = _source_identity(tmp_path)
    checkpoint_id = build_checkpoint_identity(
        inventory_path=inventory,
        image_digest=f"sha256:{'b' * 64}",
        git_commit="c" * 40,
        normalized_stage_args={"batch_size": 10_000},
        nonsecret_config={"R1_SOURCE_SUBPATH": "snapshot"},
        sidecar_manifest_sha=hashlib.sha256(b"sidecars").hexdigest(),
    )
    return checkpoint_id, inventory, input_manifest


def test_checkpoint_identity_changes_when_code_or_inputs_change(tmp_path: Path) -> None:
    checkpoint_id, inventory, _ = _identity(tmp_path)
    changed = build_checkpoint_identity(
        inventory_path=inventory,
        image_digest=f"sha256:{'b' * 64}",
        git_commit="d" * 40,
        normalized_stage_args={"batch_size": 10_000},
        nonsecret_config={"R1_SOURCE_SUBPATH": "snapshot"},
        sidecar_manifest_sha=hashlib.sha256(b"sidecars").hexdigest(),
    )

    assert checkpoint_id != changed


@pytest.mark.parametrize(
    ("image_digest", "git_commit", "sidecar_manifest_sha"),
    [
        ("sha256:" + "z" * 64, "b" * 40, "c" * 64),
        ("sha256:" + "a" * 64, "z" * 40, "c" * 64),
        ("sha256:" + "a" * 64, "b" * 40, "z" * 64),
    ],
)
def test_checkpoint_identity_rejects_non_hex_contract_values(
    tmp_path: Path,
    image_digest: str,
    git_commit: str,
    sidecar_manifest_sha: str,
) -> None:
    _, inventory, _ = _identity(tmp_path)

    with pytest.raises(CheckpointContractError):
        build_checkpoint_identity(
            inventory_path=inventory,
            image_digest=image_digest,
            git_commit=git_commit,
            normalized_stage_args={},
            nonsecret_config={},
            sidecar_manifest_sha=sidecar_manifest_sha,
        )


def test_checkpoint_publish_runs_nine_census_gates_and_restores(tmp_path: Path) -> None:
    checkpoint_id, inventory, input_manifest = _identity(tmp_path)
    work, database = _work_tree(tmp_path)
    store = S1CheckpointStore(tmp_path / "checkpoints")

    completion = store.publish(
        checkpoint_id=checkpoint_id,
        work_dir=work,
        inventory_path=inventory,
        input_manifest=input_manifest,
        database=database,
        expected_sidecars=(),
    )

    assert completion["status"] == "complete"
    assert len(completion["census"]) == 9
    assert all(item["passed"] for item in completion["census"])
    restored = tmp_path / "restored"
    store.restore(checkpoint_id=checkpoint_id, work_dir=restored)
    assert (restored / "ubist" / "_manifest.json").is_file()
    assert (restored / "iqvia-records" / "nsa" / "2026-Q1.parquet").is_file()
    assert (restored / "iqvia-nsa" / "2026-Q1.parquet").is_file()


def test_checkpoint_publish_fails_before_completion_on_db_count_mismatch(tmp_path: Path) -> None:
    checkpoint_id, inventory, input_manifest = _identity(tmp_path)
    work, _ = _work_tree(tmp_path)
    store = S1CheckpointStore(tmp_path / "checkpoints")

    with pytest.raises(CheckpointContractError, match="DB row count"):
        store.publish(
            checkpoint_id=checkpoint_id,
            work_dir=work,
            inventory_path=inventory,
            input_manifest=input_manifest,
            database=DatabaseCensus(1, {"2026-Q1": 1}),
            expected_sidecars=(),
        )

    assert not (tmp_path / "checkpoints" / checkpoint_id / "completion.json").exists()


def test_checkpoint_publish_rechecks_every_inventory_object(tmp_path: Path) -> None:
    checkpoint_id, inventory, input_manifest = _identity(tmp_path)
    work, database = _work_tree(tmp_path)
    store = S1CheckpointStore(tmp_path / "checkpoints")
    (input_manifest.ubist_source_dir / "raw.xlsx").write_bytes(b"changed")

    with pytest.raises(CheckpointContractError, match="input inventory changed"):
        store.publish(
            checkpoint_id=checkpoint_id,
            work_dir=work,
            inventory_path=inventory,
            input_manifest=input_manifest,
            database=database,
            expected_sidecars=(),
        )


def test_restore_rejects_tampered_parquet(tmp_path: Path) -> None:
    checkpoint_id, inventory, input_manifest = _identity(tmp_path)
    work, database = _work_tree(tmp_path)
    store = S1CheckpointStore(tmp_path / "checkpoints")
    store.publish(
        checkpoint_id=checkpoint_id,
        work_dir=work,
        inventory_path=inventory,
        input_manifest=input_manifest,
        database=database,
        expected_sidecars=(),
    )
    artifact = next((tmp_path / "checkpoints" / checkpoint_id / "s1").rglob("*.parquet"))
    artifact.write_bytes(b"tampered")

    with pytest.raises(CheckpointContractError, match="identity mismatch"):
        store.restore(checkpoint_id=checkpoint_id, work_dir=tmp_path / "restore")
