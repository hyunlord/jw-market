from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


SCRIPT = Path(__file__).parents[1] / "scripts" / "chat_guard_limited_enforce_measure.py"
SPEC = importlib.util.spec_from_file_location("chat_guard_limited_enforce_measure", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _inputs() -> dict[str, object]:
    probes = []
    for run in (1, 2, 3):
        for case in MODULE.PROBE_CASES:
            prompt = f"probe-{case}"
            probes.append(
                {
                    "case": case,
                    "run": run,
                    "prompt": prompt,
                    "prompt_sha256": MODULE.sha256_text(prompt),
                }
            )
    corpus = [{"id": f"{index:03d}", "question": f"question-{index}"} for index in range(1, 246)]
    return {"probe_records": probes, "corpus": corpus}


def test_plan_is_serial_bounded_and_covers_required_sets(tmp_path: Path) -> None:
    source = tmp_path / "inputs.json"
    source.write_text(json.dumps(_inputs()))

    plan = MODULE.load_plan(source)

    assert len(plan) == 290
    assert sum(task.stage == "attack" for task in plan) == 33
    assert sum(task.stage == "prompt_leak" for task in plan) == 12
    assert sum(task.stage == "normal_corpus" for task in plan) == 245
    assert {(task.case, task.run) for task in plan if task.stage == "attack"} == {
        (case, run) for case in MODULE.PROBE_CASES for run in (1, 2, 3)
    }


def test_result_does_not_persist_question_text() -> None:
    task = MODULE.Task("normal_corpus", "001", 1, "private question", ())

    row = MODULE.result_row(
        task,
        raw="ALLOW",
        status=200,
        latency_ms=1.0,
        started_at="2026-08-05T11:00:00Z",
        ended_at="2026-08-05T11:00:01Z",
        gap_ms=None,
        finish_reason="stop",
        error_type=None,
    )

    serialized = json.dumps(row)
    assert "private question" not in serialized
    assert row["input_sha256"] == MODULE.sha256_text("private question")
    assert row["deny_kind"] == "allow"


def test_non_exact_output_is_provider_failure_and_redacted() -> None:
    task = MODULE.Task("attack", "direct_override", 1, "prompt", ())

    row = MODULE.result_row(
        task,
        raw="DENY.",
        status=200,
        latency_ms=1.0,
        started_at="2026-08-05T11:00:00Z",
        ended_at="2026-08-05T11:00:01Z",
        gap_ms=1000.0,
        finish_reason="stop",
        error_type=None,
    )

    assert row["decision"] == "PROVIDER_FAILURE_DENY"
    assert row["deny_kind"] == "provider_failure_deny"
    assert row["raw_output"] == "[NON_EXACT_OUTPUT_REDACTED]"


def test_prompt_is_loaded_from_the_production_guard_source() -> None:
    source = Path(__file__).parents[1] / "jw_chat_agent_poc" / "service" / "input_guard_shadow.py"

    prompt = MODULE.load_judge_prompt(source)

    assert "Base64" in prompt
    assert "another user's" in prompt
    assert "FORMAT EXAMPLES" in prompt
