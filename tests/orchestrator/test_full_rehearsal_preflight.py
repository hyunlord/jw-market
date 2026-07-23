import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

from pipeline.orchestrator.full_rehearsal import (
    FullRehearsalConfig,
    FullInputManifest,
    RehearsalStep,
    UbistParquetSidecar,
)
from pipeline.orchestrator import full_rehearsal_preflight as preflight
from pipeline.orchestrator import cli

def test_full_rehearsal_preflight_is_an_independent_module():
    assert (
        importlib.util.find_spec(
            "pipeline.orchestrator.full_rehearsal_preflight"
        )
        is not None
    )


def test_sidecar_overlap_is_rejected_before_s1(tmp_path: Path):
    inputs = FullInputManifest(
        tmp_path,
        tmp_path,
        tmp_path / "master.xlsx",
        (
            UbistParquetSidecar(
                tmp_path / "data.parquet",
                Path("year=2026/month=05/data.parquet"),
                "a" * 64,
            ),
        ),
    )
    plan = (
        RehearsalStep(
            "load_ubist",
            ("python", "-m", "pipeline.etl.run", "--stage", "s1"),
        ),
    )

    finding = preflight.check_sidecar_exclusions(inputs, plan)

    assert not finding.passed
    assert "2026-05" in finding.detail


def test_missing_iqvia_environment_is_rejected_without_values():
    environment = {key: "secret-value" for key in preflight.REQUIRED_ENV_KEYS}
    environment.pop("DB_PASSWORD")

    finding = preflight.check_required_environment(environment)

    assert not finding.passed
    assert "DB_PASSWORD" in finding.detail
    assert "secret-value" not in finding.detail


def test_nfd_source_path_is_rejected(tmp_path: Path):
    nfd = tmp_path / "UBIST 2026.05" / "의원.xlsx"
    nfd.parent.mkdir()
    nfd.write_bytes(b"xlsx")
    iqvia = tmp_path / "iqvia"
    iqvia.mkdir()
    (iqvia / "data.csv").write_text("period,value\n2026-05,1\n")
    master = tmp_path / "master.xlsx"
    master.write_bytes(b"xlsx")
    inputs = FullInputManifest(nfd.parent, iqvia, master)

    finding = preflight.check_unicode_paths(inputs)

    assert not finding.passed
    assert "NFC" in finding.detail


def test_missing_s2_seed_is_rejected(tmp_path: Path):
    (tmp_path / "data" / "cache").mkdir(parents=True)
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "molecule_v4_worklist.csv").write_text("brand\n")

    finding = preflight.check_required_assets(tmp_path)

    assert not finding.passed
    assert "prototype_11_step" in finding.detail


def test_broken_durable_logging_contract_is_rejected():
    manifest = """
spec:
  ttlSecondsAfterFinished: 60
  template:
    spec:
      containers:
        - args:
            - python -m pipeline.orchestrator rehearse-full
      volumes:
        - name: work
          emptyDir: {}
"""

    finding = preflight.check_job_contract(manifest)

    assert not finding.passed
    assert "durable tee" in finding.detail
    assert "evidence PVC" in finding.detail
    assert "TTL" in finding.detail


def test_cli_exposes_independent_preflight_command(tmp_path: Path):
    args = cli._build_parser().parse_args(
        [
            "preflight-full",
            "--input-manifest",
            str(tmp_path / "input_manifest.json"),
            "--input-inventory",
            str(tmp_path / "input_inventory.json"),
            "--target-db",
            "jw_mart_rehearsal_test",
            "--cache-db",
            "jw_mart_s6_rehearsal_test",
            "--source-db",
            "jw_mart_source",
            "--work-dir",
            str(tmp_path / "work"),
            "--evidence-dir",
            str(tmp_path / "evidence"),
            "--job-manifest",
            str(tmp_path / "job.yaml"),
        ]
    )

    assert args.command == "preflight-full"


def test_canonical_job_runs_preflight_before_rehearsal():
    manifest_path = (
        Path(__file__).resolve().parents[2]
        / "deploy/k8s/orchestrator/pipeline-orchestrator-full-rehearsal-job.yaml"
    )

    finding = preflight.check_job_contract(manifest_path.read_text())

    assert finding.passed, finding.detail


def test_normal_preflight_completes_all_ten_checks(tmp_path: Path, monkeypatch):
    inputs_root = tmp_path / "materialized"
    ubist = inputs_root / "ubist"
    iqvia = inputs_root / "iqvia"
    master_dir = inputs_root / "mi-master"
    ubist.mkdir(parents=True)
    iqvia.mkdir()
    master_dir.mkdir()

    def write_xlsx(path: Path) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("xl/workbook.xml", "<workbook/>")

    ubist_file = ubist / "UBIST_202605.xlsx"
    iqvia_file = iqvia / "IQVIA_2026Q1.csv"
    master = master_dir / "MI_Master.xlsx"
    write_xlsx(ubist_file)
    iqvia_file.write_text("period,value\n2026Q1,1\n")
    write_xlsx(master)
    manifest = inputs_root / "input_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ubist_source_dir": str(ubist),
                "iqvia_source_dir": str(iqvia),
                "mi_master": str(master),
            }
        )
    )
    objects = [
        {
            "bucket": bucket,
            "key": key,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }
        for bucket, key, path in (
            ("raw-ubist", ubist_file.name, ubist_file),
            ("raw-iqvia", iqvia_file.name, iqvia_file),
            ("repository", master.name, master),
        )
    ]
    inventory = inputs_root / "input_inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "classification": "census",
                "population": len(objects),
                "objects": objects,
            }
        )
    )
    repo_root = tmp_path / "repo"
    for asset in preflight.REQUIRED_ASSETS:
        target = repo_root / asset
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("seed\n")
    environment = {key: "present" for key in preflight.REQUIRED_ENV_KEYS}
    environment["R1_RUN_ID"] = "r1-20260723"
    environment["R1_SOURCE_SUBPATH"] = "snapshot-20260723"
    environment["R1_IMAGE_DIGEST"] = f"sha256:{'a' * 64}"
    environment["R1_GIT_COMMIT"] = "b" * 40
    monkeypatch.setattr(
        preflight,
        "check_database_readiness",
        lambda _environment, _config: (True, "read-only probe passed"),
    )
    job_manifest = (
        Path(__file__).resolve().parents[2]
        / "deploy/k8s/orchestrator/pipeline-orchestrator-full-rehearsal-job.yaml"
    )
    (tmp_path / "work").mkdir()
    (tmp_path / "evidence").mkdir()

    findings = preflight.run_preflight(
        preflight.PreflightRequest(
            rehearsal=FullRehearsalConfig(
                manifest,
                "jw_mart_rehearsal_test",
                "jw_mart_s6_rehearsal_test",
                "jw_mart_source",
                tmp_path / "work" / "rehearsal",
            ),
            inventory_path=inventory,
            job_manifest_path=job_manifest,
            evidence_dir=tmp_path / "evidence",
            repo_root=repo_root,
            environment=environment,
        )
    )

    assert len(findings) == 10
    assert all(finding.passed for finding in findings), findings
