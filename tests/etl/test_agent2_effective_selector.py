from __future__ import annotations

from dataclasses import asdict
from datetime import date
import json
import re

import pytest

from pipeline.etl.io.mart.agent2_eligibility import (
    AGENT2_ELIGIBILITY_REVISION,
    Agent2ScoreRow,
    EligibleAgent2Event,
    eligible_agent2_events,
)
from pipeline.scripts.ai_analysis.bundle_builder import agent2_effective_selector
from pipeline.scripts.ai_analysis.bundle_builder.agent2_effective_selector import (
    EffectiveSelectorConfig,
    select_effective_agent2_events,
    selector_revision_for_config,
)


def _eligible(
    news_id: str,
    *,
    score: int,
    published_date: date | None,
    derivation: str = "llm_direct",
):
    processor = "cross_match_adapter_v1" if derivation == "cross_match" else "workflow_196_optionB"
    rows = eligible_agent2_events(
        (
            Agent2ScoreRow(
                news_id=news_id,
                source_processor=processor,
                derivation=derivation,
                tag="자본/경영",
                score=score,
                published_date=published_date,
                news_exists=True,
            ),
        )
    )
    assert len(rows) == 1
    return rows[0]


def test_effective_selector_applies_lookback_dedup_order_and_caps() -> None:
    config = EffectiveSelectorConfig(
        lookback_months=6,
        direct_prefetch=4,
        direct_cap=2,
        cross_cap=1,
        deduplicate_direct_by_date=True,
    )
    events = (
        _eligible("direct-best", score=90, published_date=date(2026, 7, 20)),
        _eligible("direct-dedup", score=80, published_date=date(2026, 7, 20)),
        _eligible("direct-second", score=70, published_date=date(2026, 6, 1)),
        _eligible("direct-capped", score=60, published_date=date(2026, 5, 1)),
        _eligible("direct-prefetch-capped", score=50, published_date=date(2026, 4, 1)),
        _eligible("cross-best", score=75, published_date=date(2026, 7, 1), derivation="cross_match"),
        _eligible("cross-capped", score=65, published_date=date(2026, 6, 1), derivation="cross_match"),
        _eligible("too-old", score=100, published_date=date(2025, 12, 31)),
        _eligible("future", score=100, published_date=date(2026, 7, 23)),
        _eligible("no-date", score=100, published_date=None),
    )

    result = select_effective_agent2_events(
        events,
        snapshot_date=date(2026, 7, 22),
        config=config,
    )

    assert result.selected_direct_news_ids == ("direct-best", "direct-second")
    assert result.selected_cross_news_ids == ("cross-best",)
    assert result.selected_news_ids == ("cross-best", "direct-best", "direct-second")
    assert result.rejected_by_dedup_news_ids == ("direct-dedup",)
    assert result.rejected_by_cap_news_ids == (
        "cross-capped",
        "direct-capped",
        "direct-prefetch-capped",
    )
    assert result.rejected_outside_lookback_news_ids == ("future", "no-date", "too-old")
    assert result.selector_revision == selector_revision_for_config(config)


def test_effective_selector_uses_news_id_as_a_distinct_identity() -> None:
    events = (
        _eligible("same", score=60, published_date=date(2026, 7, 1)),
        _eligible("same", score=80, published_date=date(2026, 7, 2)),
    )

    result = select_effective_agent2_events(events, snapshot_date=date(2026, 7, 22))

    assert result.selected_news_ids == ("same",)
    assert result.duplicate_news_ids == ("same",)


def test_effective_selector_is_byte_deterministic_for_reordered_input() -> None:
    events = (
        _eligible("b", score=60, published_date=date(2026, 7, 1)),
        _eligible("a", score=60, published_date=date(2026, 7, 1)),
    )

    payloads = []
    for candidate in (events, tuple(reversed(events)), events):
        result = select_effective_agent2_events(candidate, snapshot_date=date(2026, 7, 22))
        payloads.append(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    assert payloads[0] == payloads[1] == payloads[2]


def test_selector_revision_is_content_addressed_by_settings() -> None:
    base = EffectiveSelectorConfig()
    changed = EffectiveSelectorConfig(direct_cap=base.direct_cap + 1)

    assert re.fullmatch(r"[0-9a-f]{64}", selector_revision_for_config(base))
    assert selector_revision_for_config(base) != selector_revision_for_config(changed)


def test_selector_revision_is_content_addressed_by_upstream_eligibility(monkeypatch) -> None:
    before = selector_revision_for_config(EffectiveSelectorConfig())

    monkeypatch.setattr(agent2_effective_selector, "AGENT2_ELIGIBILITY_REVISION", "f" * 64)

    assert selector_revision_for_config(EffectiveSelectorConfig()) != before


def test_effective_selector_accepts_only_central_eligible_events() -> None:
    with pytest.raises(TypeError, match="EligibleAgent2Event"):
        select_effective_agent2_events(
            (object(),),  # type: ignore[arg-type]
            snapshot_date=date(2026, 7, 22),
        )

    forged = EligibleAgent2Event(
        news_id="forged",
        source_processor="future_unknown_processor",
        derivation="llm_direct",
        tag="자본/경영",
        score=90,
        published_date=date(2026, 7, 1),
        eligibility_revision=AGENT2_ELIGIBILITY_REVISION,
    )
    with pytest.raises(ValueError, match="central eligibility"):
        select_effective_agent2_events((forged,), snapshot_date=date(2026, 7, 22))
