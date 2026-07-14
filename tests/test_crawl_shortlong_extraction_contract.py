from __future__ import annotations

import hashlib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

PROTECTED_BLOB_SHA256 = {
    "api/Dockerfile": "20d969eae848996624cca8b0a8f7349aadb51e45c543bcde3e5211576da5d008",
    "pipeline/scripts/agent3/repository.py": "83af919e96eac24b372fa500c7fdb920b1e23e9abfb91352134bc98305fec858",
    "pipeline/scripts/agent3/run_source.py": "be4dcaf7cffb77cdcb0898970597a76d12349822ecdf3ff4aebd633aa794c376",
    "pipeline/scripts/agent3/strength_candidate_extractor.py": "67773b5947f4eb36b79c85edb421a9c1002d413450dbb001f842b91f2c8cb271",
    "pipeline/scripts/api/dynamic_market/strategic_runtime.py": "9c90906657dde4ef281b13fa8e3d347e328ee9abdd05458378ddcd29793ff221",
    "pipeline/scripts/api/main.py": "6da9103a735cb599a14a719e1e6729df15d163ec0481b9e4c97d369935715485",
    "pipeline/scripts/api/market_definition_display.py": "774291083195348bd17707e6a61b7853b3c4202ac91649788c5ec8cef0773d5b",
    "pipeline/scripts/api/openapi_docs.py": "0eb9077317ddc3d1bd470059e75b38321c7a4480c54fddd2d17e2b6b8032052a",
    "pipeline/scripts/api/routes/cause.py": "cdd25a31b1318d310726b73afb170c636a09a8ed657c2469604c091217f4eb80",
    "pipeline/scripts/api/routes/deep_analysis.py": "07dddb5cc0a578757cbe3000f021a2ba7a6661960eed55b5eec8f96321147130",
}


def test_extraction_preserves_protected_contract_blobs() -> None:
    actual = {
        path: hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest()
        for path in PROTECTED_BLOB_SHA256
    }

    assert actual == PROTECTED_BLOB_SHA256


def test_agent3_manifests_keep_revision_pin_assert_and_immutable_image() -> None:
    for relative_path in (
        "deploy/k8s/agent3/agent3-full-job.yaml",
        "deploy/k8s/agent3/agent3-refresh-cronjob.yaml",
    ):
        manifest = (REPO_ROOT / relative_path).read_text(encoding="utf-8")

        assert "AGENT3_WORKFLOW_REV" in manifest
        assert 'value: "5692"' in manifest
        assert "--expected-workflow-rev 5692" in manifest
        assert "AGENT3_MODE" not in manifest
        assert "@sha256:" in manifest


def test_agent3_source_keeps_pre_io_and_idempotency_gates() -> None:
    source = (
        REPO_ROOT / "pipeline/scripts/agent3/run_source.py"
    ).read_text(encoding="utf-8")

    assert "validate_execution_contract(" in source
    assert "expected_workflow_rev=args.expected_workflow_rev" in source
    assert "validate_source_coverage(" in source
    assert "evaluate_idempotency_gate(counts)" in source
    assert '"calls_unexplained"' in source
    assert "build_market_position_fallback(" in source
