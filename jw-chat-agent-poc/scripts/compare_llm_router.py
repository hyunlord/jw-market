# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests==2.32.5",
# ]
# ///
# ─── How to run ───
# From repo root:
#   python3 jw-chat-agent-poc/scripts/compare_llm_router.py --out /tmp/chat_llm_router_compare
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import argparse
import csv
import json
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jw_chat_agent_poc.router import BQRouter, LLMFirstBQRouter


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    question: str
    expected_bqs: tuple[str, ...]
    expected_sources: tuple[str, ...]
    no_data: bool = False
    note: str = ""


SCENARIOS = (
    Scenario("sales", "리바로 매출 알려줘", ("Q1",), ("metrics",)),
    Scenario("market_share", "리바로 시장 점유율은?", ("Q2",), ("metrics",)),
    Scenario("market_size_growth", "리바로젯 시장규모랑 성장률?", ("Q1",), ("metrics",)),
    Scenario("competition_clinical", "리바로 경쟁이랑 임상 현황 같이 봐줘", ("Q2", "Q2.5"), ("metrics", "external_api")),
    Scenario("vague_market", "리바로 잘나가?", ("Q1",), ("metrics",), note="LLM이 키워드보다 잡을 가능성이 큰 모호 질문"),
    Scenario("hhi_series", "리바로 HHI 추이", ("Q1",), ("metrics",)),
    Scenario("monthly_sales", "리바로 월별 매출 추이", ("Q1",), ("metrics",)),
    Scenario("momentum", "리바로 모멘텀은?", ("Q1",), ("metrics",)),
    Scenario("ei", "리바로 EI 알려줘", ("Q1",), ("metrics",)),
    Scenario("sales_activity_boundary", "리바로 영업활동 Impact는?", ("Q4",), ("none",), True),
    Scenario("portfolio_boundary", "리바로 포트폴리오 사업성은?", ("Q5",), ("none",), True),
    Scenario("outside_brand", "타이레놀 매출 알려줘", ("Q1",), ("metrics",), note="resolver 단계에서 unsupported 처리"),
    Scenario("combo_clinical", "리바로젯 임상", ("Q2.5",), ("external_api",)),
    Scenario("combo_patent", "리바로젯 특허랑 FDA 라벨", ("Q2",), ("resolver", "external_api")),
    Scenario("prescription", "리바로 처방 세그먼트 추이는?", ("Q3",), ("metrics",)),
    Scenario("document", "업로드한 가이드라인 기반 시장 전망", ("Q1/Q5",), ("document",)),
    Scenario("clinical_kr", "페린젝트 국내 임상시험 찾아줘", ("Q2.5",), ("external_api",)),
    Scenario("label", "리바로 FDA label 요약", ("Q2",), ("resolver", "external_api")),
    Scenario("business_feasibility", "리바로 신사업 타당성 검토해줘", ("Q5",), ("none",), True),
    Scenario("unmatched", "오늘 날씨랑 리바로 관계 있어?", ("UNKNOWN",), ("none",), note="도메인 밖 질문"),
)

