from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from jw_chat_agent_poc.service.file_sql_query import SqlFileSource, SqlQueryOutcome
from jw_chat_agent_poc.tool_use.catalog import TOOL_DESCRIPTION_CATALOG
import jw_chat_agent_poc.tool_use.internal_adapters as adapters_module
from jw_chat_agent_poc.tool_use.internal_adapters import (
    ExistingFileCatalogBackend,
    InternalToolAdapterRegistry,
)


EXTERNAL_TOOL_COUNT = 23
INTERNAL_TOOL_NAMES = {
    "market.get_brand_metric",
    "market.get_market_size",
    "market.get_market_members",
    "market.get_timeseries",
    "market.get_channel_breakdown",
    "market.get_hhi",
    "market.get_growth_contribution",
    "market.compare_brands",
    "file.get_schema",
    "file.query",
}
ACTIVE_DESCRIPTION_SHA256 = "0549804803f1b6667592d08a1b8921bbcac24d3c3df42d44f5682ca43ea991f3"


class _RecordingMarketLayer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = {"method": method, "ordinal": len(self.calls) + 1}
        self.calls.append((method, args, kwargs))
        return result

    def brand_metric(
        self,
        brand: str,
        metric: str,
        period: str,
        market: str | None = None,
        source: str = "",
        history_points: int = 10,
    ) -> dict[str, Any]:
        return self._record(
            "brand_metric",
            brand,
            metric,
            period,
            market=market,
            source=source,
            history_points=history_points,
        )

    def market_scope(self, brand: str, market: str | None = None) -> dict[str, Any]:
        return self._record("market_scope", brand, market=market)

    def dimension_breakdown(
        self,
        brand: str,
        dimension: str,
        source: str = "",
        period: str = "latest",
        limit: int = 10,
        market: str | None = None,
        metric: str = "sales",
    ) -> dict[str, Any]:
        return self._record(
            "dimension_breakdown",
            brand,
            dimension,
            source=source,
            period=period,
            limit=limit,
            market=market,
            metric=metric,
        )

    def market_member_metric(
        self,
        brand: str,
        comparison: str,
        market: str | None = None,
        metric: str = "series",
    ) -> dict[str, Any]:
        return self._record(
            "market_member_metric",
            brand,
            comparison,
            market=market,
            metric=metric,
        )


class _RecordingFileBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def get_schema(
        self,
        *,
        conversation_id: str,
        sources: Sequence[SqlFileSource],
    ) -> tuple[str, ...]:
        self.calls.append(("get_schema", (), {"conversation_id": conversation_id, "sources": sources}))
        return ("brand", "sales")

    def query(
        self,
        *,
        question: str,
        conversation_id: str,
        sources: Sequence[SqlFileSource],
    ) -> SqlQueryOutcome:
        self.calls.append(
            (
                "query",
                (),
                {"question": question, "conversation_id": conversation_id, "sources": sources},
            )
        )
        return SqlQueryOutcome("query-result", (), ())

def _catalog_by_name():
    return {record.name: record for record in TOOL_DESCRIPTION_CATALOG}


def _arguments() -> Mapping[str, object]:
    return {
        "brand": "리바로",
        "comparison_brand": "리피토",
        "metric": "sales",
        "period": "2026-05",
        "market": "strategy_006",
        "source": "ubist",
        "history_points": 6,
        "limit": 7,
        "conversation_id": "catalog-test-session",
        "question": "Sheet1의 B2:D4 셀을 보여줘",
        "sources": (SqlFileSource("sales", "sample.xlsx", "Sheet1", document_id=1),),
    }


def test_catalog_additively_registers_internal_tools_without_enabling_selection() -> None:
    records = TOOL_DESCRIPTION_CATALOG
    internal = {record.name for record in records if not record.selection_enabled}

    assert len(records) == EXTERNAL_TOOL_COUNT + len(INTERNAL_TOOL_NAMES)
    assert len({record.name for record in records}) == len(records)
    assert internal == INTERNAL_TOOL_NAMES
    assert sum(record.selection_enabled for record in records) == EXTERNAL_TOOL_COUNT
    assert all(record.has_spec for record in records)


def test_every_catalog_record_has_substantive_routing_metadata() -> None:
    for record in TOOL_DESCRIPTION_CATALOG:
        assert record.not_for
        assert record.constraints
        assert record.does_not_return
        assert 2 <= len(record.examples) <= 3
        assert all(value.strip() for value in record.not_for)
        assert all(value.strip() for value in record.constraints)
        assert all(value.strip() for value in record.does_not_return)
        assert all(value.strip() for value in record.examples)
        lowered = record.catalog_description.casefold()
        assert "when to use" in lowered
        assert "when not" in lowered
        assert "constraints" in lowered
        assert "does not return" in lowered


