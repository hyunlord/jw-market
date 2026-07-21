from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from pipeline.scripts.crawler import crawl_2tier
from pipeline.scripts.crawler.tier2_catalog import stable_weekday_slice
from pipeline.scripts.crawler.temporal_tier2_pilot import (
    PilotInput,
    activity_commands,
    safe_run_id,
)


def test_score_only_uses_existing_corpus_without_crawling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    brand_file = tmp_path / "brands.json"
    brand_file.write_text(
        json.dumps([{"brand_name": "리바로", "brand_key": "brand-1", "source": "ubist"}]),
        encoding="utf-8",
    )
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "article.json").write_text(
        json.dumps({"title": "리바로 임상", "content": "리바로 결과"}),
        encoding="utf-8",
    )
    processed = tmp_path / "processed"
    plan = tmp_path / "plan.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "crawl_2tier.py",
            "--tier",
            "2",
            "--score-only",
            "--brand-file",
            str(brand_file),
            "--weekday-slice",
            str(stable_weekday_slice("brand-1")),
            "--output-dir",
            str(raw),
            "--processed-dir",
            str(processed),
            "--brand-plan-output",
            str(plan),
        ],
    )

    assert crawl_2tier.main() == 0
    scored = json.loads((processed / "article.json").read_text(encoding="utf-8"))
    assert scored["processed_by"] == "tier2_exact_rule_v1"
    assert scored["matches"][0]["brand_key"] == "brand-1"


def test_pilot_commands_are_fixed_and_write_only_to_isolated_staging() -> None:
    config = PilotInput(
        run_id="pilot-20260721-a",
        brand_file="/work/pilot/brands.json",
        weekday_slice=0,
        limit_brands=1,
        sites="약업신문",
        max_articles=1,
        match_table="tier2_match_staging",
    )

    commands = activity_commands(config)

    assert list(commands) == [
        "select_brand_universe",
        "crawl_news",
        "match_and_prescore",
        "llm_precision_score",
        "validate_isolated_result",
    ]
    assert "--dry-run" in commands["select_brand_universe"]
    assert "--score-only" in commands["match_and_prescore"]
    assert "append-live" not in " ".join(sum(commands.values(), []))
    assert "replace-live" not in " ".join(sum(commands.values(), []))
    assert "event_brand_scores__temporal_pilot_pilot_20260721_a" in commands["llm_precision_score"]


@pytest.mark.parametrize("value", ["../escape", "bad;drop", "space name", ""])
def test_run_id_must_be_safe_for_paths_and_table_names(value: str) -> None:
    with pytest.raises(ValueError):
        safe_run_id(value)


def test_parser_rejects_score_only_for_tier1() -> None:
    args = argparse.Namespace(tier="1", score_only=True)
    with pytest.raises(ValueError, match="Tier2"):
        crawl_2tier.validate_mode(args)


def test_worker_manifest_is_isolated_and_does_not_change_cronjob() -> None:
    repo = Path(__file__).resolve().parents[2]
    manifest = (repo / "deploy/k8s/crawler/temporal-tier2-worker.yaml").read_text(encoding="utf-8")
    cronjob = (repo / "deploy/k8s/crawler/crawl-tier2-cronjob.yaml").read_text(encoding="utf-8")

    assert "TEMPORAL_NAMESPACE" in manifest
    assert "jw-market-pilot" in manifest
    assert "jw-market-tier2-pilot-v1" in manifest
    assert "replicas: 1" in manifest
    assert "event_brand_scores" not in manifest
    assert "name: jw-news-crawl-tier2-daily-slice" in cronjob
    assert "suspend: false" in cronjob
