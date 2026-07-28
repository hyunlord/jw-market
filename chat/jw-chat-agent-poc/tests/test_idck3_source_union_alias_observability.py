from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging

import pytest
from pydantic import BaseModel

from jw_chat_agent_poc.agent_loop import factory as factory_module
from jw_chat_agent_poc.agent_loop.factory import default_brand_alias_reader
from jw_chat_agent_poc.resolver import BrandResolver, UnsupportedBrandError
from jw_chat_agent_poc.resolver.alias_reader import MariaDbBrandAliasSource
from jw_chat_agent_poc.resolver.molecule_reader import MariaDbBrandMoleculeSource
from jw_chat_agent_poc.tool_use.executor import AgentExecutor
from jw_chat_agent_poc.tool_use.provider import ToolChoice
from jw_chat_agent_poc.tool_use.reimbursement_evidence import (
    public_reimbursement_identity_fields,
    reimbursement_envelope,
)
from jw_chat_agent_poc.tool_use.specs import ToolSpec
from jw_chat_agent_poc.tools.external.hira_reimbursement import (
    CacheLookupStatus,
    CacheStatus,
    ReimbursementCriterion,
    ReimbursementLookupResult,
)
from jw_chat_agent_poc.tools.metrics.cache_live import StaticMetricsCacheReader


class _StaticMembershipReader:
    def brand_memberships(self) -> tuple[dict[str, str], ...]:
        return (
            {
                "brand": "라베칸듀오",
                "market_id": "ml_001",
                "market_name": "위식도역류질환 시장",
                "support_source": "strategic_mart",
            },
            {
                "brand": "리바로젯",
                "market_id": "ml_006",
                "market_name": "리바로 리바로젯",
                "support_source": "strategic_mart",
            },
        )


class _StaticMoleculeReader:
    def brand_molecules(self) -> tuple[dict[str, str], ...]:
        return (
            {
                "brand_key": "라베칸듀오",
                "brand_name": "라베칸 듀오",
                "atc4_code": "",
                "mart_source": "any",
                "molecule_norm": "rabeprazole",
                "molecule_display": "rabeprazole",
            },
            {
                "brand_key": "라베칸듀오",
                "brand_name": "라베칸 듀오",
                "atc4_code": "",
                "mart_source": "any",
                "molecule_norm": "sodium bicarbonate",
                "molecule_display": "sodium bicarbonate",
            },
            {
                "brand_key": "라베칸듀오",
                "brand_name": "라베칸듀오",
                "atc4_code": "A02B2",
                "mart_source": "iqvia_nsa",
                "molecule_norm": "rabeprazole",
                "molecule_display": "rabeprazole",
            },
            {
                "brand_key": "라베칸듀오",
                "brand_name": "라베칸듀오",
                "atc4_code": "A02B2",
                "mart_source": "iqvia_nsa",
                "molecule_norm": "sodium",
                "molecule_display": "sodium",
            },
            {
                "brand_key": "라베칸듀오",
                "brand_name": "라베칸듀오",
                "atc4_code": "A2B2",
                "mart_source": "ubist",
                "molecule_norm": "rabeprazole",
                "molecule_display": "rabeprazole",
            },
        )


class _StaticAliasReader:
    def brand_aliases(self) -> tuple[dict[str, str], ...]:
        return ({"alias_name": "리바로 젯", "brand_key": "리바로젯"},)


class _NoInput(BaseModel):
    pass


@dataclass(slots=True)
class _OneChoice:
    def choose(self, *, user_text: str, messages: list[dict], tools: list[dict]) -> ToolChoice:
        del user_text, messages, tools
        return ToolChoice(
            "hira_reimbursement_criteria",
            {},
            "lookup",
            call_id="idck3-call",
        )


def _cache_reader() -> StaticMetricsCacheReader:
    return StaticMetricsCacheReader(
        cache_brands=[
            {"brand": "라베칸듀오", "market_id": "ml_001", "market_name": "위식도역류질환 시장"},
            {"brand": "리바로젯", "market_id": "ml_006", "market_name": "리바로 리바로젯"},
        ],
        market_status=[],
    )


def test_molecule_query_preserves_complete_logical_key_and_source_axis() -> None:
    sql = " ".join(MariaDbBrandMoleculeSource.molecule_sql().split())

    assert "brand_key, brand_name, atc4_code, mart_source, molecule_norm, molecule_display" in sql
    assert "ORDER BY brand_key, atc4_code, mart_source, molecule_norm" in sql


def test_alias_query_is_exact_and_orders_by_its_primary_key() -> None:
    sql = " ".join(MariaDbBrandAliasSource.alias_sql().split())

    assert "SELECT alias_name, brand_key FROM brand_alias" in sql
    assert "ORDER BY alias_name" in sql
    assert "LIKE" not in sql


