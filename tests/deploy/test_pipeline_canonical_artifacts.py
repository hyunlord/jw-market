from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


EXPECTED_SHA256 = {
    "pipeline/scripts/deploy/brand_activity_307/row_topic_monthly_wrapper.py": (
        "01471fb2269fe6b47d2217cf4052fa83c8628cc47f7d4d7823cf513bc6fe7db0"
    ),
    "pipeline/scripts/agent3/ops/run_agent3_strategic_chunks.py": (
        "7fc704cae11af5af331b0ad3312150e5f650c5ed63b204dc62ea7a339bca1d54"
    ),
    "pipeline/scripts/agent3/ops/finalize_strategic_strength.py": (
        "31bbdff3a18875bbd088778425cd5e425fc741fb5addf6237b6fefbadf4a7594"
    ),
    "pipeline/scripts/agent3/ops/restore_revision_5692.sh": (
        "b68ef0337b48e1d23fd86f2cb8643fe3fee17a96ddcde3e616405872f317e8e3"
    ),
    "pipeline/scripts/agent3/ops/restore_revision_5692_units.tsv": (
        "a1f81df3e14acb1dab03229ba36fa988d8245094a71a28d76c121530f0dae617"
    ),
    "pipeline/scripts/etl/probes/brand_elements_stage0_check.py": (
        "4fe701b34e45fe4cedbbcc2d1fac05ea0444b9b7d0ef54779dd9225946cbb90b"
    ),
    "pipeline/scripts/etl/probes/f00x_corpus_parity.py": (
        "829b05595f19ab115b9d78fc54e9e5395a9fbe296421a68586ea9ea584681965"
    ),
    "pipeline/scripts/ai_analysis/ops/agent2_regen_orchestrator_vm_snapshot.py": (
        "951f4a62b37cfb5591dc851274336c4e2785dd48d8d24a222d11b53bce8fe2f5"
    ),
    "pipeline/scripts/ai_analysis/ops/test_agent2_regen_orchestrator_vm_snapshot.py": (
        "63cad60f23f52a5bcc27951955959246bf0b93b3e333775ed8d56a8e60bb732a"
    ),
}


def test_ingested_operational_artifacts_preserve_captured_bytes() -> None:
    for relative_path, expected in EXPECTED_SHA256.items():
        actual = hashlib.sha256((ROOT / relative_path).read_bytes()).hexdigest()
        assert actual == expected, relative_path


def test_restore_target_list_preserves_the_1593_unit_gate() -> None:
    path = ROOT / "pipeline/scripts/agent3/ops/restore_revision_5692_units.tsv"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1593
