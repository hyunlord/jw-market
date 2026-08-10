"""Contract v2 manifest parsing — fail-closed on every required field."""
from __future__ import annotations

import json

import pytest

from pipeline.scripts.ingest_hook.contract import ContractError, load_manifest, parse_manifest_bytes
from ingest_fixtures import write_submission


def _valid() -> dict:
    return {
        "contract_version": "v2",
        "epoch": "2026-07",
        "category": "ubist",
        "complete": True,
        "files": [{"path": "ubist/data.csv", "sha256": "a" * 64, "rows": 3}],
    }


def _parse(data: dict):
    return parse_manifest_bytes(json.dumps(data).encode("utf-8"))


def test_valid_manifest_parses(bucket):
    manifest = load_manifest(write_submission(bucket))
    assert manifest.epoch == "2026-07"
    assert manifest.category == "ubist"
    assert manifest.complete is True
    assert len(manifest.files) == 1
    assert len(manifest.manifest_sha) == 64


@pytest.mark.parametrize("missing", ["contract_version", "category", "complete", "files"])
def test_missing_required_field_fails(missing):
    data = _valid()
    del data[missing]
    with pytest.raises(ContractError, match=missing):
        _parse(data)


@pytest.mark.parametrize(
    "mutation, match",
    [
        ({"contract_version": "v1"}, "contract_version"),
        ({"epoch": "202607"}, "epoch"),
        ({"epoch": "2026-13"}, "epoch"),
        ({"complete": "yes"}, "complete"),
        ({"files": []}, "files"),
        ({"files": [{"path": "x.csv", "sha256": "zz"}]}, "sha256"),
        ({"files": [{"path": "x.csv", "sha256": "a" * 64, "rows": -1}]}, "rows"),
    ],
)
def test_invalid_values_fail(mutation, match):
    data = {**_valid(), **mutation}
    with pytest.raises(ContractError, match=match):
        _parse(data)


def test_quarterly_epoch_accepted():
    manifest = _parse({**_valid(), "epoch": "2026-Q2"})
    assert manifest.epoch == "2026-Q2"


def test_missing_epoch_is_accepted_as_unknown():
    data = _valid()
    del data["epoch"]
    assert _parse(data).epoch == "unknown"


@pytest.mark.parametrize("epoch", ["2026-W01", "2026-W27", "2026-W53"])
def test_weekly_epoch_accepted(epoch):
    assert _parse({**_valid(), "epoch": epoch}).epoch == epoch


@pytest.mark.parametrize("epoch", ["2026-W00", "2026-W54", "2026-W7"])
def test_weekly_epoch_out_of_range_fails(epoch):
    with pytest.raises(ContractError, match="epoch"):
        _parse({**_valid(), "epoch": epoch})


def test_uploaded_by_parsed_and_optional():
    assert _parse({**_valid(), "uploaded_by": "user@jw.example"}).uploaded_by == "user@jw.example"
    # v2.1: absence or emptiness must NEVER fail a submission
    assert _parse(_valid()).uploaded_by is None
    assert _parse({**_valid(), "uploaded_by": "  "}).uploaded_by is None


def test_not_json_fails():
    with pytest.raises(ContractError):
        parse_manifest_bytes(b"\x00\x01 not json")
