import hashlib
import importlib.util
import json
import zipfile
from datetime import date
from pathlib import Path

import openpyxl

from pipeline.orchestrator.full_rehearsal import (
    FullRehearsalConfig,
    FullInputManifest,
    RehearsalStep,
    UbistParquetSidecar,
)
from pipeline.orchestrator import full_rehearsal_preflight as preflight
from pipeline.orchestrator import cli


NSA_HEADERS = [
    "DATA PERIOD",
    "AUDIT CODE",
    "AUDIT DESC",
    "MFR CODE",
    "MFR NAME",
    "PRODUCT NAME",
    "PACK DESC",
    "Values LC",
    "Units",
    "Counting Units",
    "Dosage Units",
    "Price",
]


def _write_nsa_workbook(
    path: Path,
    *,
    headers: list[str] | None = None,
    metric_value: date | int | str = 1234,
) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "NSA"
    sheet.append(headers or NSA_HEADERS)
    sheet.append(
        [
            "2026-03-01",
            "KCPA",
            "Clinic",
            "MFR",
            "Manufacturer",
            "Drug",
            "10MG",
            metric_value,
            metric_value,
            metric_value,
            metric_value,
            1,
        ]
    )
    workbook.save(path)


def _bounded_inputs(tmp_path: Path, nsa_path: Path) -> FullInputManifest:
    ubist = tmp_path / "ubist"
    ubist.mkdir()
    (ubist / "raw.csv").write_text("period,value\n2026-05,1\n")
    master = tmp_path / "master.csv"
    master.write_text("market,value\nsample,1\n")
    return FullInputManifest(ubist, nsa_path.parents[1], master)


def test_bounded_inputs_rejects_missing_canonical_nsa_metric_header(
    tmp_path: Path,
) -> None:
    nsa = tmp_path / "iqvia" / "NSA" / "KOR_NSA_Jun-25-2026.xlsx"
    nsa.parent.mkdir(parents=True)
    _write_nsa_workbook(
        nsa,
        headers=[header for header in NSA_HEADERS if header != "Values LC"],
    )

    finding = preflight.check_bounded_inputs(_bounded_inputs(tmp_path, nsa))

    assert not finding.passed
    assert "Values LC" in finding.detail


def test_bounded_inputs_rejects_nonnumeric_canonical_nsa_metric_sample(
    tmp_path: Path,
) -> None:
    nsa = tmp_path / "iqvia" / "NSA" / "KOR_NSA_Jun-25-2026.xlsx"
    nsa.parent.mkdir(parents=True)
    _write_nsa_workbook(nsa, metric_value="not-a-number")

    finding = preflight.check_bounded_inputs(_bounded_inputs(tmp_path, nsa))

    assert not finding.passed
    assert "non-numeric IQVIA NSA metric" in finding.detail


def test_bounded_inputs_accepts_comma_string_canonical_nsa_metric_sample(
    tmp_path: Path,
) -> None:
    nsa = tmp_path / "iqvia" / "NSA" / "KOR_NSA_Jun-25-2026.xlsx"
    nsa.parent.mkdir(parents=True)
    _write_nsa_workbook(nsa, metric_value="1,234")

    finding = preflight.check_bounded_inputs(_bounded_inputs(tmp_path, nsa))

    assert finding.passed


def test_bounded_inputs_rejects_non_scalar_canonical_nsa_metric_sample(
    tmp_path: Path,
) -> None:
    nsa = tmp_path / "iqvia" / "NSA" / "KOR_NSA_Jun-25-2026.xlsx"
    nsa.parent.mkdir(parents=True)
    _write_nsa_workbook(nsa, metric_value=date(2026, 1, 1))

    finding = preflight.check_bounded_inputs(_bounded_inputs(tmp_path, nsa))

    assert not finding.passed
    assert "non-numeric IQVIA NSA metric" in finding.detail


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


