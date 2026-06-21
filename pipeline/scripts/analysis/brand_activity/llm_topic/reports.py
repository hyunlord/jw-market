from __future__ import annotations

from collections.abc import Sequence

from .models import JsonValue


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[str | int | float]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    return "\n".join(lines)


def render_design(generated_at: str, market_stats: dict[str, dict[str, JsonValue]], call_plan: list[dict[str, JsonValue]]) -> str:
    market_rows = [
        [market, stats["row_count"], stats["brand_count"], stats["estimated_tokens"]]
        for market, stats in sorted(market_stats.items(), key=lambda item: str(item[0]))
    ]
    plan_rows = [
        [item["task"], item["serving_id"], item.get("atc4", ""), item.get("brand", ""), item["sample_rows"], item["estimated_input_tokens"]]
        for item in call_plan
    ]
    return "\n".join(
        [
            "# LLM_TOPIC_01_DESIGN",
            "",
            f"- Generated: {generated_at}",
            "- Scope: analysis-only PoC; no operational DB/cache/mart writes, no deploy, no push.",
            "- Architecture: two-stage GenOS assist, first market-common axis then brand-level shares on that fixed axis.",
            "- Privacy: raw `keyword_text` may be sent transiently to GenOS only during execution; docs/audit/cache persist hashes and aggregate metrics only.",
            "",
            "## Two-Stage Flow",
            "",
            "1. Market axis generation: for each ATC4, sample market rows across selected brands and ask GenOS for 5-8 reusable Korean topic axes with definitions and representative keywords.",
            "2. Brand share generation: for each brand, send the frozen market axis, alias-derived brand description, row metadata, and sampled messages; request primary-topic shares summing to 100% including 기타.",
            "3. Cache by task/model/prompt version/input hash; reruns reuse the same result unless nondeterminism measurement explicitly bypasses cache.",
            "",
            "## Input Columns",
            "",
            "`period_ym`, `visit_location`, `specialty`, `product_name`, `therapeutic_class`, `keyword_text`, `interest`, `prescription_frequency`, `prescription_evolution`, `abstract_lit`, `patient_lit`, `promotional_lit`, `stage_row_sha256`.",
            "",
            "## Sample Markets",
            "",
            markdown_table(["ATC4", "rows", "brands", "estimated text tokens"], market_rows),
            "",
            "## Planned GenOS Calls",
            "",
            markdown_table(["task", "serving", "ATC4", "brand", "sample rows", "estimated input tokens"], plan_rows),
            "",
            "## Large-Market Strategy",
            "",
            "- PoC uses stratified deterministic sampling rather than chunk-summary merge because the goal is prompt/cost/nondeterminism measurement, not full production coverage.",
            "- Production option: run stage 1 once per ATC4 from PL-approved axes, then refresh only changed data snapshots.",
            "- A02B2 is left as a scale-up candidate because it is the largest market and would materially raise the call payload.",
            "",
            "## Output Schemas",
            "",
            "Market axis JSON: `{atc4, axis_version, topics:[{topic_id,label,definition,keywords}], etc}`.",
            "",
            "Brand share JSON: `{brand, atc4, axis_version, denominator, topic_shares:[{topic_id,label,share_pct,row_count}], etc_pct, cross_insights, evidence_note}`.",
            "",
            "## Cache Key",
            "",
            "`{task}__serving-{model_serving_id}__{prompt_version}__{input_hash}` where input hash includes prompt version, axis version, row refs, stage hashes, metadata, and text hashes.",
            "",
            "## Denominator",
            "",
            "Brand shares use `brand_row_count_primary_topic`: each input row is assigned to one primary topic; 기타 absorbs rows outside the axis. This keeps brand x topic matrices comparable and forces total share to 100%.",
        ]
    ) + "\n"


