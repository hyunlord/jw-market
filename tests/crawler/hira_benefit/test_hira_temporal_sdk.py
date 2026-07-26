from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("temporalio")

from temporalio.exceptions import ApplicationError

from pipeline.scripts.crawler.hira_benefit.receipts import write_json
from pipeline.scripts.crawler.hira_benefit.temporal_workflow import (
    completed_stage_receipt,
    raise_for_stage_result,
)


def test_stage_process_failure_remains_retryable(tmp_path: Path) -> None:
    with pytest.raises(ApplicationError) as error:
        raise_for_stage_result(
            stage="collect_details",
            return_code=1,
            receipt_path=tmp_path / "missing.json",
        )

    assert error.value.non_retryable is False
    assert error.value.type == "HiraStageError"


def test_explicit_gate_failure_is_non_retryable(tmp_path: Path) -> None:
    receipt_path = tmp_path / "collect_details.receipt.json"
    write_json(
        receipt_path,
        {"status": "failed", "gate_failures": ["pending_gap"]},
    )

    with pytest.raises(ApplicationError) as error:
        raise_for_stage_result(
            stage="collect_details",
            return_code=1,
            receipt_path=receipt_path,
        )

    assert error.value.non_retryable is True
    assert error.value.type == "HiraGateError"


def test_circuit_open_receipt_is_non_retryable(tmp_path: Path) -> None:
    receipt_path = tmp_path / "collect_details.receipt.json"
    write_json(
        receipt_path,
        {
            "status": "failed",
            "gate_failures": ["circuit_open"],
            "retry_after_seconds": 1800,
        },
    )

    with pytest.raises(ApplicationError) as error:
        raise_for_stage_result(
            stage="collect_details",
            return_code=1,
            receipt_path=receipt_path,
        )

    assert error.value.non_retryable is True


def test_completed_stage_receipt_resumes_without_rerunning_subprocess(
    tmp_path: Path,
) -> None:
    receipt_path = tmp_path / "collect_details.receipt.json"
    expected = {
        "stage": "collect_details",
        "status": "complete",
        "collected_count": 500,
    }
    write_json(receipt_path, expected)

    assert completed_stage_receipt(receipt_path) == expected


def test_failed_stage_receipt_is_not_resumable(tmp_path: Path) -> None:
    receipt_path = tmp_path / "collect_details.receipt.json"
    write_json(
        receipt_path,
        {"stage": "collect_details", "status": "failed"},
    )

    assert completed_stage_receipt(receipt_path) is None