def test_iqvia_role_gate_rejects_mixed_or_ambiguous_nsa_inputs(tmp_path: Path):
    ubist = tmp_path / "ubist"
    iqvia = tmp_path / "iqvia"
    nsa = iqvia / "NSA"
    chso = iqvia / "CHSO"
    ubist.mkdir()
    nsa.mkdir(parents=True)
    chso.mkdir()
    (ubist / "raw.xlsx").write_bytes(b"xlsx")
    (nsa / "KOR_NSA_Jun-25-2026.xlsx").write_bytes(b"nsa")
    (nsa / "legacy.xlsx").write_bytes(b"legacy")
    (chso / "CHSO_KOR.xlsx").write_bytes(b"chso")
    master = tmp_path / "master.xlsx"
    master.write_bytes(b"xlsx")

    finding = preflight.check_iqvia_source_roles(
        FullInputManifest(ubist, iqvia, master)
    )

    assert not finding.passed
    assert "exactly one NSA" in finding.detail


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


def test_normal_preflight_completes_all_eleven_checks(tmp_path: Path, monkeypatch):
    inputs_root = tmp_path / "materialized"
    ubist = inputs_root / "ubist"
    iqvia = inputs_root / "iqvia"
    master_dir = inputs_root / "mi-master"
    ubist.mkdir(parents=True)
    (iqvia / "NSA").mkdir(parents=True)
    master_dir.mkdir()

    def write_xlsx(path: Path) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("xl/workbook.xml", "<workbook/>")

    ubist_file = ubist / "UBIST_202605.xlsx"
    iqvia_file = iqvia / "NSA" / "KOR_NSA_Jun-25-2026.xlsx"
    master = master_dir / "MI_Master.xlsx"
    write_xlsx(ubist_file)
    _write_nsa_workbook(iqvia_file)
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
            ("raw-iqvia", f"NSA/{iqvia_file.name}", iqvia_file),
            ("repository", master.name, master),
        )
    ]
    inventory = inputs_root / "input_inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "classification": "census",
                "raw_buckets": {
                    "iqvia": "raw-iqvia",
                    "ubist": "raw-ubist",
                },
                "population": len(objects),
                "objects": objects,
                "schema_version": 2,
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

    assert len(findings) == 11
    assert all(finding.passed for finding in findings), findings


def test_inventory_uses_declared_raw_bucket_roles(tmp_path: Path):
    ubist = tmp_path / "ubist"
    iqvia = tmp_path / "iqvia"
    ubist.mkdir()
    iqvia.mkdir()
    ubist_file = ubist / "UBIST.xlsx"
    iqvia_file = iqvia / "IQVIA.csv"
    ubist_file.write_bytes(b"ubist")
    iqvia_file.write_bytes(b"iqvia")
    master = tmp_path / "master.xlsx"
    master.write_bytes(b"master")
    inputs = FullInputManifest(ubist, iqvia, master)
    objects = [
        {
            "bucket": bucket,
            "key": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }
        for bucket, path in (
            ("jw-market-raw-ubist", ubist_file),
            ("jw-market-raw-iqvia", iqvia_file),
            ("repository", master),
        )
    ]
    inventory = tmp_path / "input_inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "classification": "census",
                "raw_buckets": {
                    "iqvia": "jw-market-raw-iqvia",
                    "ubist": "jw-market-raw-ubist",
                },
                "population": len(objects),
                "objects": objects,
                "schema_version": 2,
            }
        )
    )

    finding = preflight.check_inventory(inputs, inventory)

    assert finding.passed, finding.detail


def test_inventory_rejects_missing_raw_bucket_roles(tmp_path: Path):
    inventory = tmp_path / "input_inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "classification": "census",
                "population": 0,
                "objects": [],
                "schema_version": 2,
            }
        )
    )
    inputs = FullInputManifest(
        tmp_path / "ubist",
        tmp_path / "iqvia",
        tmp_path / "master.xlsx",
    )

    finding = preflight.check_inventory(inputs, inventory)

    assert not finding.passed
    assert "raw bucket roles" in finding.detail