VARIANCE_QUESTIONS = (
    "리바로 경쟁이랑 임상 현황 같이 봐줘",
    "리바로 매출 알려줘",
    "리바로 HHI 추이",
    "리바로젯 특허랑 FDA 라벨",
    "리바로 신사업 타당성 검토해줘",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--variance-question", action="append", dest="variance_questions")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    keyword_router = BQRouter()
    llm_router = LLMFirstBQRouter()
    rows = [compare_scenario(scenario, keyword_router, llm_router) for scenario in SCENARIOS]
    variance_questions = tuple(args.variance_questions) if args.variance_questions else VARIANCE_QUESTIONS
    variance = [run_variance(question, llm_router) for question in variance_questions]

    write_csv(args.out / "router_comparison.csv", rows)
    (args.out / "router_comparison.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "variance.json").write_text(json.dumps(variance, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "router_comparison.md").write_text(render_markdown(rows, variance), encoding="utf-8")
    print(json.dumps(summary(rows, variance, args.out), ensure_ascii=False, indent=2))
    return 0


def compare_scenario(scenario: Scenario, keyword_router: BQRouter, llm_router: LLMFirstBQRouter) -> dict[str, Any]:
    has_documents = scenario.name == "document"
    keyword_routes = keyword_router.route(scenario.question, has_documents=has_documents)
    llm_routes = llm_router.route(scenario.question, has_documents=has_documents)
    keyword_score = score_routes(keyword_routes, scenario)
    llm_score = score_routes(llm_routes, scenario)
    if llm_score > keyword_score:
        winner = "llm"
    elif keyword_score > llm_score:
        winner = "keyword"
    else:
        winner = "tie"
    return {
        "name": scenario.name,
        "question": scenario.question,
        "expected_bqs": scenario.expected_bqs,
        "expected_sources": scenario.expected_sources,
        "keyword": route_summary(keyword_routes),
        "llm_first": route_summary(llm_routes),
        "llm_diagnostics": asdict(llm_router.last_diagnostics),
        "keyword_score": keyword_score,
        "llm_score": llm_score,
        "winner": winner,
        "note": scenario.note,
    }


def run_variance(question: str, llm_router: LLMFirstBQRouter) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for index in range(3):
        routes = llm_router.route(question)
        results.append(
            {
                "run": index + 1,
                "question": question,
                "routes": route_summary(routes),
                "diagnostics": asdict(llm_router.last_diagnostics),
            }
        )
    unique_variants = {json.dumps(item["routes"], ensure_ascii=False, sort_keys=True) for item in results}
    return {"question": question, "unique_route_count": len(unique_variants), "runs": results}


def score_routes(routes, scenario: Scenario) -> int:
    bqs = {route.bq for route in routes}
    sources = {source for route in routes for source in route.sources}
    score = 0
    if set(scenario.expected_bqs).issubset(bqs):
        score += 2
    if set(scenario.expected_sources).issubset(sources):
        score += 2
    if scenario.no_data and sources == {"none"}:
        score += 2
    if not scenario.no_data and sources != {"none"}:
        score += 1
    return score


def route_summary(routes) -> list[dict[str, Any]]:
    return [
        {
            "bq": route.bq,
            "question": route.question,
            "sources": route.sources,
            "reason": route.reason,
        }
        for route in routes
    ]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "name",
                "question",
                "expected_bqs",
                "expected_sources",
                "keyword_score",
                "llm_score",
                "winner",
                "llm_fallback",
                "llm_reason",
                "note",
            ],
        )
        writer.writeheader()
        for row in rows:
            diag = row["llm_diagnostics"]
            writer.writerow(
                {
                    "name": row["name"],
                    "question": row["question"],
                    "expected_bqs": ",".join(row["expected_bqs"]),
                    "expected_sources": ",".join(row["expected_sources"]),
                    "keyword_score": row["keyword_score"],
                    "llm_score": row["llm_score"],
                    "winner": row["winner"],
                    "llm_fallback": diag["fallback_used"],
                    "llm_reason": diag["reason"],
                    "note": row["note"],
                }
            )


def render_markdown(rows: list[dict[str, Any]], variance: list[dict[str, Any]]) -> str:
    lines = [
        "# LLM-first Router Comparison",
        "",
        "| 질문 | 키워드 점수 | LLM-first 점수 | 우위 | LLM fallback | 이유 |",
        "|---|---:|---:|---|---|---|",
    ]
    for row in rows:
        diag = row["llm_diagnostics"]
        lines.append(
            "| {question} | {keyword_score} | {llm_score} | {winner} | {fallback} | {reason} |".format(
                question=row["question"],
                keyword_score=row["keyword_score"],
                llm_score=row["llm_score"],
                winner=row["winner"],
                fallback=diag["fallback_used"],
                reason=diag["reason"],
            )
        )
    lines.extend(["", "## Variance", ""])
    for group in variance:
        lines.append(f"### {group['question']}")
        lines.append(f"- unique route count: {group['unique_route_count']}")
        for item in group["runs"]:
            lines.append(f"- run {item['run']}: {item['routes']} / {item['diagnostics']}")
    lines.append("")
    return "\n".join(lines)


def summary(rows: list[dict[str, Any]], variance: list[dict[str, Any]], out_dir: Path) -> dict[str, Any]:
    fallback_count = sum(1 for row in rows if row["llm_diagnostics"]["fallback_used"])
    wins = {
        "llm": sum(1 for row in rows if row["winner"] == "llm"),
        "keyword": sum(1 for row in rows if row["winner"] == "keyword"),
        "tie": sum(1 for row in rows if row["winner"] == "tie"),
    }
    return {
        "scenario_count": len(rows),
        "fallback_count": fallback_count,
        "wins": wins,
        "variance_unique_route_count": {item["question"]: item["unique_route_count"] for item in variance},
        "out_dir": str(out_dir),
    }


if __name__ == "__main__":
    raise SystemExit(main())
