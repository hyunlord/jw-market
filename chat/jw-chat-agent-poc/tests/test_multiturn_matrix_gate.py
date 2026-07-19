from __future__ import annotations

import json
from pathlib import Path

from scripts.multiturn_matrix_gate import evaluate_turn, load_matrix


FIXTURE = Path(__file__).parent / "fixtures" / "multiturn_matrix.json"


def test_permanent_multiturn_matrix_has_all_nine_required_scenarios() -> None:
    matrix = load_matrix(FIXTURE)

    assert [item["id"] for item in matrix["scenarios"]] == [f"MT-{index:02d}" for index in range(1, 10)]
    assert matrix["repeats"] == 3
    assert all(2 <= len(item["turns"]) <= 3 for item in matrix["scenarios"])
    assert matrix["scenarios"][0]["turns"][1]["question"] == "2024년은?"
    assert matrix["scenarios"][0]["turns"][2]["question"] == "그 전 해는?"


def test_matrix_turn_gate_checks_trace_slots_and_answer_contract() -> None:
    turn = {
        "question": "2024년은?",
        "expected": {
            "resolved_question_contains": ["리바로", "2024년", "매출"],
            "slots": {
                "anchor_brand": ["리바로"],
                "metric": ["매출", "sales"],
                "period": ["2024"],
            },
            "answer_contains": ["리바로", "2024"],
            "answer_excludes": ["알츠하이머", "IPO"],
            "disposition": "answered",
        },
    }
    capture = {
        "answer": "리바로 2024년 매출을 확인했습니다.",
        "trace": {
            "qa_trace": {
                "conversation": {
                    "resolved_question": "리바로 2024년 매출은?",
                    "resolved_slots": {
                        "anchor_brand": "리바로",
                        "metric": "sales",
                        "period": "2024",
                    },
                },
                "final": {"disposition": "answered", "body_empty": False},
            }
        },
    }

    assert evaluate_turn(turn, capture) == []


def test_matrix_fixture_is_json_serializable_without_runtime_secrets() -> None:
    matrix = load_matrix(FIXTURE)

    encoded = json.dumps(matrix, ensure_ascii=False)

    assert "token" not in encoded.casefold()
    assert "password" not in encoded.casefold()
