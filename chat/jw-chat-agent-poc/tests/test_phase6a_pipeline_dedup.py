from __future__ import annotations

import builtins
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from jw_chat_agent_poc.service import answer_pipeline


FIXTURES = Path(__file__).parent / "characterization" / "fixtures"


def _context(*, deep_mode: bool = False) -> answer_pipeline.AnswerPipelineContext:
    return answer_pipeline.AnswerPipelineContext(
        question="리바로 매출 알려줘",
        result={"tool_calls": []},
        markdown_response=None,
        fact_md="",
        policy_fact_md="",
        file_context_fact="",
        deep_mode=deep_mode,
        market_contract_allowed=True,
        general_contracts_allowed=True,
        external_tool_agent_result=False,
        empty_file_answer=lambda _answer: False,
        file_context_fallback=lambda answer: answer,
        append_file_context_source=lambda answer, _fact, _file: answer,
        record_source_notice=lambda _attached: None,
        relational_claim_gate=lambda answer: answer,
        natural_fact_lead=lambda answer: answer,
        file_postprocess_isolation=lambda answer: answer,
        evidence_binding_gate=lambda answer: answer,
        strip_verified_progress=lambda answer: answer,
    )


def _named(
    stages: tuple[answer_pipeline.AnswerPipelineStage, ...],
    *names: str,
) -> tuple[answer_pipeline.AnswerPipelineStage, ...]:
    wanted = set(names)
    return tuple(stage for stage in stages if stage.name in wanted)


def test_same_gate_and_same_input_is_evaluated_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"answer_contract": 0, "claim_policy": 0}

    def contract(_question, answer, *_args, **_kwargs):
        calls["answer_contract"] += 1
        return answer

    def policy(_question, answer, _fact_md):
        calls["claim_policy"] += 1
        return answer

    monkeypatch.setenv(answer_pipeline.PIPELINE_DEDUP_ENV, "1")
    monkeypatch.setattr(answer_pipeline, "enforce_answer_contract", contract)
    monkeypatch.setattr(answer_pipeline, "apply_claim_policy", policy)

    pre, _post = answer_pipeline.build_answer_pipeline_stages(_context())
    selected = _named(
        pre,
        "answer_contract_first",
        "claim_policy_repeat",
        "answer_contract_second",
        "claim_policy_post",
    )

    assert answer_pipeline.run_answer_pipeline("same", selected) == "same"
    assert calls == {"answer_contract": 1, "claim_policy": 1}


def test_changed_input_is_revalidated_and_violation_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_inputs: list[str] = []

    def contract(_question, answer, *_args, **_kwargs):
        contract_inputs.append(answer)
        return answer.replace("UNBOUND", "REJECTED")

    policy_calls = 0

    def inject_violation(_question, answer, _fact_md):
        nonlocal policy_calls
        policy_calls += 1
        return f"{answer}|UNBOUND" if policy_calls == 1 else answer

    monkeypatch.setenv(answer_pipeline.PIPELINE_DEDUP_ENV, "1")
    monkeypatch.setattr(answer_pipeline, "enforce_answer_contract", contract)
    monkeypatch.setattr(answer_pipeline, "apply_claim_policy", inject_violation)

    pre, _post = answer_pipeline.build_answer_pipeline_stages(_context())
    selected = _named(
        pre,
        "answer_contract_first",
        "claim_policy_repeat",
        "answer_contract_second",
    )

    assert answer_pipeline.run_answer_pipeline("seed", selected) == "seed|REJECTED"
    assert contract_inputs == ["seed", "seed|UNBOUND"]


def test_changed_input_is_rechecked_by_late_claim_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_inputs: list[str] = []

    def policy(_question, answer, _fact_md):
        policy_inputs.append(answer)
        return answer.replace("UNSAFE", "REMOVED")

    monkeypatch.setenv(answer_pipeline.PIPELINE_DEDUP_ENV, "1")
    monkeypatch.setattr(answer_pipeline, "apply_claim_policy", policy)
    monkeypatch.setattr(
        answer_pipeline,
        "append_blocked_metric_notices_from_markdown_response",
        lambda answer, _response: f"{answer}|UNSAFE",
    )
    pre, _post = answer_pipeline.build_answer_pipeline_stages(_context())
    selected = _named(
        pre,
        "claim_policy_repeat",
        "blocked_metric_notices",
        "claim_policy_post",
    )

    assert answer_pipeline.run_answer_pipeline("seed", selected) == "seed|REMOVED"
    assert policy_inputs == ["seed", "seed|UNSAFE"]