def test_active_selection_descriptions_remain_byte_identical() -> None:
    rows = sorted(
        (record.name, record.description)
        for record in TOOL_DESCRIPTION_CATALOG
        if record.selection_enabled
    )
    payload = "\n".join(f"{name}\0{description}" for name, description in rows).encode()

    assert hashlib.sha256(payload).hexdigest() == ACTIVE_DESCRIPTION_SHA256


def test_external_descriptions_disambiguate_known_misroutes() -> None:
    records = _catalog_by_name()

    assert "품목 검색" in records["mfds_permission_search"].catalog_description
    assert "ITEM_SEQ" in records["mfds_permission_detail"].catalog_description
    assert "NCT ID" in records["clinicaltrials_study_details"].catalog_description
    assert "NCT ID 없는" in records["clinicaltrials_v2_search"].catalog_description
    assert "급여" in records["hira_reimbursement_criteria"].catalog_description
    assert "환자" in records["hira_disease_hospitalization_outpatient_stats"].catalog_description
    mfds_guidance = " ".join(
        records[name].catalog_description
        for name in ("mfds_permission_search", "mfds_permission_detail")
    )
    assert "dosage_unit" in mfds_guidance
    assert "시장 지표 단위" in mfds_guidance


def test_internal_adapters_delegate_to_existing_implementations_without_transforming_results() -> None:
    market = _RecordingMarketLayer()
    files = _RecordingFileBackend()
    registry = InternalToolAdapterRegistry(market_layer=market, file_backend=files)
    arguments = _arguments()

    results = {
        name: registry.execute(name, arguments)
        for name in sorted(INTERNAL_TOOL_NAMES)
    }

    assert registry.names() == tuple(sorted(INTERNAL_TOOL_NAMES))
    assert results["market.get_brand_metric"]["method"] == "brand_metric"
    assert results["market.get_market_size"]["method"] == "market_scope"
    assert results["market.get_market_members"]["method"] == "market_scope"
    assert results["market.get_timeseries"]["method"] == "brand_metric"
    assert results["market.get_channel_breakdown"]["method"] == "dimension_breakdown"
    assert results["market.get_hhi"]["method"] == "brand_metric"
    assert results["market.get_growth_contribution"]["method"] == "brand_metric"
    assert results["market.compare_brands"]["method"] == "market_member_metric"
    assert results["file.get_schema"] == ("brand", "sales")
    assert results["file.query"].file_context == "query-result"
    assert [call[0] for call in files.calls] == ["get_schema", "query"]


def test_missing_file_cells_implementation_is_not_registered_as_a_placeholder() -> None:
    assert "file.get_cells" not in _catalog_by_name()


def test_existing_file_backend_delegates_to_current_read_only_functions(monkeypatch) -> None:
    source = SqlFileSource("sales", "sample.xlsx", "Sheet1", document_id=1)
    expected_schema = ("brand", "sales")
    expected_outcome = SqlQueryOutcome("same-object", (), ())
    calls: list[tuple[object, ...]] = []

    def fake_schema(conversation_id, sources):
        calls.append(("schema", conversation_id, sources))
        return expected_schema

    def fake_query(question, conversation_id, sources):
        calls.append(("query", question, conversation_id, sources))
        return expected_outcome

    monkeypatch.setattr(adapters_module, "fetch_sql_schema_columns", fake_schema)
    monkeypatch.setattr(adapters_module, "query_uploaded_sql", fake_query)
    backend = ExistingFileCatalogBackend()

    assert backend.get_schema(conversation_id="session", sources=(source,)) is expected_schema
    assert (
        backend.query(question="합계", conversation_id="session", sources=(source,))
        is expected_outcome
    )
    assert calls == [
        ("schema", "session", (source,)),
        ("query", "합계", "session", (source,)),
    ]


def test_internal_adapter_module_is_not_connected_to_current_selection_path() -> None:
    package_root = Path(__file__).parents[1] / "jw_chat_agent_poc"
    selection_files = (
        package_root / "tool_use" / "registry.py",
        package_root / "tool_use" / "provider.py",
        package_root / "tool_use" / "integration.py",
        package_root / "tool_use" / "routing_v4_planner.py",
    )

    for path in selection_files:
        assert "internal_adapters" not in path.read_text(encoding="utf-8")