def render_poc_result(
    generated_at: str,
    execution_status: str,
    call_logs: list[dict[str, JsonValue]],
    axis_results: dict[str, JsonValue],
    brand_results: dict[str, JsonValue],
    nondeterminism: dict[str, JsonValue],
    monthly_estimate: dict[str, JsonValue],
) -> str:
    call_rows = [
        [
            item["task"],
            item["serving_id"],
            item.get("atc4", ""),
            item.get("brand", ""),
            item["status"],
            item.get("latency_ms", "n/a"),
            item.get("estimated_input_tokens", "n/a"),
        ]
        for item in call_logs
    ]
    axis_rows = [[market, _topic_count(value), _status(value)] for market, value in sorted(axis_results.items())]
    brand_rows = [[key, _status(value), _share_summary(value)] for key, value in sorted(brand_results.items())]
    return "\n".join(
        [
            "# LLM_TOPIC_02_POC_RESULT",
            "",
            f"- Generated: {generated_at}",
            f"- Execution status: `{execution_status}`",
            "- External LLM real-call policy: bounded sample only, temperature 0, no raw prompt dump.",
            "- Auth note: this dev gateway accepted no-auth calls during PoC; production use still needs PL/GenOS bearer-policy confirmation.",
            "",
            "## Call Log",
            "",
            markdown_table(["task", "serving", "ATC4", "brand", "status", "latency ms", "est input tokens"], call_rows),
            "",
            "## Stage 1 Axis Results",
            "",
            markdown_table(["ATC4/model", "topic count", "status"], axis_rows),
            "",
            "## Stage 2 Brand Share Results",
            "",
            markdown_table(["brand key", "status", "top shares"], brand_rows),
            "",
            "## Nondeterminism",
            "",
            f"- Measurement: {nondeterminism.get('status', 'n/a')}",
            f"- Max share delta pp: {nondeterminism.get('max_share_delta_pp', 'n/a')}",
            f"- Note: {nondeterminism.get('note', '')}",
            "",
            "## Token, Latency, And Monthly Estimate",
            "",
            f"- Sample estimated input tokens: {monthly_estimate.get('sample_estimated_input_tokens', 'n/a')}",
            f"- Sample measured total tokens: {monthly_estimate.get('sample_measured_total_tokens', 'n/a')}",
            f"- Estimated monthly calls: {monthly_estimate.get('monthly_calls', 'n/a')}",
            f"- Estimated monthly input tokens: {monthly_estimate.get('monthly_input_tokens', 'n/a')}",
            f"- Cost: {monthly_estimate.get('cost_note', 'GenOS tariff unavailable')}",
        ]
    ) + "\n"


def render_comparison(
    generated_at: str,
    dictionary_baseline: list[dict[str, JsonValue]],
    execution_status: str,
    brand_results: dict[str, JsonValue],
) -> str:
    baseline_rows = [
        [item["atc4"], item["brand"], item["sample_rows"], item["top_dictionary_labels"]]
        for item in dictionary_baseline
    ]
    return "\n".join(
        [
            "# LLM_TOPIC_03_COMPARISON",
            "",
            f"- Generated: {generated_at}",
            f"- LLM execution status: `{execution_status}`",
            "",
            "## REDESIGN Dictionary Baseline",
            "",
            markdown_table(["ATC4", "brand", "sample rows", "top dictionary labels"], baseline_rows),
            "",
            "## Matrix Consistency",
            "",
            _matrix_consistency(execution_status, brand_results),
            "",
            "## Pros And Cons",
            "",
            "| Method | Strength | Risk |",
            "| --- | --- | --- |",
            "| REDESIGN dictionary | Deterministic, cheap, auditable, easy to version | Misses contextual paraphrases and brand-positioning nuance |",
            "| Pure LLM two-stage | Can compress nuanced message context into comparable axes | Requires token governance, privacy approval, cache discipline, and nondeterminism gating |",
            "| Hybrid | Keeps fixed axes while using LLM on cache misses or periodic calibration | More moving parts and needs PL-approved enum/version ownership |",
            "",
            "## Recommendation",
            "",
            "Use a hybrid operating model: PL-approved market axes as the durable matrix denominator, GenOS Flash/Pro for bounded recalibration or low-confidence rows, and cache every accepted result by input hash.",
        ]
    ) + "\n"


def _cell(value: str | int | float) -> str:
    return str(value).replace("\n", "<br>").replace("|", "\\|")


def _status(value: JsonValue) -> str:
    if isinstance(value, dict) and isinstance(value.get("status"), str):
        return value["status"]
    return "n/a"


def _topic_count(value: JsonValue) -> int:
    if not isinstance(value, dict):
        return 0
    topics = value.get("topics")
    if isinstance(topics, list):
        return len(topics)
    return 0


def _share_summary(value: JsonValue) -> str:
    if not isinstance(value, dict):
        return "n/a"
    shares = value.get("topic_shares")
    if not isinstance(shares, list):
        return "n/a"
    rendered: list[str] = []
    for item in shares[:3]:
        if isinstance(item, dict):
            rendered.append(f"{item.get('label')} {item.get('share_pct')}%")
    etc = value.get("etc_pct")
    if isinstance(etc, (int, float)):
        rendered.append(f"기타 {etc}%")
    return ", ".join(rendered) or "n/a"


def _matrix_consistency(execution_status: str, brand_results: dict[str, JsonValue]) -> str:
    if execution_status != "executed":
        return "LLM matrix consistency could not be measured because GenOS real calls were not executed. The scripted design preserves consistency by freezing one market axis before brand-share prompts."
    invalid = [key for key, value in brand_results.items() if not isinstance(value, dict) or value.get("status") != "ok"]
    if invalid:
        return f"Partially measurable: accepted rows share the same axis, but invalid/quarantined results remain for {', '.join(invalid)}."
    return "Measured brand results share the same market axis per ATC4, so the brand x topic matrix remains comparable."