def test_flag_off_preserves_all_30_stages_and_imports_no_dedup_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "jw_chat_agent_poc.service.pipeline_dedup"
    monkeypatch.setenv(answer_pipeline.PIPELINE_DEDUP_ENV, "0")
    sys.modules.pop(module_name, None)
    real_import = builtins.__import__

    def guarded_import(name: str, *args, **kwargs):
        if name == module_name:
            raise AssertionError("dedup module imported while disabled")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    pre, post = answer_pipeline.build_answer_pipeline_stages(_context())

    assert tuple(stage.name for stage in pre) == answer_pipeline.PRE_CHART_STAGE_NAMES
    assert tuple(stage.name for stage in post) == answer_pipeline.POST_CHART_STAGE_NAMES
    assert len(pre) + len(post) == 30
    assert module_name not in sys.modules


def test_dedup_does_not_cache_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(answer_pipeline.PIPELINE_DEDUP_ENV, "1")
    attempts = 0

    def broken(_question, _answer, *_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("gate failed")

    monkeypatch.setattr(answer_pipeline, "enforce_answer_contract", broken)
    pre, _post = answer_pipeline.build_answer_pipeline_stages(_context())
    first = _named(pre, "answer_contract_first")

    with pytest.raises(RuntimeError, match="gate failed"):
        answer_pipeline.run_answer_pipeline("same", first)
    with pytest.raises(RuntimeError, match="gate failed"):
        answer_pipeline.run_answer_pipeline("same", first)

    assert attempts == 2


def test_flag_off_cold_import_does_not_load_dedup_module() -> None:
    project_root = Path(__file__).resolve().parents[1]
    module_name = "jw_chat_agent_poc.service.pipeline_dedup"
    script = f"""
import sys

class BlockDedupModule:
    def find_spec(self, fullname, path, target=None):
        if fullname == {module_name!r}:
            raise RuntimeError("dedup import attempted")
        return None

sys.meta_path.insert(0, BlockDedupModule())
from jw_chat_agent_poc.service import answer_pipeline
assert {module_name!r} not in sys.modules
assert answer_pipeline.pipeline_dedup_enabled() is False
"""
    env = dict(os.environ)
    env[answer_pipeline.PIPELINE_DEDUP_ENV] = "0"

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def _snapshot_fact_markdown(snapshot: dict) -> str:
    rows = [fact for fact in snapshot.get("evidence_facts", ()) if isinstance(fact, dict)]
    if not rows:
        return ""
    lines = [
        "## 확정 fact set",
        "| entity | metric | value | unit | period |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {fact.get('entity', '')} | {fact.get('metric', '')} | "
        f"{fact.get('count', '')} | {fact.get('unit', '')} | {fact.get('period', '')} |"
        for fact in rows
    )
    return "\n".join(lines)


def test_128_corpus_answers_are_byte_identical_with_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = json.loads((FIXTURES / "corpus.v1.json").read_text(encoding="utf-8"))["cases"]
    snapshots = {
        snapshot["case_id"]: snapshot
        for snapshot in json.loads(
            (FIXTURES / "observed_snapshots.v1.json").read_text(encoding="utf-8")
        )["snapshots"]
    }
    compared = 0

    for case in corpus:
        snapshot = next(
            (snapshots[snapshot_id] for snapshot_id in case["snapshot_ids"] if snapshot_id in snapshots),
            None,
        )
        assert snapshot is not None, case["question"]
        fact_md = _snapshot_fact_markdown(snapshot)
        context = replace(
            _context(),
            question=case["question"],
            markdown_response={"fact_md": fact_md},
            fact_md=fact_md,
            policy_fact_md=fact_md,
        )

        monkeypatch.setenv(answer_pipeline.PIPELINE_DEDUP_ENV, "0")
        legacy_pre, legacy_post = answer_pipeline.build_answer_pipeline_stages(context)
        legacy = answer_pipeline.run_answer_pipeline(snapshot["final_answer"], legacy_pre)
        legacy = answer_pipeline.run_answer_pipeline(legacy, legacy_post)

        monkeypatch.setenv(answer_pipeline.PIPELINE_DEDUP_ENV, "1")
        dedup_pre, dedup_post = answer_pipeline.build_answer_pipeline_stages(context)
        dedup = answer_pipeline.run_answer_pipeline(snapshot["final_answer"], dedup_pre)
        dedup = answer_pipeline.run_answer_pipeline(dedup, dedup_post)

        assert dedup.encode() == legacy.encode(), case["question"]
        assert [stage.name for stage in dedup_pre] == [stage.name for stage in legacy_pre]
        assert [stage.name for stage in dedup_post] == [stage.name for stage in legacy_post]
        compared += 1

    assert compared == 128
