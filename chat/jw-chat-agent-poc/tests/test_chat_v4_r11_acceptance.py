from __future__ import annotations

from scripts.chat_v4_r11_reason_code_acceptance import evaluate_cases


def test_r11_reason_code_precision_and_fact_retention_gate() -> None:
    rows, summary = evaluate_cases()

    assert len(rows) == 1_920
    for metrics in summary.values():
        assert metrics["samples"] == 480
        assert metrics["precision"] >= 0.995
        assert metrics["recall"] == 1.0
        assert metrics["false_positive"] == 0
        assert metrics["false_negative"] == 0
        assert metrics["grounded_value_loss"] == 0
        assert metrics["source_loss"] == 0
        assert metrics["empty_output"] == 0
