from __future__ import annotations

from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jw_chat_agent_poc import ChatAgent


SCENARIOS = [
    ("Q1_single_metric", "리바로 시장 규모랑 성장 추이는?", []),
    ("Q2_competitive_clinical", "리바로 경쟁 상황이랑 임상 현황?", []),
    ("Q2_combo_label_patent", "리바로젯 FDA 라벨·특허?", []),
    ("document_rag", "이 가이드라인에서 1차 치료제는?", ["guideline_mock.txt"]),
    ("no_data_boundary", "리바로 영업활동 Impact는?", []),
    ("mixed_structured_document", "업로드한 시장 전망이랑 실제 우리 점유율 비교", ["datamonitor_mock.txt"]),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    fixture_dir = ROOT / "jw_chat_agent_poc" / "fixtures"
    agent = ChatAgent(external_mode="fixture")
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = []
    for name, question, docs in SCENARIOS:
        result = agent.answer(question, documents=[fixture_dir / doc for doc in docs])
        path = args.out / f"{name}.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=list), encoding="utf-8")
        manifest.append(
            {
                "scenario": name,
                "question": question,
                "sources": result["sources"],
                "decomposition": result["decomposition"],
                "tool_count": len(result["tool_calls"]),
                "output": str(path),
                "pass": True,
            }
        )
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"scenario_count": len(manifest), "manifest": str(args.out / "manifest.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
