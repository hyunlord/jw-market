from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests

from scripts.fact_scoreboard.sse import parse_sse_file
from scripts.fact_scoreboard.text_numbers import extract_numeric_mentions

from jw_chat_agent_poc.service.app import SessionStore, _answer_question, _default_agent_factory, _sse_events
from jw_chat_agent_poc.tools.metrics.market_scope import MarketScopeResolver


QUESTIONS: tuple[tuple[str, str], ...] = (
    ("Q01", "리바로 관련 최근 이슈 뭐 있어"),
    ("Q02", "리바로하이 질병 환자수랑 최근 매출 한번에"),
    ("Q04", "리바로 최근 매출 추이 어때"),
    ("Q05", "리바로 시장 경쟁 구도 변화는 어때"),
    ("Q06", "페린젝트 매출 추이 어때"),
    ("Q07", "리바로 어느 채널에서 잘 팔려"),
    ("Q08", "리바로젯 시장 규모 얼마나 돼"),
    ("Q09", "리바로 점유율 몇 위야"),
    ("Q10", "가드렛 매출 추이"),
    ("Q11", "베노훼럼 매출 추이"),
)
CHANNEL_PARAPHRASE_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("Q07_CHANNEL_BY", "리바로 채널별로 보여줘"),
    ("Q07_CHANNEL_SHORT", "리바로 채널"),
    ("Q07_CHANNEL_WHERE", "리바로 어느 채널에서 잘 팔려"),
    ("Q07_CHANNEL_HOSPITAL", "리바로 의원/병원별 실적"),
    ("Q07_CHANNEL_SALES", "리바로 채널별 매출"),
    ("Q07_CHANNEL_DISTRIBUTION", "리바로 채널 분포"),
    ("Q07_CHANNEL_MIX", "리바로 채널 mix"),
    ("Q07_CHANNEL_COMPOSITION", "리바로 채널 구성"),
)
FRESH_GOLDEN_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("F01", "2025년 2분기 매출 얼마야"),
    ("F02", "고지혈증 시장 상위 5개 브랜드 알려줘"),
    ("F03", "리바로 최근 매출 추이 어때"),
)
HISTORY_GOLDEN_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("H01", "뇌경색 임상·허가 경쟁약물"),
    ("H02", "2025년 2분기 매출 얼마야"),
    ("H03", "고지혈증 시장 상위 5개 브랜드 알려줘"),
)
MODE_TRANSITION_GOLDEN_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("M01", "/deep 리바로 경쟁구도"),
    ("M02", "2025년 2분기 매출 얼마야"),
    ("M03", "고지혈증 시장 상위 5개 브랜드 알려줘"),
)
P0G_GENERAL_GOLDEN_QIDS = frozenset({"F01", "F02", "H02", "H03", "M02", "M03"})
P0G_FAST_PATH_STAGE_NAME = "조회 계획 확정"
P0G_MARKET_TOOL_STAGE_BY_QID = {
    "F01": "브랜드 매출 조회",
    "F02": "상위 브랜드 확인",
    "H02": "브랜드 매출 조회",
    "H03": "상위 브랜드 확인",
    "M02": "브랜드 매출 조회",
    "M03": "상위 브랜드 확인",
}
P0G_FORBIDDEN_GENERAL_STAGE_NAMES = frozenset(
    {
        "첨부 문서 조회",
        "임상 데이터 조회",
        "국내 임상 정보 확인",
        "식약처 허가 정보 확인",
        "건강보험 환자 정보 확인",
        "FDA 안전성 정보 확인",
        "최신 웹 자료 검색",
    }
)
P0G_FAIL_CLOSED_ANSWER_SENTINELS = (
    "데이터 존재 여부를 확인하지 못했습니다",
    "지원되지 않는 시장",
    "조회 오류",
)
P0G_HISTORY_SEED_STAGE_NAMES = frozenset(
    {
        "임상 데이터 조회",
        "국내 임상 정보 확인",
        "식약처 허가 정보 확인",
    }
)

VOLATILE_KEYS = {
    "answer_cleanup",
    "answer_generation_total",
    "chart_generation",
    "conversation_id",
    "elapsed_ms",
    "generated_at",
    "generation_time_ms",
    "session_id",
    "started_at",
    "started_at_monotonic",
    "timing",
    "timing_markdown",
}
TIMING_BLOCK = re.compile(r"\n*## 처리 시간\n\n(?:.*?)(?=\n## 출처|\Z)", re.DOTALL)


