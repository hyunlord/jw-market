from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
GATE = ROOT / "scripts" / "verify_ocr_psm_gate.py"
SOURCE = ROOT / "src" / "preprocessor.py"


def _summary(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "timing": [
                    {
                        "engine": "tesseract",
                        "tesseract_psm": 4,
                        "dpi": 200,
                        "workers": 4,
                        "pages": 60,
                        "seconds_per_page": 2.46,
                        "peak_rss_bytes": 585_000_000,
                    }
                ],
                "accuracy": [
                    {
                        "engine": "tesseract",
                        "tesseract_psm": 4,
                        "dpi": 200,
                        "workers": 4,
                        "group": "en",
                        "pages": 20,
                        "cer_macro": 0.10,
                        "numeric_error_rate": 0.01,
                    },
                    {
                        "engine": "tesseract",
                        "tesseract_psm": 4,
                        "dpi": 200,
                        "workers": 4,
                        "group": "ko",
                        "pages": 20,
                        "cer_macro": 0.0771,
                        "numeric_error_rate": 0.108,
                    },
                    {
                        "engine": "tesseract",
                        "tesseract_psm": 4,
                        "dpi": 200,
                        "workers": 4,
                        "group": "mixed",
                        "pages": 20,
                        "cer_macro": 0.34,
                        "numeric_error_rate": 0.21,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def test_gate_accepts_measured_population_when_thresholds_hold(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _summary(summary)

    completed = subprocess.run(
        [
            sys.executable,
            str(GATE),
            "--summary",
            str(summary),
            "--max-ko-cer",
            "0.08",
            "--max-numeric-error",
            "0.11",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "checked=60" in completed.stdout
    assert "population=60" in completed.stdout
    assert "failures=0" in completed.stdout
    assert "exit_code=0" in completed.stdout


def test_gate_fails_when_accuracy_threshold_is_injected_too_low(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _summary(summary)

    completed = subprocess.run(
            [
                sys.executable,
                str(GATE),
                "--summary",
                str(summary),
                "--max-ko-cer",
                "0.01",
                "--max-numeric-error",
                "0.11",
            ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "failures=1" in completed.stdout
    assert "exit_code=1" in completed.stdout


def test_gate_fails_when_population_is_zero(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"timing": [], "accuracy": []}), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(GATE), "--summary", str(summary)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "checked=0" in completed.stdout
    assert "population=0" in completed.stdout
    assert "exit_code=1" in completed.stdout


def test_runtime_rejects_invalid_psm_instead_of_falling_back() -> None:
    environment = {**os.environ, "OCR_TESSERACT_PSM": "99"}
    completed = subprocess.run(
        [sys.executable, "-c", f"exec(compile(open({str(SOURCE)!r}, 'rb').read(), {str(SOURCE)!r}, 'exec'))"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "OCR_TESSERACT_PSM" in completed.stderr
