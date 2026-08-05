from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "chat_guard_final_measure.py"
SPEC = importlib.util.spec_from_file_location("chat_guard_final_measure", SCRIPT)
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


def test_plan_stays_below_budget_and_marks_n7_unmeasured() -> None:
    plan = MODULE.build_plan(_inputs(), normal_ids=[f"{index:03d}" for index in range(1, 21)])

    assert len(plan.tasks) == 1196
    assert plan.call_budget == 1200
    assert plan.measured_corpus_windows == (5, 3)
    assert plan.unmeasured_corpus_windows == (7,)
    assert sum(task.stage == "reasoning_effort_245" for task in plan.tasks) == 490
    assert sum(task.stage == "detection_by_condition" for task in plan.tasks) == 66
    assert sum(task.stage == "any_deny_live" for task in plan.tasks) == 150
    assert sum(task.stage == "corpus_by_n" for task in plan.tasks) == 490


def test_build_body_changes_only_reasoning_effort() -> None:
    baseline = MODULE.build_body("question", history=("old",), condition="baseline")
    low = MODULE.build_body("question", history=("old",), condition="low")

    assert "reasoning_effort" not in baseline
    assert low["reasoning_effort"] == "low"
    low_without_condition = dict(low)
    low_without_condition.pop("reasoning_effort")
    assert low_without_condition == baseline
    assert baseline["temperature"] == 0
    assert baseline["max_tokens"] == 256
    assert baseline["stop"] == ["\n"]


@pytest.mark.parametrize(
    ("raw", "finish_reason", "decision", "taxonomy", "kind"),
    [
        ("ALLOW", "stop", "ALLOW", "exact_token", "allow"),
        ("DENY", "stop", "DENY", "exact_token", "policy_deny"),
        (" allow ", "stop", "PROVIDER_FAILURE_DENY", "whitespace_or_case", "provider_failure_deny"),
        ("DENY.", "stop", "PROVIDER_FAILURE_DENY", "punctuation", "provider_failure_deny"),
        ("", "length", "PROVIDER_FAILURE_DENY", "empty", "provider_failure_deny"),
        ("MAYBE", "stop", "PROVIDER_FAILURE_DENY", "unknown_token", "provider_failure_deny"),
    ],
)
def test_exact_token_parser_keeps_provider_failure_separate(
    raw: str,
    finish_reason: str,
    decision: str,
    taxonomy: str,
    kind: str,
) -> None:
    parsed = MODULE.parse_output(raw, finish_reason)

    assert parsed.decision == decision
    assert parsed.taxonomy == taxonomy
    assert parsed.deny_kind == kind


def test_result_artifact_never_contains_input_text(tmp_path: Path) -> None:
    task = MODULE.Task(
        stage="reasoning_effort_245",
        case="001",
        window=1,
        run=1,
        condition="baseline",
        question="private question text",
        question_sha256=MODULE.sha256_text("private question text"),
        history=(),
    )
    result = MODULE.success_result(
        task,
        raw="ALLOW",
        status=200,
        latency_ms=1.25,
        finish_reason="stop",
        usage={"completion_tokens": 4},
    )
    target = MODULE.write_result(tmp_path, result)
    serialized = target.read_text()

    assert "private question text" not in serialized
    assert json.loads(serialized)["input_sha256"] == task.question_sha256


def test_non_exact_output_is_always_redacted() -> None:
    task = MODULE.Task(
        stage="reasoning_effort_245",
        case="001",
        window=1,
        run=1,
        condition="baseline",
        question="short",
        question_sha256=MODULE.sha256_text("short"),
        history=(),
    )
    result = MODULE.success_result(
        task,
        raw="partial user echo",
        status=200,
        latency_ms=1.0,
        finish_reason="length",
        usage={},
    )

    assert result["raw_model_output"] == "[NON_EXACT_OUTPUT_REDACTED]"
    assert result["raw_output_sha256"] == MODULE.sha256_text("partial user echo")
    assert result["raw_output_length"] == len("partial user echo")


def test_sanitizer_rewrites_legacy_non_exact_artifact(tmp_path: Path) -> None:
    target = tmp_path / "judge" / "baseline" / "case_1_run1.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "decision": "PROVIDER_FAILURE_DENY",
                "taxonomy": "prose_or_multilingual",
                "raw_model_output": "legacy reflected input",
            }
        )
    )

    assert MODULE.sanitize_output_tree(tmp_path) == 1
    assert json.loads(target.read_text())["raw_model_output"] == "[NON_EXACT_OUTPUT_REDACTED]"


def test_normal_sample_is_deterministic_and_excludes_prior_denies() -> None:
    prior = [
        {"case": f"{index:03d}", "decision": "ALLOW", "taxonomy": "exact_token"}
        for index in range(1, 23)
    ]
    prior[3]["decision"] = "DENY"
    prior[7]["decision"] = "PROVIDER_FAILURE_DENY"

    assert MODULE.select_normal_ids(prior, count=20) == tuple(
        f"{index:03d}" for index in range(1, 23) if index not in (4, 8)
    )


def test_input_validation_requires_authoritative_counts_and_hashes() -> None:
    payload = _inputs()
    MODULE.validate_inputs(payload)
    payload["corpus"][0]["id"] = payload["corpus"][1]["id"]

    with pytest.raises(ValueError, match="duplicate corpus id"):
        MODULE.validate_inputs(payload)


def test_login_accepts_ephemeral_credentials_without_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"data": {"access_token": "token"}}

    def fake_post(url: str, **kwargs: object) -> Response:
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(MODULE.requests, "post", fake_post)
    token = MODULE.login(
        "https://admin.invalid",
        3.0,
        credentials={"GENOS_ADMIN_USER": "ephemeral-user", "GENOS_ADMIN_PASSWORD": "ephemeral-password"},
    )

    assert token == "token"
    assert captured["json"] == {"user_id": "ephemeral-user", "password": "ephemeral-password"}
