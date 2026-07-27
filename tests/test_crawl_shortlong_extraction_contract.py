from __future__ import annotations

import hashlib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

PROTECTED_BLOB_SHA256 = {
    # Repinned 2026-07-28: the API image now includes the contract/domain
    # packages proven reachable by tests/api/test_backend_dockerfile_contract.py.
    "api/Dockerfile": "e01912fa3d40de831b4e74a7f3ff98071efe09f9a095ca680ec4d2b0c37d18d1",
    "pipeline/scripts/agent3/repository.py": "83af919e96eac24b372fa500c7fdb920b1e23e9abfb91352134bc98305fec858",
    "pipeline/scripts/agent3/run_source.py": "be4dcaf7cffb77cdcb0898970597a76d12349822ecdf3ff4aebd633aa794c376",
    "pipeline/scripts/agent3/strength_candidate_extractor.py": "67773b5947f4eb36b79c85edb421a9c1002d413450dbb001f842b91f2c8cb271",
    # Repinned 2026-07-25: dimension aliases and brand normalization now come
    # from shared contract/domain modules; the runtime filtering behavior is
    # byte-locked against the previous implementation by the registry contract.
    "pipeline/scripts/api/dynamic_market/strategic_runtime.py": "366a9a706bc06d6cbaa31284f9be2eb42ec9ea0a9013675047fa85e9dab4e764",
    "pipeline/scripts/api/main.py": "5ada140bfdf2213a23dfbf885be70257cd185518f714876cf314933c9c401272",
    "pipeline/scripts/api/market_definition_display.py": "36cd88d6b0ada9718ddf959f2b8172c0443fb52843ebca64ca8340e1b6bbd269",
    # Repinned 2026-07-22: brand-activity topics company_name openapi description updated to
    # reflect the manufacturer (제조사, IQVIA MFR NAME KOR) source switch. Forward, description-
    # only edit; does not revert the crawl-shortlong extraction lineage the blob set guards.
    "pipeline/scripts/api/openapi_docs.py": "995e57a772e159339d52d68199c1e1f570d51abc1de872f36cfc425ba2cdc465",
    "pipeline/scripts/api/routes/cause.py": "d5d2ba035d49bc8ac9f5951becc2a38dfe7e7217d8c75e8d2c64945371a70edb",
    "pipeline/scripts/api/routes/deep_analysis.py": "4dc23b1e6dcd6afea9a8937288f34b3ba230504c2bad7417edbe145bebbc82b7",
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