def capture(
    out_dir: Path,
    external_mode: str,
    base_url: str | None = None,
    questions: tuple[tuple[str, str], ...] = QUESTIONS,
    conversation_id: str | None = None,
    *,
    portal_user_id: str | None = None,
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("sse", "markdown", "traces"):
        (out_dir / name).mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []
    store = SessionStore()
    resolver = MarketScopeResolver()
    for qid, question in questions:
        started = time.perf_counter()
        status = "ok"
        error = ""
        result: dict[str, Any] = {}
        events: list[str] = []
        try:
            if base_url:
                events = [
                    _http_sse(
                        base_url,
                        question,
                        external_mode,
                        conversation_id=conversation_id,
                        portal_user_id=portal_user_id,
                    )
                ]
                result = {"capture_mode": "http", "trace_available": False}
            else:
                item = _answer_question(
                    store,
                    resolver,
                    _default_agent_factory,
                    question,
                    external_mode,
                    conversation_id,
                )
                result = item["result"]
                events = list(_sse_events(question, result, item.get("conversation_id")))
        except Exception as exc:  # noqa: BLE001 - parity capture must record all question failures.
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
        raw_sse = "".join(events)
        (out_dir / "sse" / f"{qid}.sse").write_text(raw_sse, encoding="utf-8")
        parsed = parse_sse_file(out_dir / "sse" / f"{qid}.sse")
        acceptance_pass, acceptance_error = _history_golden_acceptance(
            qid,
            parsed.answer_markdown,
        )
        (out_dir / "markdown" / f"{qid}.md").write_text(parsed.answer_markdown, encoding="utf-8")
        (out_dir / "traces" / f"{qid}.json").write_text(
            json.dumps(_trace_payload(qid, question, result, parsed), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        row = {
            "qid": qid,
            "question": question,
            "status": status,
            "error": error,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "delta_count": parsed.delta_count,
            "done_count": parsed.done_count,
            "error_count": parsed.error_count,
            "answer_chars": parsed.answer_chars,
            "sources": parsed.sources,
            "steps": list(parsed.steps),
            "conversation_ids": list(parsed.conversation_ids),
            "acceptance_pass": acceptance_pass,
            "acceptance_error": acceptance_error,
        }
        summary.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "questions.json").write_text(json.dumps(dict(questions), ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(
        row["status"] == "ok"
        and row["done_count"] == 1
        and row["error_count"] == 0
        and row["acceptance_pass"]
        for row in summary
    ) else 1


def capture_p0g_suite(
    out_dir: Path,
    external_mode: str,
    base_url: str | None,
    *,
    history_conversation_id: str | None = None,
    max_general_elapsed_ms: float = 10_000.0,
    portal_equivalent: bool = False,
    portal_user_id: str | None = None,
    file_base_url: str | None = None,
    file_workflow_id: int = 301,
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    scenarios = (
        ("fresh", FRESH_GOLDEN_QUESTIONS, None),
        (
            "history",
            HISTORY_GOLDEN_QUESTIONS,
            history_conversation_id or f"parity-history-{uuid4().hex}",
        ),
        (
            "mode-transition",
            MODE_TRANSITION_GOLDEN_QUESTIONS,
            f"parity-mode-transition-{uuid4().hex}",
        ),
    )
    summary: list[dict[str, Any]] = []
    for name, questions, conversation_id in scenarios:
        status = capture(
            out_dir / name,
            external_mode,
            base_url,
            questions,
            conversation_id,
            portal_user_id=portal_user_id,
        )
        rows = json.loads((out_dir / name / "summary.json").read_text(encoding="utf-8"))
        latency_failures = [
            str(row["qid"])
            for row in rows
            if row.get("qid") in P0G_GENERAL_GOLDEN_QIDS
            and float(row.get("elapsed_ms", float("inf"))) > max_general_elapsed_ms
        ]
        route_contamination_failures = _p0g_route_contamination_failures(rows)
        step_evidence_failures = _p0g_missing_step_evidence(rows) if portal_equivalent else []
        fast_path_stage_failures = _p0g_fast_path_stage_failures(rows) if portal_equivalent else []
        market_tool_stage_failures = _p0g_market_tool_stage_failures(rows) if portal_equivalent else {}
        source_evidence_failures = _p0g_source_evidence_failures(rows) if portal_equivalent else {}
        seed_execution_failures = _p0g_seed_execution_failures(name, rows) if portal_equivalent else []
        session_continuity_failures = (
            _p0g_session_continuity_failures(rows, conversation_id)
            if portal_equivalent and conversation_id
            else {}
        )
        summary.append(
            {
                "scenario": name,
                "status": status,
                "latency_failures": latency_failures,
                "route_contamination_failures": route_contamination_failures,
                "step_evidence_failures": step_evidence_failures,
                "fast_path_stage_failures": fast_path_stage_failures,
                "market_tool_stage_failures": market_tool_stage_failures,
                "source_evidence_failures": source_evidence_failures,
                "seed_execution_failures": seed_execution_failures,
                "session_continuity_failures": session_continuity_failures,
            }
        )
    qualification_failures: list[str] = []
    if not portal_equivalent:
        qualification_failures.append("portal-equivalent entry path was not declared")
    if portal_equivalent and not base_url:
        qualification_failures.append("portal-equivalent evidence requires a deployed base URL")
    if portal_equivalent and not str(portal_user_id or "").strip():
        qualification_failures.append("portal-equivalent evidence requires X-Portal-User-Id")
    if not history_conversation_id:
        qualification_failures.append("uploaded-file history conversation ID was not supplied")
    file_probe = {
        "attempted": False,
        "document_count": 0,
        "passed": False,
        "error": "",
    }
    if history_conversation_id and not file_base_url:
        qualification_failures.append("file bridge base URL was not supplied")
        file_probe["error"] = "file bridge base URL was not supplied"
    elif history_conversation_id and file_base_url:
        passed, document_count, error = _probe_uploaded_file_session(
            file_base_url,
            history_conversation_id,
            file_workflow_id,
        )
        file_probe.update(
            attempted=True,
            document_count=document_count,
            passed=passed,
            error=error,
        )
        if not passed:
            qualification_failures.append(f"uploaded-file history session probe failed: {error}")
    report = {
        "evidence_context": {
            "transport": "direct-chat-sse" if base_url else "local-inprocess",
            "portal_equivalent_declared": portal_equivalent,
            "portal_user_id_supplied": bool(str(portal_user_id or "").strip()),
            "history_conversation_id_supplied": bool(history_conversation_id),
            "file_probe": file_probe,
        },
        "qualification_failures": qualification_failures,
        "scenarios": summary,
    }
    (out_dir / "p0g_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"p0g": report}, ensure_ascii=False), flush=True)
    return 0 if (
        not qualification_failures
        and all(
            item["status"] == 0
            and not item["latency_failures"]
            and not item["route_contamination_failures"]
            and not item["step_evidence_failures"]
            and not item["fast_path_stage_failures"]
            and not item["market_tool_stage_failures"]
            and not item["source_evidence_failures"]
            and not item["seed_execution_failures"]
            and not item["session_continuity_failures"]
            for item in summary
        )
    ) else 1


def _p0g_route_contamination_failures(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    failures: dict[str, list[str]] = {}
    for row in rows:
        qid = str(row.get("qid", ""))
        if qid not in P0G_GENERAL_GOLDEN_QIDS:
            continue
        forbidden: list[str] = []
        for step in row.get("steps", ()):
            if not isinstance(step, dict):
                continue
            name = str(step.get("name", ""))
            if (
                name in P0G_FORBIDDEN_GENERAL_STAGE_NAMES
                or name.startswith("딥리서치 ")
            ) and name not in forbidden:
                forbidden.append(name)
        if forbidden:
            failures[qid] = forbidden
    return failures


def _p0g_missing_step_evidence(rows: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    for row in rows:
        qid = str(row.get("qid", ""))
        if qid not in P0G_GENERAL_GOLDEN_QIDS:
            continue
        steps = row.get("steps", ())
        if not isinstance(steps, (list, tuple)) or not any(
            isinstance(step, dict) and str(step.get("name", "")).strip()
            for step in steps
        ):
            failures.append(qid)
    return failures


def _p0g_fast_path_stage_failures(rows: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    for row in rows:
        qid = str(row.get("qid", ""))
        if qid not in P0G_GENERAL_GOLDEN_QIDS:
            continue
        completed_names = {
            str(step.get("name", "")).strip()
            for step in row.get("steps", ())
            if isinstance(step, dict)
            and str(step.get("status", "")).strip() == "done"
            and str(step.get("name", "")).strip()
        }
        if P0G_FAST_PATH_STAGE_NAME not in completed_names:
            failures.append(qid)
    return failures


def _p0g_market_tool_stage_failures(rows: list[dict[str, Any]]) -> dict[str, str]:
    failures: dict[str, str] = {}
    for row in rows:
        qid = str(row.get("qid", ""))
        expected_stage = P0G_MARKET_TOOL_STAGE_BY_QID.get(qid)
        if expected_stage is None:
            continue
        completed_names = {
            str(step.get("name", "")).strip()
            for step in row.get("steps", ())
            if isinstance(step, dict)
            and str(step.get("status", "")).strip() == "done"
            and str(step.get("name", "")).strip()
        }
        if expected_stage not in completed_names:
            failures[qid] = expected_stage
    return failures


def _p0g_source_evidence_failures(rows: list[dict[str, Any]]) -> dict[str, str]:
    failures: dict[str, str] = {}
    for row in rows:
        qid = str(row.get("qid", ""))
        if qid not in P0G_GENERAL_GOLDEN_QIDS:
            continue
        raw_sources = str(row.get("sources", "")).strip()
        labels = {item.strip() for item in raw_sources.split(",") if item.strip()}
        if "UBIST" not in labels:
            failures[qid] = raw_sources
    return failures


def _p0g_seed_execution_failures(scenario: str, rows: list[dict[str, Any]]) -> list[str]:
    seed_qid = {"history": "H01", "mode-transition": "M01"}.get(scenario)
    if seed_qid is None:
        return []
    seed_row = next((row for row in rows if str(row.get("qid", "")) == seed_qid), None)
    if seed_row is None:
        return [seed_qid]
    step_names = {
        str(step.get("name", "")).strip()
        for step in seed_row.get("steps", ())
        if isinstance(step, dict)
        and str(step.get("status", "")).strip() == "done"
        and str(step.get("name", "")).strip()
    }
    if scenario == "history":
        executed = bool(step_names & P0G_HISTORY_SEED_STAGE_NAMES)
    else:
        executed = any(name.startswith("딥리서치 ") for name in step_names)
    return [] if executed else [seed_qid]


def _p0g_session_continuity_failures(
    rows: list[dict[str, Any]],
    expected_conversation_id: str,
) -> dict[str, list[str]]:
    failures: dict[str, list[str]] = {}
    for row in rows:
        qid = str(row.get("qid", ""))
        raw_ids = row.get("conversation_ids", ())
        returned_ids = (
            [str(item) for item in raw_ids if str(item).strip()]
            if isinstance(raw_ids, (list, tuple))
            else []
        )
        if not returned_ids:
            failures[qid] = ["<missing>"]
        elif any(item != expected_conversation_id for item in returned_ids):
            failures[qid] = returned_ids
    return failures


def diff_captures(
    before: Path,
    after: Path,
    report_dir: Path,
    questions: tuple[tuple[str, str], ...] = QUESTIONS,
) -> int:
    report_dir.mkdir(parents=True, exist_ok=True)
    results = [_diff_question(qid, before, after) for qid, _ in questions]
    report = {"status": "pass" if all(item["pass"] for item in results) else "fail", "questions": results}
    (report_dir / "parity_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "parity_report.md").write_text(_markdown_report(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "failed": [item["qid"] for item in results if not item["pass"]]}, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


def diff_multi_baseline(baseline_root: Path, after: Path, report_dir: Path) -> int:
    report_dir.mkdir(parents=True, exist_ok=True)
    baselines = _baseline_dirs(baseline_root)
    if not baselines:
        raise SystemExit(f"no capture baselines found under {baseline_root}")
    results = [_diff_question_against_any(qid, baselines, after) for qid, _ in QUESTIONS]
    report = {
        "status": "pass" if all(item["pass"] for item in results) else "fail",
        "baseline_root": str(baseline_root),
        "baselines": [str(path) for path in baselines],
        "questions": results,
    }
    (report_dir / "parity_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "parity_report.md").write_text(_markdown_report(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "failed": [item["qid"] for item in results if not item["pass"]]}, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


def self_test(capture_dir: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    same_dir = out_dir / "same"
    mutated_dir = out_dir / "mutated"
    same_status = diff_captures(capture_dir, capture_dir, same_dir)
    if mutated_dir.exists():
        shutil.rmtree(mutated_dir)
    shutil.copytree(capture_dir, mutated_dir)
    target = next((mutated_dir / "sse").glob("*.sse"))
    target.write_text(
        target.read_text(encoding="utf-8") + "event: delta\ndata: MUTATION_SENTINEL 999억원\n\n",
        encoding="utf-8",
    )
    mutated_status = diff_captures(capture_dir, mutated_dir, out_dir / "mutated_diff")
    summary = {"same_passed": same_status == 0, "mutation_detected": mutated_status != 0}
    (out_dir / "self_test_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["same_passed"] and summary["mutation_detected"] else 1


def _baseline_dirs(root: Path) -> list[Path]:
    candidates = sorted(path for path in root.iterdir() if path.is_dir())
    return [path for path in candidates if (path / "sse").is_dir() and (path / "traces").is_dir()]


def _diff_question_against_any(qid: str, baselines: list[Path], after: Path) -> dict[str, Any]:
    attempts = [_diff_question(qid, baseline, after) for baseline in baselines]
    passing = [attempt for attempt in attempts if attempt["pass"]]
    if passing:
        selected = dict(passing[0])
        selected["matched_baseline"] = str(baselines[attempts.index(passing[0])])
        selected["baseline_attempts"] = len(attempts)
        return selected
    best = _best_failed_attempt(attempts)
    best["matched_baseline"] = None
    best["baseline_attempts"] = len(attempts)
    best["all_attempt_failures"] = [
        {"baseline": str(path), "checks": attempt["checks"], "detail": attempt["detail"]}
        for path, attempt in zip(baselines, attempts, strict=True)
    ]
    return best


def _best_failed_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    return dict(max(attempts, key=lambda attempt: sum(1 for ok in attempt["checks"].values() if ok)))


def _trace_payload(qid: str, question: str, result: dict[str, Any], parsed: Any) -> dict[str, Any]:
    return {
        "qid": qid,
        "question": question,
        "result": result,
        "sse": {
            "delta_count": parsed.delta_count,
            "done_count": parsed.done_count,
            "error_count": parsed.error_count,
            "render_issues": parsed.render_issues,
            "sources": parsed.sources,
            "charts": parsed.charts,
        },
    }


def _http_sse(
    base_url: str,
    question: str,
    external_mode: str,
    *,
    conversation_id: str | None = None,
    portal_user_id: str | None = None,
) -> str:
    url = base_url.rstrip("/") + "/chat/stream"
    params = {"question": question, "external_mode": external_mode}
    if conversation_id:
        params["conversation_id"] = conversation_id
    headers = {"X-Portal-User-Id": portal_user_id} if portal_user_id else {}
    response = requests.get(url, params=params, headers=headers, timeout=180)
    response.raise_for_status()
    return response.text


def _probe_uploaded_file_session(
    base_url: str,
    conversation_id: str,
    workflow_id: int,
) -> tuple[bool, int, str]:
    try:
        response = requests.get(
            base_url.rstrip("/") + "/documents",
            params={
                "workflow_id": workflow_id,
                "app_session_id": conversation_id,
                "chat_id": conversation_id,
            },
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        return False, 0, f"{type(exc).__name__}: {exc}"
    documents = body.get("documents") if isinstance(body, dict) else None
    if not isinstance(documents, list) or not documents:
        return False, 0, "no documents"
    return True, len(documents), ""


def _history_golden_acceptance(qid: str, answer: str) -> tuple[bool, str]:
    requirements = {
        "F01": (re.compile(r"242\.72\s*억원"), "missing 242.72억원"),
        "F02": (re.compile(r"29\.52\s*%"), "missing 29.52%"),
        "H02": (re.compile(r"242\.72\s*억원"), "missing 242.72억원"),
        "H03": (re.compile(r"29\.52\s*%"), "missing 29.52%"),
        "M02": (re.compile(r"242\.72\s*억원"), "missing 242.72억원"),
        "M03": (re.compile(r"29\.52\s*%"), "missing 29.52%"),
    }
    requirement = requirements.get(qid)
    if requirement is None:
        return True, ""
    for sentinel in P0G_FAIL_CLOSED_ANSWER_SENTINELS:
        if sentinel in answer:
            return False, f"fail-closed answer: {sentinel}"
    pattern, error = requirement
    return (True, "") if pattern.search(answer) else (False, error)


def _diff_question(qid: str, before: Path, after: Path) -> dict[str, Any]:
    before_sse = parse_sse_file(before / "sse" / f"{qid}.sse")
    after_sse = parse_sse_file(after / "sse" / f"{qid}.sse")
    before_trace = _read_json(before / "traces" / f"{qid}.json")
    after_trace = _read_json(after / "traces" / f"{qid}.json")
    fact_check = _fact_equivalent(before_trace, before_sse, after_trace, after_sse)
    answer_number_check = _answer_number_check(before_trace, before_sse, after_trace, after_sse)
    route_check = _route_equivalent(before_trace, after_trace)
    render_check = not before_sse.render_issues and not after_sse.render_issues
    checks = {
        "L0_sse": before_sse.error_count == after_sse.error_count == 0 and before_sse.done_count == after_sse.done_count == 1 and render_check,
        "L1_route": route_check["pass"],
        "L2_fact": fact_check["pass"],
        "L3_answer": answer_number_check["pass"],
        "L4_numbers": answer_number_check["pass"],
    }
    detail: dict[str, Any] = {}
    if not render_check:
        detail["render_issues"] = {
            "before": before_sse.render_issues,
            "after": after_sse.render_issues,
        }
    if not checks["L1_route"]:
        detail["route_diff"] = route_check["detail"]
    elif route_check["detail"]:
        detail["route_note"] = route_check["detail"]
    if not checks["L2_fact"]:
        detail.update(fact_check["detail"])
    if _normalize_markdown(before_sse.answer_markdown) != _normalize_markdown(after_sse.answer_markdown):
        detail["answer_text_changed"] = True
    if not answer_number_check["pass"]:
        detail.update(answer_number_check["detail"])
    return {"qid": qid, "pass": all(checks.values()), "checks": checks, "detail": detail}


def _route_equivalent(before_trace: dict[str, Any], after_trace: dict[str, Any]) -> dict[str, Any]:
    before_core = _route_payload(before_trace, include_tools=False)
    after_core = _route_payload(after_trace, include_tools=False)
    before_tools = _tool_names(before_trace)
    after_tools = _tool_names(after_trace)
    detail: dict[str, Any] = {}
    if before_tools != after_tools:
        detail["tool_plan_changed_allowed"] = {"before": before_tools, "after": after_tools}
    if before_core != after_core:
        detail["route_core_diff"] = _json_diff(before_core, after_core)
        return {"pass": False, "detail": detail}
    return {"pass": True, "detail": detail}


def _route_payload(trace: dict[str, Any], *, include_tools: bool = True) -> dict[str, Any]:
    result = trace.get("result") if isinstance(trace.get("result"), dict) else {}
    payload = {
        "decomposition": _route_decomposition(result.get("decomposition")),
        "router_diagnostics": _route_diagnostics(result.get("router_diagnostics")),
    }
    if include_tools:
        payload["tool_names"] = _tool_names(trace)
    return _normalize_json(payload)


def _tool_names(trace: dict[str, Any]) -> list[str]:
    result = trace.get("result") if isinstance(trace.get("result"), dict) else {}
    calls = result.get("tool_calls") if isinstance(result.get("tool_calls"), list) else []
    return [str(call.get("tool")) for call in calls if isinstance(call, dict) and call.get("tool")]


def _route_decomposition(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    items: list[dict[str, Any]] = []
    for route in value:
        if not isinstance(route, dict):
            continue
        items.append(
            {
                key: route.get(key)
                for key in ("intent", "bq", "question", "sources", "filters", "brands", "status", "max_steps")
                if key in route
            }
        )
    return items


def _route_diagnostics(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {key: value.get(key) for key in ("mode", "deterministic_execution", "fallback_used") if key in value}


def _fact_payload(trace: dict[str, Any], parsed: Any) -> dict[str, Any]:
    result = trace.get("result") if isinstance(trace.get("result"), dict) else {}
    markdown = result.get("markdown_response") if isinstance(result.get("markdown_response"), dict) else {}
    return _normalize_json(
        {
            "fact_md": _normalize_markdown(str(markdown.get("fact_md") or "")),
            "sources_md": _normalize_markdown(str(markdown.get("sources_md") or "")),
            "notice_md": _normalize_markdown(str(markdown.get("notice_md") or "")),
        }
    )


def _fact_equivalent(before_trace: dict[str, Any], before_sse: Any, after_trace: dict[str, Any], after_sse: Any) -> dict[str, Any]:
    before_core = _fact_payload(before_trace, before_sse)
    after_core = _fact_payload(after_trace, after_sse)
    detail: dict[str, Any] = {}
    fact_core_equal = before_core == after_core
    if before_core != after_core:
        detail["fact_diff"] = _json_diff(before_core, after_core)
        surface_result = _surface_number_equivalence(before_sse, after_sse)
        detail["fact_structure_changed_allowed"] = surface_result["detail"]
        fact_core_equal = surface_result["pass"]
    chart_result = _chart_equivalence(before_sse.charts, after_sse.charts)
    if chart_result["common_changed"]:
        detail["chart_diff"] = chart_result
    if chart_result["presence_changed"]:
        detail["chart_presence_changed"] = chart_result["presence_changed"]
    return {"pass": fact_core_equal and not chart_result["common_changed"], "detail": detail}


def _surface_number_equivalence(before_sse: Any, after_sse: Any) -> dict[str, Any]:
    before_numbers = _number_multiset(_normalize_markdown(before_sse.answer_markdown))
    after_numbers = _number_multiset(_normalize_markdown(after_sse.answer_markdown))
    detail = {"before_surface_numbers": before_numbers, "after_surface_numbers": after_numbers}
    return {"pass": before_numbers == after_numbers, "detail": detail}


def _number_multiset(text: str) -> list[str]:
    return sorted(_number_token(item) for item in extract_numeric_mentions(text))


def _chart_equivalence(before_charts: tuple[dict[str, object], ...], after_charts: tuple[dict[str, object], ...]) -> dict[str, object]:
    before = _chart_index(before_charts)
    after = _chart_index(after_charts)
    before_keys = set(before)
    after_keys = set(after)
    common_changed = {
        key: {"before": before[key], "after": after[key]}
        for key in sorted(before_keys & after_keys)
        if before[key] != after[key]
    }
    presence_changed = {
        "before_only": sorted(before_keys - after_keys),
        "after_only": sorted(after_keys - before_keys),
    }
    presence_changed = {key: value for key, value in presence_changed.items() if value}
    return {"common_changed": common_changed, "presence_changed": presence_changed}


def _chart_index(charts: tuple[dict[str, object], ...]) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for chart in charts:
        normalized = _normalize_json(chart)
        if not isinstance(normalized, dict):
            continue
        key = "|".join(
            str(normalized.get(part) or "")
            for part in ("title", "source", "type", "unit")
        )
        indexed[key] = normalized
    return indexed


def _normalize_markdown(text: str) -> str:
    clean = TIMING_BLOCK.sub("", text.replace("\r\n", "\n")).strip()
    clean = re.sub(r"(?m)^event: timing.*$", "", clean)
    clean = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", clean)
    return re.sub(r"\n{3,}", "\n\n", clean).strip()


def _normalize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_json(val) for key, val in sorted(value.items()) if key not in VOLATILE_KEYS}
    if isinstance(value, list | tuple):
        return [_normalize_json(item) for item in value]
    if isinstance(value, str):
        return _normalize_markdown(value)
    return value


def _answer_number_check(before_trace: dict[str, Any], before_parsed: Any, after_trace: dict[str, Any], after_parsed: Any) -> dict[str, Any]:
    if _has_fact_support(after_trace, after_parsed):
        ungrounded = _ungrounded_answer_numbers(after_trace, after_parsed)
        return {"pass": ungrounded == [], "detail": {"ungrounded_numbers": ungrounded} if ungrounded else {}}
    new_numbers = _new_numbers(before_parsed.answer_markdown, after_parsed.answer_markdown)
    return {"pass": new_numbers == [], "detail": {"new_numbers_without_trace": new_numbers} if new_numbers else {}}


def _has_fact_support(trace: dict[str, Any], parsed: Any) -> bool:
    return bool(extract_numeric_mentions(_support_text(trace, parsed)))


def _ungrounded_answer_numbers(trace: dict[str, Any], parsed: Any) -> list[str]:
    answer_tokens = {_number_token(item) for item in extract_numeric_mentions(_normalize_markdown(parsed.answer_markdown))}
    support_tokens = {_number_token(item) for item in extract_numeric_mentions(_support_text(trace, parsed))}
    return sorted(answer_tokens - support_tokens)


def _new_numbers(before: str, after: str) -> list[str]:
    before_tokens = {_number_token(item) for item in extract_numeric_mentions(_normalize_markdown(before))}
    after_tokens = {_number_token(item) for item in extract_numeric_mentions(_normalize_markdown(after))}
    return sorted(after_tokens - before_tokens)


def _support_text(trace: dict[str, Any], parsed: Any) -> str:
    result = trace.get("result") if isinstance(trace.get("result"), dict) else {}
    markdown = result.get("markdown_response") if isinstance(result.get("markdown_response"), dict) else {}
    return "\n".join(
        (
            _normalize_markdown(str(markdown.get("fact_md") or "")),
            _normalize_markdown(str(markdown.get("sources_md") or "")),
            _normalize_markdown(str(markdown.get("notice_md") or "")),
            "\n".join(str(value) for value in markdown.get("allowed_numbers", ()) if value is not None),
            _normalize_markdown(str(parsed.sources or "")),
            json.dumps(parsed.charts, ensure_ascii=False, sort_keys=True, default=str),
        )
    )


def _number_token(item: Any) -> str:
    return f"{item.raw}|{item.unit}|{item.value:g}"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _json_diff(before: Any, after: Any) -> list[str]:
    return _text_diff(json.dumps(before, ensure_ascii=False, indent=2), json.dumps(after, ensure_ascii=False, indent=2))


def _text_diff(before: str, after: str) -> list[str]:
    return list(difflib.unified_diff(before.splitlines(), after.splitlines(), fromfile="before", tofile="after", lineterm=""))[:200]


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [f"# Parity Report", "", f"status: {report['status']}", "", "| ID | L0 | L1 | L2 | L3 | L4 |", "| --- | --- | --- | --- | --- | --- |"]
    for item in report["questions"]:
        checks = item["checks"]
        lines.append(
            f"| {item['qid']} | {_mark(checks['L0_sse'])} | {_mark(checks['L1_route'])} | {_mark(checks['L2_fact'])} | {_mark(checks['L3_answer'])} | {_mark(checks['L4_numbers'])} |"
        )
    return "\n".join(lines) + "\n"


def _mark(value: bool) -> str:
    return "PASS" if value else "FAIL"


def _capture_questions(name: str) -> tuple[tuple[str, str], ...]:
    if name == "channel":
        return CHANNEL_PARAPHRASE_QUESTIONS
    if name == "fresh":
        return FRESH_GOLDEN_QUESTIONS
    if name == "history":
        return HISTORY_GOLDEN_QUESTIONS
    if name == "mode-transition":
        return MODE_TRANSITION_GOLDEN_QUESTIONS
    return QUESTIONS


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture and diff jw-chat parity baselines.")
    sub = parser.add_subparsers(dest="command", required=True)
    capture_parser = sub.add_parser("capture")
    capture_parser.add_argument("--out-dir", type=Path, required=True)
    capture_parser.add_argument("--external-mode", default="live")
    capture_parser.add_argument("--base-url", help="Capture deployed /chat/stream SSE instead of local service trace.")
    question_sets = ("core", "channel", "fresh", "history", "mode-transition")
    capture_parser.add_argument("--question-set", choices=question_sets, default="core")
    capture_parser.add_argument(
        "--conversation-id",
        help="Reuse one session across capture questions. Required to reproduce history contamination against an existing uploaded-file session.",
    )
    p0g_parser = sub.add_parser("capture-p0g")
    p0g_parser.add_argument("--out-dir", type=Path, required=True)
    p0g_parser.add_argument("--external-mode", default="live")
    p0g_parser.add_argument("--base-url", help="Deployed /chat/stream base URL.")
    p0g_parser.add_argument(
        "--portal-equivalent",
        action="store_true",
        help="Declare that the supplied endpoint and payload path match the portal path under release review.",
    )
    p0g_parser.add_argument(
        "--portal-user-id",
        help="Portal user ID forwarded as X-Portal-User-Id for portal-equivalent evidence.",
    )
    p0g_parser.add_argument(
        "--history-conversation-id",
        help="Existing session containing an uploaded file and unrelated prior turns.",
    )
    p0g_parser.add_argument(
        "--file-base-url",
        help="235 file bridge base URL used to verify that the history session owns documents.",
    )
    p0g_parser.add_argument("--file-workflow-id", type=int, default=301)
    p0g_parser.add_argument(
        "--max-general-elapsed-ms",
        type=float,
        default=10_000.0,
        help="Fast-path budget for the six general golden turns (default: 10000).",
    )
    diff_parser = sub.add_parser("diff")
    diff_parser.add_argument("--before", type=Path, required=True)
    diff_parser.add_argument("--after", type=Path, required=True)
    diff_parser.add_argument("--report-dir", type=Path, required=True)
    diff_parser.add_argument("--question-set", choices=question_sets, default="core")
    multi_parser = sub.add_parser("diff-multi")
    multi_parser.add_argument("--baseline-root", type=Path, required=True)
    multi_parser.add_argument("--after", type=Path, required=True)
    multi_parser.add_argument("--report-dir", type=Path, required=True)
    self_parser = sub.add_parser("self-test")
    self_parser.add_argument("--capture-dir", type=Path, required=True)
    self_parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "capture":
        conversation_id = args.conversation_id
        if args.question_set in {"history", "mode-transition"} and not conversation_id:
            conversation_id = f"parity-{args.question_set}-{uuid4().hex}"
        return capture(
            args.out_dir,
            args.external_mode,
            args.base_url,
            _capture_questions(args.question_set),
            conversation_id,
        )
    if args.command == "capture-p0g":
        return capture_p0g_suite(
            args.out_dir,
            args.external_mode,
            args.base_url,
            history_conversation_id=args.history_conversation_id,
            max_general_elapsed_ms=args.max_general_elapsed_ms,
            portal_equivalent=args.portal_equivalent,
            portal_user_id=args.portal_user_id,
            file_base_url=args.file_base_url,
            file_workflow_id=args.file_workflow_id,
        )
    if args.command == "diff":
        return diff_captures(
            args.before,
            args.after,
            args.report_dir,
            _capture_questions(args.question_set),
        )
    if args.command == "diff-multi":
        return diff_multi_baseline(args.baseline_root, args.after, args.report_dir)
    if args.command == "self-test":
        return self_test(args.capture_dir, args.out_dir)
    raise AssertionError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
