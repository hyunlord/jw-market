from __future__ import annotations

import json

from pipeline.scripts.crawler.tier2_hybrid_plan import plan_hybrid_tasks, summarize_tasks
from pipeline.scripts.crawler.tier2_match_score import Tier2Brand


def test_plan_hybrid_tasks_splits_rule_single_and_llm_tagging(tmp_path) -> None:
    (tmp_path / "single.json").write_text(
        json.dumps(
            {
                "title": "가드렛 처방 확대",
                "content": "가드렛 처방 데이터가 공개됐다.",
                "search_keyword": "가드렛",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "multi.json").write_text(
        json.dumps(
            {
                "title": "PCSK9 항체 급여 논의",
                "content": "프랄런트와 레파타가 모두 언급됐다.",
                "search_keyword": "프랄런트",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    brands = [
        Tier2Brand("가드렛", "guardlet", "ubist"),
        Tier2Brand("프랄런트", "praluent", "ubist"),
        Tier2Brand("레파타", "repatha", "ubist"),
    ]

    tasks = plan_hybrid_tasks(tmp_path, brands)

    assert [(task.title, task.mode, task.candidate_count) for task in tasks] == [
        ("PCSK9 항체 급여 논의", "tier2_llm_tagging", 2),
        ("가드렛 처방 확대", "rule_single_wf196", 1),
    ]
    assert summarize_tasks(tasks) == {
        "article_tasks": 2,
        "candidate_pairs": 3,
        "mode_counts": {"tier2_llm_tagging": 1, "rule_single_wf196": 1},
    }
