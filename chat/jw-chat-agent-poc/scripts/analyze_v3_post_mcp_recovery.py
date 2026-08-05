# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys


FORMULAIC = frozenset(
    {
        "요청한 조회 중 일부를 확인하지 못했습니다.",
        "근거와 결속되지 않은 일부 표현은 답변에서 제외했습니다.",
    }
)
NUMBER = re.compile(r"(?<![A-Za-z0-9])[-+]?\d[\d,]*(?:\.\d+)?(?:%|억|만|천)?")
ROOT = Path(sys.argv[1])
EVIDENCE = ROOT / "evidence"
PRIOR_C3 = Path(sys.argv[2]) if len(sys.argv) > 2 else None
NONDETERMINISM_INDICES = frozenset({40, 51, 64, 77, 78, 86, 95, 114, 170, 223})
RECOVERY_FLOOR = datetime.fromisoformat("2026-08-05T10:56:03+00:00")


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    paths = sorted((ROOT / "remeasure").glob("*_run*.json"))
    all_rows = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    for row in all_rows:
        assert_measurement_after_recovery(row)
    rows = [
        row
        for row in all_rows
        if int(row["measurement"]["index"]) in NONDETERMINISM_INDICES
        and row["measurement"]["mode"] == "nondeterminism"
    ]
    if len(rows) != 50:
        raise RuntimeError(f"expected 50 raw records, found {len(rows)}")
    by_index: dict[int, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_index[int(row["measurement"]["index"])].append(row)
    if any(len(group) != 5 for group in by_index.values()) or len(by_index) != 10:
        raise RuntimeError("expected exactly 10 questions with five runs each")

    run_index(rows)
    selection_variance, evidence_variance, fusion_variance = variance_by_stage(by_index)
    limitations_variance(by_index)
    typed_failure_stability(by_index)
    unstable_values = value_stability(by_index)
    latency_token_distribution(by_index)
    targeted_remeasure_outputs(all_rows)
    measurement_timestamps(all_rows)
    summary = {
        "selection_variance_question_count": selection_variance,
        "evidence_variance_question_count": evidence_variance,
        "fusion_variance_question_count": fusion_variance,
        "typed_fake_brand_accepted_runs": sum(
            bool(accepted_claims(row)) for row in by_index[95]
        ),
        "typed_086_accepted_runs": sum(
            bool(accepted_claims(row)) for row in by_index[86]
        ),
        "unstable_internal_metric_key_count": unstable_values,
        "e2e_p95_ms": percentile(
            [float(row["measurement"]["end_to_end_latency_ms"]) for row in rows],
            0.95,
        ),
        "run_count": len(rows),
    }
    (EVIDENCE / "analysis_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if (
        summary["typed_fake_brand_accepted_runs"]
        or summary["typed_086_accepted_runs"]
        or unstable_values
    ):
        return 42
    return 0


def assert_measurement_after_recovery(row: Mapping[str, object]) -> None:
    measurement = row.get("measurement")
    if not isinstance(measurement, Mapping):
        raise RuntimeError("raw record is missing measurement metadata")
    for field in ("started_at_utc", "completed_at_utc"):
        raw_value = str(measurement.get(field) or "")
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        if parsed <= RECOVERY_FLOOR:
            raise RuntimeError(
                f"{field} is not after MCP recovery: {raw_value} <= "
                f"{RECOVERY_FLOOR.isoformat()}"
            )


def run_index(rows: list[dict[str, object]]) -> None:
    lines = [
        "index\trun\tstarted_at_utc\tcompleted_at_utc\tselected_tools_hash\tevidence_bundle_hash\tweb_result_hash\tanswer_hash\taccepted_claim_count\tselection_status\texecution_status\tfusion_status"
    ]
    for row in sorted(rows, key=run_key):
        measurement = row["measurement"]
        hashes = row["hashes"]
        claims = accepted_claims(row)
        lines.append(
            "\t".join(
                [
                    str(measurement["index"]),
                    str(measurement["run"]),
                    str(measurement["started_at_utc"]),
                    str(measurement["completed_at_utc"]),
                    str(hashes["selected_tools_hash"]),
                    str(hashes["evidence_bundle_hash"]),
                    str(hashes["web_result_hash"]),
                    stable_hash(claims),
                    str(len(claims)),
                    str(row["selection"]["status"]),
                    str(row["execution"]["status"]),
                    str(row["fusion"]["status"]),
                ]
            )
        )
    write_text("nondeterminism_after_recovery.tsv", lines)


def variance_by_stage(
    by_index: Mapping[int, list[dict[str, object]]],
) -> tuple[int, int, int]:
    lines = [
        "index\tquestion\tselected_tools_hash_unique\tevidence_bundle_hash_unique\tweb_result_hash_unique\tanswer_hash_unique\tclaim_count_min\tclaim_count_max\tclaim_count_unique\tselection_varies\tevidence_varies_with_same_selection\tfusion_varies_with_same_bundle\tsame_bundle_different_answer_pairs"
    ]
    selection_variance_questions = 0
    evidence_variance_questions = 0
    fusion_variance_questions = 0
    for index, group in sorted(by_index.items()):
        selected = {str(row["hashes"]["selected_tools_hash"]) for row in group}
        evidence = {str(row["hashes"]["evidence_bundle_hash"]) for row in group}
        web = {str(row["hashes"]["web_result_hash"]) for row in group}
        answers = {stable_hash(accepted_claims(row)) for row in group}
        claim_counts = [len(accepted_claims(row)) for row in group]
        evidence_varies_same_selection = any(
            len({str(row["hashes"]["evidence_bundle_hash"]) for row in selected_group}) > 1
            for selected_group in group_by(group, lambda row: str(row["hashes"]["selected_tools_hash"])).values()
        )
        answer_pairs: list[str] = []
        for evidence_group in group_by(group, lambda row: str(row["hashes"]["evidence_bundle_hash"])).values():
            for left_pos, left in enumerate(evidence_group):
                for right in evidence_group[left_pos + 1 :]:
                    if stable_hash(accepted_claims(left)) != stable_hash(accepted_claims(right)):
                        answer_pairs.append(
                            f"run{left['measurement']['run']}-run{right['measurement']['run']}"
                        )
        fusion_varies = bool(answer_pairs)
        selection_variance_questions += len(selected) > 1
        evidence_variance_questions += evidence_varies_same_selection
        fusion_variance_questions += fusion_varies
        lines.append(
            "\t".join(
                [
                    str(index),
                    str(group[0]["measurement"]["question"]).replace("\t", " "),
                    str(len(selected)),
                    str(len(evidence)),
                    str(len(web)),
                    str(len(answers)),
                    str(min(claim_counts)),
                    str(max(claim_counts)),
                    ",".join(str(value) for value in sorted(set(claim_counts))),
                    str(len(selected) > 1).lower(),
                    str(evidence_varies_same_selection).lower(),
                    str(fusion_varies).lower(),
                    ",".join(answer_pairs) or "NONE",
                ]
            )
        )
    lines.extend(
        [
            "",
            f"selection_variance_question_count={selection_variance_questions}",
            f"evidence_variance_question_count={evidence_variance_questions}",
            f"fusion_variance_question_count={fusion_variance_questions}",
            "before_selection_variance_question_count=4",
            "before_evidence_variance_question_count=5",
            "before_fusion_variance_question_count=6",
        ]
    )
    write_text("variance_after_recovery.tsv", lines)
    return (
        selection_variance_questions,
        evidence_variance_questions,
        fusion_variance_questions,
    )


def limitations_variance(by_index: Mapping[int, list[dict[str, object]]]) -> None:
    lines = ["LIMITATIONS VARIANCE", "rule=exact string membership only", ""]
    for index, group in sorted(by_index.items()):
        labels = []
        lines.append(f"index={index} question={group[0]['measurement']['question']}")
        for row in sorted(group, key=run_key):
            limitations = answer_limitations(row)
            if not limitations:
                label = "none"
            elif all(text in FORMULAIC for text in limitations):
                label = "formulaic_only"
            else:
                label = "has_specific"
            labels.append(label)
            lines.append(
                f"  run={row['measurement']['run']} label={label} limitations={json.dumps(limitations, ensure_ascii=False)}"
            )
        counts = Counter(labels)
        lines.append(f"  distribution={dict(sorted(counts.items()))}")
        lines.append(f"  formulaic_specific_mixed={set(labels) >= {'formulaic_only', 'has_specific'}}")
        lines.append("")
    write_text("limitations_variance.txt", lines)


def typed_failure_stability(by_index: Mapping[int, list[dict[str, object]]]) -> None:
    lines = ["TYPED FAILURE STABILITY", ""]
    for index in (95, 86):
        group = sorted(by_index[index], key=run_key)
        lines.append(f"index={index} question={group[0]['measurement']['question']}")
        for row in group:
            lines.append(
                "  run={run} selected={selected} execution={execution} fusion={fusion} accepted_claim_count={claims} limitations={limitations}".format(
                    run=row["measurement"]["run"],
                    selected=len(row["selection"]["choices"]),
                    execution=row["execution"]["status"],
                    fusion=row["fusion"]["status"],
                    claims=len(accepted_claims(row)),
                    limitations=json.dumps(answer_limitations(row), ensure_ascii=False),
                )
            )
        answered = sum(bool(accepted_claims(row)) for row in group)
        lines.append(f"  answered_runs={answered}/5")
        lines.append("")
    lines.append(
        "hard_stop_fake_brand_triggered="
        + str(any(accepted_claims(row) for row in by_index[95])).lower()
    )
    write_text("typed_failure_stability_after.txt", lines)


def value_stability(by_index: Mapping[int, list[dict[str, object]]]) -> int:
    lines = [
        "VALUE STABILITY",
        "scope=internal evidence facts only; web facts listed separately and not treated as mart values",
        "comparison_key=tool_name|entity|metric|period|unit|view|market",
        "",
    ]
    unstable = 0
    for index, group in sorted(by_index.items()):
        values: dict[str, dict[int, list[object]]] = defaultdict(lambda: defaultdict(list))
        claim_numbers: dict[int, list[str]] = {}
        for row in group:
            run = int(row["measurement"]["run"])
            claim_numbers[run] = [
                literal
                for claim in accepted_claims(row)
                for literal in NUMBER.findall(str(claim.get("text") or ""))
            ]
            for fact in row["execution"]["facts"]:
                if not isinstance(fact, Mapping) or "metric" not in fact:
                    continue
                key = "|".join(
                    str(fact.get(field) or "")
                    for field in ("tool_name", "entity", "metric", "period", "unit", "view", "market")
                )
                value, value_path = projected_metric_value(fact)
                if value_path is None:
                    continue
                values[f"{key}|value_path={value_path}"][run].append(value)
        lines.append(f"index={index} question={group[0]['measurement']['question']}")
        for key, per_run in sorted(values.items()):
            unique = {canonical_json(value) for run_values in per_run.values() for value in run_values}
            is_unstable = len(unique) > 1
            unstable += is_unstable
            lines.append(
                f"  key={key} unique_values={len(unique)} unstable={str(is_unstable).lower()} per_run={json.dumps(per_run, ensure_ascii=False, sort_keys=True)}"
            )
        lines.append(
            f"  accepted_claim_numeric_literals_by_run={json.dumps(claim_numbers, ensure_ascii=False, sort_keys=True)}"
        )
        lines.append("")
    lines.append(f"unstable_internal_metric_key_count={unstable}")
    write_text("value_stability_after.txt", lines)
    return unstable


def projected_metric_value(fact: Mapping[str, object]) -> tuple[object, str | None]:
    if "value" in fact:
        return fact.get("value"), "fact.value"
    raw_result = mapping(fact.get("raw_result"))
    render_data = mapping(raw_result.get("render_data"))
    metric = str(fact.get("metric") or "")
    metric_fields = {
        "growth_contribution": "growth_contribution_pct",
        "hhi": "hhi_recent",
        "market_size": "market_size_recent_krw",
        "rank": "rank",
        "sales": "sales_krw",
        "share": "ms_recent_pct",
    }
    field = metric_fields.get(metric, "value")
    if field in render_data:
        return render_data.get(field), f"raw_result.render_data.{field}"
    return None, None


def latency_token_distribution(by_index: Mapping[int, list[dict[str, object]]]) -> None:
    lines = ["LATENCY AND TOKEN DISTRIBUTION", "percentiles=nearest-rank", ""]
    category_values: dict[str, list[dict[str, object]]] = {"internal": [], "web_dependent": []}
    web_indices = {77, 51}
    for index, group in sorted(by_index.items()):
        lines.append(f"index={index} question={group[0]['measurement']['question']}")
        for row in sorted(group, key=run_key):
            token = token_record(row)
            record = {
                "run": row["measurement"]["run"],
                "e2e_ms": row["measurement"]["end_to_end_latency_ms"],
                "selection_ms": row["latency"]["selection_ms"],
                "execution_ms": row["latency"]["execution_ms"],
                "web_ms": row["latency"]["web_ms"],
                "fusion_ms": row["latency"]["fusion_ms"],
                **token,
            }
            category_values["web_dependent" if index in web_indices else "internal"].append(record)
            lines.append("  " + json.dumps(record, ensure_ascii=False, sort_keys=True))
        e2e = [float(row["measurement"]["end_to_end_latency_ms"]) for row in group]
        lines.append(
            f"  e2e_p50_ms={percentile(e2e, 0.50)} e2e_p95_ms={percentile(e2e, 0.95)} e2e_max_ms={max(e2e):.3f}"
        )
        lines.append("")
    for category, records in category_values.items():
        e2e = [float(record["e2e_ms"]) for record in records]
        lines.append(
            f"category={category} count={len(records)} e2e_p50_ms={percentile(e2e, 0.50)} e2e_p95_ms={percentile(e2e, 0.95)} e2e_max_ms={max(e2e):.3f}"
        )
    all_rows = [row for group in by_index.values() for row in group]
    all_e2e = [float(row["measurement"]["end_to_end_latency_ms"]) for row in all_rows]
    lines.extend(
        [
            "",
            f"overall_count={len(all_e2e)}",
            f"overall_e2e_p50_ms={percentile(all_e2e, 0.50)}",
            f"overall_e2e_p95_ms={percentile(all_e2e, 0.95)}",
            "before_e2e_p95_ms=47311.890",
        ]
    )
    write_text("latency_after_recovery.txt", lines)


def targeted_remeasure_outputs(rows: Sequence[dict[str, object]]) -> None:
    selected = sorted(
        (
            row
            for row in rows
            if row["measurement"]["mode"] == "targeted"
            or int(row["measurement"]["index"]) == 77
        ),
        key=run_key,
    )
    candidates = json.loads((ROOT / "candidate_selection.json").read_text(encoding="utf-8"))
    criteria = str(candidates["criteria"])
    lines = [
        "selection_criteria\t" + criteria,
        "index\trun\tquestion\tbefore_claims\tbefore_web_facts\tafter_claims\tafter_limitations\tselected_tools\tevidence_sources\tstarted_at_utc\tcompleted_at_utc",
    ]
    material_path = ROOT / "answer_fitness" / "remeasure_material.jsonl"
    material_path.parent.mkdir(parents=True, exist_ok=True)
    material_rows: list[str] = []
    q77_q103_lines = [
        "Q77 / Q103 BEFORE-AFTER",
        "fitness_grade=NOT_PERFORMED",
        "",
    ]
    for row in selected:
        index = int(row["measurement"]["index"])
        run = int(row["measurement"]["run"])
        if index == 77 and run > 3:
            continue
        before = prior_row(index)
        claims = accepted_claims(row)
        limitations = answer_limitations(row)
        sources = evidence_sources(row)
        tools = [str(choice.get("name") or "") for choice in row["selection"]["choices"]]
        lines.append(
            "\t".join(
                [
                    str(index),
                    str(run),
                    clean_tsv(row["measurement"]["question"]),
                    str(len(prior_claims(before))),
                    str(len(prior_web_facts(before))),
                    str(len(claims)),
                    clean_tsv(json.dumps(limitations, ensure_ascii=False)),
                    clean_tsv(json.dumps(tools, ensure_ascii=False)),
                    clean_tsv(json.dumps(sources, ensure_ascii=False)),
                    str(row["measurement"]["started_at_utc"]),
                    str(row["measurement"]["completed_at_utc"]),
                ]
            )
        )
        material_rows.append(
            json.dumps(
                {
                    "index": index,
                    "run": run,
                    "question": row["measurement"]["question"],
                    "claims": claims,
                    "limitations": limitations,
                    "selected_tools": tools,
                    "evidence_sources": sources,
                    "fitness_grade": "NOT_PERFORMED",
                    "started_at_utc": row["measurement"]["started_at_utc"],
                    "completed_at_utc": row["measurement"]["completed_at_utc"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        if index in {77, 103}:
            prior_web_count = len(prior_web_facts(before))
            prior_selection = mapping(before.get("selection"))
            prior_choices = prior_selection.get("choices")
            prior_tool_names = [
                str(choice.get("name") or "")
                for choice in prior_choices
                if isinstance(choice, Mapping)
            ] if isinstance(prior_choices, Sequence) else []
            prior_execution = mapping(before.get("execution_before_web"))
            prior_fact_sources = [
                str(fact.get("tool_name") or "")
                for fact in prior_execution.get("facts", [])
                if isinstance(fact, Mapping)
            ]
            after_web_statuses = [
                str(call.get("status") or "")
                for call in mapping(row.get("web")).get("calls", [])
                if isinstance(call, Mapping)
            ]
            route_kind = "mcp" if any(
                name.startswith(("hira_", "mfds_")) for name in tools
            ) else "internal_or_other"
            q77_q103_lines.extend(
                [
                    f"index={index} run={run}",
                    f"question={row['measurement']['question']}",
                    f"before_selected_tools={json.dumps(prior_tool_names, ensure_ascii=False)}",
                    f"before_execution_status={prior_execution.get('status')}",
                    f"before_execution_fact_sources={json.dumps(prior_fact_sources, ensure_ascii=False)}",
                    f"before_claims={json.dumps(prior_claims(before), ensure_ascii=False, sort_keys=True)}",
                    f"before_web_fact_count={prior_web_count}",
                    f"after_selected_tools={json.dumps(tools, ensure_ascii=False)}",
                    f"after_route_kind={route_kind}",
                    f"after_claims={json.dumps(claims, ensure_ascii=False, sort_keys=True)}",
                    f"after_limitations={json.dumps(limitations, ensure_ascii=False)}",
                    f"after_evidence_sources={json.dumps(sources, ensure_ascii=False)}",
                    f"after_web_call_statuses={json.dumps(after_web_statuses, ensure_ascii=False)}",
                    f"started_at_utc={row['measurement']['started_at_utc']}",
                    f"completed_at_utc={row['measurement']['completed_at_utc']}",
                    "",
                ]
            )
    write_text("hira_nedrug_questions_remeasure.tsv", lines)
    write_text("q77_q103_before_after.txt", q77_q103_lines)
    material_path.write_text("\n".join(material_rows) + "\n", encoding="utf-8")


def measurement_timestamps(rows: Sequence[dict[str, object]]) -> None:
    ordered = sorted(rows, key=run_key)
    lines = [
        "MEASUREMENT TIMESTAMPS",
        f"recovery_floor_utc={RECOVERY_FLOOR.isoformat().replace('+00:00', 'Z')}",
        f"record_count={len(ordered)}",
        "all_after_recovery=true",
        "",
        "mode\tindex\trun\tstarted_at_utc\tcompleted_at_utc",
    ]
    for row in ordered:
        measurement = row["measurement"]
        lines.append(
            "\t".join(
                [
                    str(measurement["mode"]),
                    str(measurement["index"]),
                    str(measurement["run"]),
                    str(measurement["started_at_utc"]),
                    str(measurement["completed_at_utc"]),
                ]
            )
        )
    write_text("measurement_timestamps.txt", lines)


def evidence_sources(row: Mapping[str, object]) -> list[str]:
    sources = {
        "web" for fact in row["web"]["web_facts"] if isinstance(fact, Mapping)
    }
    for fact in row["execution"]["facts"]:
        if not isinstance(fact, Mapping):
            continue
        tool_name = str(fact.get("tool_name") or "unknown")
        sources.add(f"internal_or_mcp:{tool_name}")
    return sorted(sources)


def prior_row(index: int) -> Mapping[str, object]:
    if PRIOR_C3 is None:
        return {}
    path = PRIOR_C3 / f"{index:03d}.json"
    if not path.exists():
        return {}
    return mapping(json.loads(path.read_text(encoding="utf-8")))


def prior_claims(row: Mapping[str, object]) -> list[object]:
    fusion = mapping(row.get("fusion_after"))
    answer = mapping(mapping(fusion.get("validated_answer")).get("answer"))
    claims = answer.get("claims")
    return list(claims) if isinstance(claims, Sequence) and not isinstance(claims, (str, bytes)) else []


def prior_web_facts(row: Mapping[str, object]) -> list[object]:
    web = mapping(row.get("web_augmentation"))
    facts = web.get("web_facts")
    if isinstance(facts, Sequence) and not isinstance(facts, (str, bytes)):
        return list(facts)
    count = web.get("web_fact_count")
    if isinstance(count, int) and count > 0:
        return [None] * count
    return []


def clean_tsv(value: object) -> str:
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def token_record(row: Mapping[str, object]) -> dict[str, object]:
    selection_usage = mapping(row["selection"].get("raw_response_json")).get("usage")
    fusion_usage = mapping(mapping(row["fusion"].get("provider")).get("usage"))
    selection = normalize_usage(selection_usage)
    fusion = normalize_usage(fusion_usage)
    return {
        "selection_input_tokens": selection[0],
        "selection_output_tokens": selection[1],
        "selection_total_tokens": selection[2],
        "fusion_input_tokens": fusion[0],
        "fusion_output_tokens": fusion[1],
        "fusion_total_tokens": fusion[2],
    }


def normalize_usage(value: object) -> tuple[int | None, int | None, int | None]:
    usage = mapping(value)
    prompt = first_int(usage, "prompt_tokens", "promptTokenCount", "input_tokens", "inputTokens")
    completion = first_int(usage, "completion_tokens", "candidatesTokenCount", "output_tokens", "outputTokens")
    total = first_int(usage, "total_tokens", "totalTokenCount", "totalTokens")
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    return prompt, completion, total


def first_int(value: Mapping[str, object], *keys: str) -> int | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, (int, float)):
            return int(item)
    return None


def accepted_claims(row: Mapping[str, object]) -> list[dict[str, object]]:
    validated = mapping(mapping(row.get("fusion")).get("validated_answer"))
    claims = mapping(validated.get("answer")).get("claims")
    if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes, bytearray)):
        return []
    return [dict(item) for item in claims if isinstance(item, Mapping)]


def answer_limitations(row: Mapping[str, object]) -> list[str]:
    fusion = mapping(row.get("fusion"))
    if fusion.get("status") == "typed_failure":
        value = fusion.get("limitations")
    else:
        validated = mapping(fusion.get("validated_answer"))
        value = mapping(validated.get("answer")).get("limitations")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [str(item) for item in value]


def mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def group_by(rows: Sequence[dict[str, object]], key) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        result[str(key(row))].append(row)
    return result


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, int((len(ordered) * fraction) + 0.999999) - 1))
    return round(ordered[position], 3)


def run_key(row: Mapping[str, object]) -> tuple[int, int]:
    return int(row["measurement"]["index"]), int(row["measurement"]["run"])


def stable_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def write_text(name: str, lines: list[str]) -> None:
    (EVIDENCE / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
