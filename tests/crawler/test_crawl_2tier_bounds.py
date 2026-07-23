from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline.scripts.crawler import crawl_2tier
from pipeline.scripts.crawler import crawl_news_full_orchestrator


def test_tier2_site_bound_rejects_unknown_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        crawl_2tier,
        "_import_crawler",
        lambda: SimpleNamespace(SITE_CONFIGS={"히트뉴스": {}, "약업신문": {}}),
    )

    with pytest.raises(ValueError, match="unknown tier2 site"):
        crawl_2tier.tier2_sites("히트뉴스,오타사이트")


def test_tier1_wrapper_uses_the_orchestrator_cli_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(crawl_2tier.subprocess, "run", fake_run)
    args = SimpleNamespace(
        drug_profile_dir="/profiles",
        output_dir="/output",
        months=1,
        delay_sec=0.1,
        concurrent_sites=1,
        sites="히트뉴스",
        max_articles=2,
    )

    assert crawl_2tier.run_tier1_existing_flow(args) == 0
    command = commands[0]
    assert command[command.index("--drug-profile-dir") + 1] == "/profiles"
    assert command[command.index("--output-base") + 1] == "/output"
    assert command[command.index("--delay") + 1] == "0.1"
    assert command[command.index("--sites") + 1] == "히트뉴스"
    assert command[command.index("--crawler") + 1] == str(
        crawl_2tier.CRAWLER_DIR / "crawl_news_v2.py"
    )
    assert command[command.index("--max-articles") + 1] == "2"
    assert "--profile-dir" not in command
    assert "--output-base-dir" not in command
    assert "--delay-sec" not in command


def test_tier1_wrapper_requires_an_explicit_site_set() -> None:
    args = SimpleNamespace(
        drug_profile_dir="/profiles",
        output_dir="/output",
        months=1,
        delay_sec=0.1,
        concurrent_sites=1,
        sites=None,
        max_articles=0,
    )

    with pytest.raises(ValueError, match="explicit tier1 site set"):
        crawl_2tier.run_tier1_existing_flow(args)


def test_tier1_orchestrator_forwards_the_article_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(crawl_news_full_orchestrator.subprocess, "run", fake_run)
    args = SimpleNamespace(
        output_base=str(tmp_path),
        crawler="crawl_news_v2.py",
        drug_profile_dir="/profiles",
        months=1,
        max_pages=1,
        max_articles=2,
        delay=0.1,
        batch_by_month=False,
    )

    report = crawl_news_full_orchestrator.run_one_site("히트뉴스", args)

    assert report["exit_code"] == 0
    command = commands[0]
    assert command[command.index("--max-articles") + 1] == "2"
