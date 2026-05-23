import json
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "pipeline" / "scripts" / "run_market_pipeline.sh"


def get_api(path):
    with urllib.request.urlopen(f"http://127.0.0.1:8013{path}") as r:
        return json.load(r)


def test_market_pipeline_runner_documents_full_order():
    result = subprocess.run(
        ["bash", str(RUNNER), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    help_text = result.stdout
    for token in [
        "--all",
        "--from-layer3",
        "Layer1",
        "Layer2",
        "Layer3",
        "Layer4",
        "layer3_compute_general_v3.py",
        "build_cache_cause.py",
    ]:
        assert token in help_text


def test_market_pipeline_runner_verify_only_reports_phase15_ratios():
    result = subprocess.run(
        ["bash", str(RUNNER), "--verify-only"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "mart_general_brand_metric:" in result.stdout
    assert "cache_cause:" in result.stdout
    assert "제이클: 2025-Q3/Q1=" in result.stdout


def test_phase15_iqvia_result_is_present_after_pipeline_rerun():
    brand = urllib.parse.quote("가드메트")
    api = get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=IQVIA&measure=sales")
    series = api["data"]["sources_data"]["market_size_series"]
    by_period = {point["period"]: point for point in series}

    q1 = by_period["2025-Q1"]["value"]
    q3 = by_period["2025-Q3"]["value"]
    assert q3 / q1 >= 0.70
    assert "yoy_growth_pct" in series[0]
