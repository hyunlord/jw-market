from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pipeline.etl.lib.storage import MI_MASTER_FILE_NAME
from pipeline.orchestrator.full_rehearsal import load_input_manifest
from pipeline.orchestrator.full_rehearsal_inputs import (
    InputMaterializationError,
    materialize_full_inputs,
)


class FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.objects if key.startswith(prefix))

    def read(self, key: str) -> bytes:
        return self.objects[key]


def test_materialize_full_inputs_writes_manifest_and_sha_inventory(tmp_path: Path) -> None:
    buckets = {
        "raw-ubist": FakeS3({"2026/UBIST_202605.xlsx": b"ubist", "README.txt": b"skip"}),
        "raw-iqvia": FakeS3({"nsa/2026Q2.csv": b"iqvia"}),
        "raw-master": FakeS3({f"master/{MI_MASTER_FILE_NAME}": b"master"}),
    }

    manifest_path = materialize_full_inputs(
        output_root=tmp_path / "inputs",
        ubist_bucket="raw-ubist",
        iqvia_bucket="raw-iqvia",
        mi_master_bucket="raw-master",
        client_factory=lambda bucket: buckets[bucket],
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert load_input_manifest(manifest_path).mi_master.read_bytes() == b"master"

    inventory = json.loads((manifest_path.parent / "input_inventory.json").read_text(encoding="utf-8"))
    assert inventory["classification"] == "census"
    assert inventory["objects"] == [
        {
            "bucket": "raw-iqvia",
            "key": "nsa/2026Q2.csv",
            "sha256": hashlib.sha256(b"iqvia").hexdigest(),
            "size": 5,
        },
        {
            "bucket": "raw-master",
            "key": f"master/{MI_MASTER_FILE_NAME}",
            "sha256": hashlib.sha256(b"master").hexdigest(),
            "size": 6,
        },
        {
            "bucket": "raw-ubist",
            "key": "2026/UBIST_202605.xlsx",
            "sha256": hashlib.sha256(b"ubist").hexdigest(),
            "size": 5,
        },
    ]


def test_materialize_full_inputs_fails_closed_on_missing_source_population(tmp_path: Path) -> None:
    buckets = {
        "raw-ubist": FakeS3({}),
        "raw-iqvia": FakeS3({"nsa/2026Q2.csv": b"iqvia"}),
        "raw-master": FakeS3({f"master/{MI_MASTER_FILE_NAME}": b"master"}),
    }

    with pytest.raises(InputMaterializationError, match="UBIST.*no supported objects"):
        materialize_full_inputs(
            output_root=tmp_path / "inputs",
            ubist_bucket="raw-ubist",
            iqvia_bucket="raw-iqvia",
            mi_master_bucket="raw-master",
            client_factory=lambda bucket: buckets[bucket],
        )


def test_materialize_full_inputs_refuses_existing_output_root(tmp_path: Path) -> None:
    output_root = tmp_path / "inputs"
    output_root.mkdir()

    with pytest.raises(InputMaterializationError, match="already exists"):
        materialize_full_inputs(
            output_root=output_root,
            ubist_bucket="raw-ubist",
            iqvia_bucket="raw-iqvia",
            mi_master_bucket="raw-master",
            client_factory=lambda _bucket: FakeS3({}),
        )


def test_materialize_full_inputs_rejects_object_key_escape(tmp_path: Path) -> None:
    buckets = {
        "raw-ubist": FakeS3({"../UBIST_202605.xlsx": b"escape"}),
        "raw-iqvia": FakeS3({"nsa/2026Q2.csv": b"iqvia"}),
        "raw-master": FakeS3({f"master/{MI_MASTER_FILE_NAME}": b"master"}),
    }

    with pytest.raises(InputMaterializationError, match="escapes"):
        materialize_full_inputs(
            output_root=tmp_path / "inputs",
            ubist_bucket="raw-ubist",
            iqvia_bucket="raw-iqvia",
            mi_master_bucket="raw-master",
            client_factory=lambda bucket: buckets[bucket],
        )
