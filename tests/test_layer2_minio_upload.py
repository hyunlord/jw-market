from __future__ import annotations

from argparse import Namespace
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "pipeline" / "scripts" / "etl"))

import layer2_enrich  # noqa: E402


def test_upload_enriched_to_minio_noops_for_local_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(layer2_enrich, "is_minio_backend", lambda: False)
    monkeypatch.setattr(
        layer2_enrich,
        "upload_local_to_minio",
        lambda *args, **kwargs: pytest.fail("upload should not run for local backend"),
    )

    layer2_enrich.upload_enriched_to_minio(tmp_path)


def test_upload_enriched_to_minio_uploads_when_minio_backend(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("MINIO_BUCKET_ENRICHED", "custom-enriched")
    monkeypatch.setattr(layer2_enrich, "is_minio_backend", lambda: True)
    monkeypatch.setattr(
        layer2_enrich,
        "upload_local_to_minio",
        lambda **kwargs: calls.append(kwargs) or 3,
    )

    layer2_enrich.upload_enriched_to_minio(tmp_path)

    assert calls == [
        {
            "local_dir": tmp_path,
            "bucket": "custom-enriched",
            "prefix": "",
        }
    ]


def test_upload_enriched_to_minio_swallows_upload_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(layer2_enrich, "is_minio_backend", lambda: True)

    def fail_upload(**_kwargs):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(layer2_enrich, "upload_local_to_minio", fail_upload)

    layer2_enrich.upload_enriched_to_minio(tmp_path)


def test_main_uploads_after_successful_non_dry_run(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        layer2_enrich,
        "parse_args",
        lambda: Namespace(
            audit_dir=str(tmp_path / "audit"),
            output_dir=str(tmp_path / "enriched"),
            truncate=False,
            ml="ml_001",
            dry_run=False,
        ),
    )
    monkeypatch.setattr(
        layer2_enrich,
        "enrich_ml",
        lambda ml_id, **_kwargs: layer2_enrich.EnrichResult(
            ml_id=ml_id,
            rows=1,
            matched_products=1,
            total_products=1,
            sources={"ubist": 1},
            skipped_sources=[],
        ),
    )
    monkeypatch.setattr(layer2_enrich, "write_loading_csv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        layer2_enrich,
        "upload_enriched_to_minio",
        lambda local_dir: calls.append(local_dir),
    )

    assert layer2_enrich.main() == 0
    assert calls == [tmp_path / "enriched"]


def test_main_skips_upload_for_dry_run(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        layer2_enrich,
        "parse_args",
        lambda: Namespace(
            audit_dir=str(tmp_path / "audit"),
            output_dir=str(tmp_path / "enriched"),
            truncate=False,
            ml="ml_001",
            dry_run=True,
        ),
    )
    monkeypatch.setattr(
        layer2_enrich,
        "enrich_ml",
        lambda ml_id, **_kwargs: layer2_enrich.EnrichResult(
            ml_id=ml_id,
            rows=0,
            matched_products=0,
            total_products=1,
            sources={},
            skipped_sources=[],
        ),
    )
    monkeypatch.setattr(layer2_enrich, "write_loading_csv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        layer2_enrich,
        "upload_enriched_to_minio",
        lambda local_dir: calls.append(local_dir),
    )

    assert layer2_enrich.main() == 0
    assert calls == []