def test_cache_factory_builds_the_alias_reader(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setenv("CHAT_METRICS_MODE", "cache")
    monkeypatch.setattr(
        factory_module,
        "shared_brand_alias_reader",
        lambda ttl_seconds: sentinel,
    )

    assert default_brand_alias_reader() is sentinel


def test_source_union_keeps_combo_identity_and_records_variance() -> None:
    resolver = BrandResolver(
        mode="cache",
        brand_reader=_cache_reader(),
        membership_reader=_StaticMembershipReader(),
        molecule_reader=_StaticMoleculeReader(),
    )

    resolution = resolver.resolve("라베칸듀오 급여기준", allow_default=False)

    assert set(resolution.molecule_en) == {"rabeprazole", "sodium", "sodium bicarbonate"}
    assert resolution.is_combo is True
    assert resolution.source_variance is True


def test_exact_runtime_alias_reaches_canonical_brand_without_partial_matching() -> None:
    resolver = BrandResolver(
        mode="cache",
        brand_reader=_cache_reader(),
        membership_reader=_StaticMembershipReader(),
        molecule_reader=_StaticMoleculeReader(),
        alias_reader=_StaticAliasReader(),
    )

    resolution = resolver.resolve("리바로 젯 급여기준", allow_default=False)

    assert resolution.canonical_brand == "리바로젯"
    assert resolution.resolved_via_alias is True


def test_runtime_alias_does_not_enable_partial_brand_matching() -> None:
    resolver = BrandResolver(
        mode="cache",
        brand_reader=_cache_reader(),
        membership_reader=_StaticMembershipReader(),
        molecule_reader=_StaticMoleculeReader(),
        alias_reader=_StaticAliasReader(),
    )

    with pytest.raises(UnsupportedBrandError):
        resolver.resolve("리바로 젯트 급여기준", allow_default=False)


def test_block_record_is_bounded_and_separates_unverifiable(
    caplog,
) -> None:
    result = ReimbursementLookupResult(
        ok=True,
        cache_status=CacheStatus.FRESH,
        retrieval="cache",
        data=ReimbursementCriterion(
            brand_name="리바로",
            title="고지혈증 치료제 급여기준",
            raw_text=(
                "Ezetimibe + pitavastatin calcium 복합경구제"
                "(품명: 리바로젯정 등)"
            ),
            source_date="2021-10-01",
            collected_at=datetime(2026, 7, 29, tzinfo=UTC),
            notice_number="제2021-245호",
            source_url="https://www.hira.or.kr/rc/example.do",
            source_notice_id="notice-idck3",
        ),
        cache_lookup_status=CacheLookupStatus.HIT,
    )

    with caplog.at_level(logging.INFO):
        envelope = reimbursement_envelope(
            result,
            subject="리바로",
            resolver=BrandResolver(mode="fixture"),
        )
    fields = public_reimbursement_identity_fields(envelope.raw)

    assert fields == {
        "identity_status": "mismatch",
        "identity_match": False,
        "identity_notice_required": True,
        "body_suppressed": True,
        "identity_notice": envelope.error_message,
        "requested_brand": "리바로",
        "served_notice_id": "notice-idck3",
        "blocked_reason": "identity_mismatch",
        "source_variance": False,
        "resolved_via_alias": False,
    }
    assert "blocked_reason=identity_mismatch" in caplog.text


def test_block_record_crosses_the_public_executor_projection() -> None:
    result = ReimbursementLookupResult(
        ok=True,
        cache_status=CacheStatus.FRESH,
        retrieval="cache",
        data=ReimbursementCriterion(
            brand_name="리바로",
            title="고지혈증 치료제 급여기준",
            raw_text="Ezetimibe + pitavastatin calcium 복합경구제(품명: 리바로젯정 등)",
            source_date="2021-10-01",
            collected_at=datetime(2026, 7, 29, tzinfo=UTC),
            notice_number="제2021-245호",
            source_url="https://www.hira.or.kr/rc/example.do",
            source_notice_id="notice-idck3-public",
        ),
        cache_lookup_status=CacheLookupStatus.HIT,
    )
    envelope = reimbursement_envelope(
        result,
        subject="리바로",
        resolver=BrandResolver(mode="fixture"),
    )
    spec = ToolSpec(
        name="hira_reimbursement_criteria",
        description="verified reimbursement fixture",
        input_model=_NoInput,
        execute=lambda _payload: envelope,
        timeout_s=1.0,
        tags=("external", "hira"),
    )

    execution = AgentExecutor(provider=_OneChoice()).run(
        user_text="리바로 급여기준",
        tools=(spec,),
    )
    render_data = execution.tool_calls[0]["render_data"]

    assert render_data["requested_brand"] == "리바로"
    assert render_data["served_notice_id"] == "notice-idck3-public"
    assert render_data["identity_status"] == "mismatch"
    assert render_data["blocked_reason"] == "identity_mismatch"
    assert render_data["source_variance"] is False
    assert render_data["resolved_via_alias"] is False


def test_unverifiable_record_is_not_reported_as_blocked() -> None:
    result = ReimbursementLookupResult(
        ok=True,
        cache_status=CacheStatus.FRESH,
        retrieval="cache",
        data=ReimbursementCriterion(
            brand_name="악템라",
            title="류마티스 치료제 급여기준",
            raw_text="관련 약제 투여 후 이상반응 관리 기준을 안내한다.",
            source_date="2026-07-01",
            collected_at=datetime(2026, 7, 29, tzinfo=UTC),
            notice_number="제2026-1호",
            source_url="https://www.hira.or.kr/rc/example.do",
            source_notice_id="notice-unverifiable",
        ),
        cache_lookup_status=CacheLookupStatus.HIT,
    )

    envelope = reimbursement_envelope(
        result,
        subject="악템라",
        resolver=BrandResolver(mode="fixture"),
    )
    fields = public_reimbursement_identity_fields(envelope.raw)

    assert envelope.ok is True
    assert fields["identity_status"] == "unverifiable"
    assert fields["blocked_reason"] is None
    assert fields["body_suppressed"] is False
    assert fields["served_notice_id"] == "notice-unverifiable"
