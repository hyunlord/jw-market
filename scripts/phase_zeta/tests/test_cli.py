import json
import subprocess
import sys


def test_cli_single_brand(tmp_path, config_path):
    out = tmp_path / "out.json"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/phase_zeta/build_brand_bundle.py",
            "--brand",
            "리바로",
            "--snapshot-at",
            "2026-05-24T08:00:00+09:00",
            "--config",
            str(config_path),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    bundle = json.loads(out.read_text(encoding="utf-8"))
    assert bundle["bundle_meta"]["brand"] == "리바로"


def test_cli_batch_brands(tmp_path, config_path):
    out_dir = tmp_path / "bundles"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/phase_zeta/build_brand_bundle.py",
            "--brands-from",
            "scripts/phase_zeta/configs/pilot_brands.txt",
            "--snapshot-at",
            "2026-05-24T08:00:00+09:00",
            "--config",
            str(config_path),
            "--out-dir",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert len(list(out_dir.glob("*.json"))) == 5


def test_cli_narrative(tmp_path, config_path):
    out = tmp_path / "out.json"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/phase_zeta/build_brand_bundle.py",
            "--brand",
            "리바로",
            "--snapshot-at",
            "2026-05-24T08:00:00+09:00",
            "--config",
            str(config_path),
            "--out",
            str(out),
            "--render-narrative",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert out.with_suffix(".narrative.md").exists()


def test_cli_v1_1_version_defaults(tmp_path):
    out = tmp_path / "out_v1_1.json"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/phase_zeta/build_brand_bundle.py",
            "--brand",
            "리바로",
            "--snapshot-at",
            "2026-05-25T08:00:00+09:00",
            "--version",
            "v1_1",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    bundle = json.loads(out.read_text(encoding="utf-8"))
    assert bundle["bundle_meta"]["config_version"] == "phase_zeta_v1_1"
    assert "market_views" in bundle
