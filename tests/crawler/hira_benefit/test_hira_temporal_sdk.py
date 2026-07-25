from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("temporalio")

from temporalio.exceptions import ApplicationError

from pipeline.scripts.crawler.hira_benefit.receipts import write_json
from pipeline.scripts.crawler.hira_benefit.temporal_workflow import (
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
