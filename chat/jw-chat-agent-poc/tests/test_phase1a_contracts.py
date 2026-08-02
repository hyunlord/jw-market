from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from jw_chat_agent_poc.contracts.capability import (
    capability_snapshot_from_source_class,
    parameter_resolution,
)
from jw_chat_agent_poc.contracts.query import (
    CatalogSourceClass,
    EntityKind,
    EntityRef,
    MarketAxisSpec,
    MarketSource,
    MeasureKind,
    MeasureSpec,
    NativeMarketMeasure,
    ParameterIssueReason,
    ParameterStatus,
    PeriodSpec,
    PortalMarketView,
    ResolutionStatus,
    ResolvedQuery,
    UnitSpec,
)
from jw_chat_agent_poc.contracts.shadow import resolved_query_from_query_spec
from jw_chat_agent_poc.orchestrator.query_spec import (
    EntityKind as LegacyEntityKind,
)
from jw_chat_agent_poc.orchestrator.query_spec import (
    QueryEntity,
    QueryOperation,
    RequestQuerySpec,
)


FIXTURE = Path(__file__).parent / "fixtures" / "phase0a_availability_matrix.v1.json"
VIEWS = tuple(PortalMarketView)
SOURCES = tuple(MarketSource)
MEASURES = {
    MarketSource.UBIST: (
        NativeMarketMeasure.SALES,
        NativeMarketMeasure.VOLUME,
    ),
    MarketSource.IQVIA: (
        NativeMarketMeasure.SALES,
        NativeMarketMeasure.UNIT,
        NativeMarketMeasure.DOSAGE_UNIT,
        NativeMarketMeasure.COUNTING_UNIT,
    ),
}


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _axis(**changes) -> MarketAxisSpec:
    values = {
        "market_id": "strategy_006",
        "market_definition": "고지혈증",
        "view": PortalMarketView.MARKET_LANDSCAPE,
        "source": MarketSource.UBIST,
        "native_measure": NativeMarketMeasure.SALES,
        "measure": MeasureSpec(kind=MeasureKind.ABSOLUTE, name="sales"),
        "period": PeriodSpec(start="2026-05", end="2026-05", granularity="month"),
        "unit": UnitSpec(code="KRW", label="원"),
        "catalog_snapshot_id": "phase0a-20260802",
        "market_definition_version": "phase0a-v1",
    }
    values.update(changes)
    return MarketAxisSpec(**values)


def test_contracts_are_frozen_and_forbid_extra_fields() -> None:
    entity = EntityRef(kind=EntityKind.BRAND, canonical_id="리바로", display_name="리바로")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EntityRef(
            kind=EntityKind.BRAND,
            canonical_id="리바로",
            display_name="리바로",
            unexpected=True,
        )
    with pytest.raises(ValidationError, match="frozen_instance"):
        entity.canonical_id = "리바로젯"


def test_resolved_query_requires_a_list_and_preserves_four_entities() -> None:
    entities = [
        EntityRef(kind=EntityKind.BRAND, canonical_id=name, display_name=name)
        for name in ("리바로", "리바로젯", "로수젯", "리피토")
    ]
    query = ResolvedQuery(
        entities=entities,
        resolution_status=ResolutionStatus.RESOLVED,
        parameter_status=ParameterStatus.NOT_APPLICABLE,
    )
    assert isinstance(query.entities, list)
    assert [entity.canonical_id for entity in query.entities] == [
        "리바로",
        "리바로젯",
        "로수젯",
        "리피토",
    ]

    with pytest.raises(ValidationError):
        ResolvedQuery(
            entities=entities[0],
            resolution_status=ResolutionStatus.RESOLVED,
            parameter_status=ParameterStatus.NOT_APPLICABLE,
        )


def test_shadow_conversion_preserves_legacy_entity_kinds() -> None:
    legacy = RequestQuerySpec(
        entities=(
            QueryEntity(LegacyEntityKind.BRAND, "리바로", "리바로"),
            QueryEntity(LegacyEntityKind.MARKET, "strategy_006", "고지혈증"),
        ),
        operation=QueryOperation.COMPARE_CURRENT,
        metrics=("sales",),
    )

    resolved = resolved_query_from_query_spec(legacy)

    assert [entity.kind for entity in resolved.entities] == [
        EntityKind.BRAND,
        EntityKind.MARKET,
    ]


def test_phase0a_matrix_is_represented_as_52_valid_and_20_invalid_cells() -> None:
    fixture = _fixture()
    counts = {ParameterStatus.VALID: 0, ParameterStatus.UNSUPPORTED_COMBINATION: 0}
    cells = 0
    for brand in fixture["brands"]:
        entity = EntityRef(
            kind=EntityKind.BRAND,
            canonical_id=brand["brand"],
            display_name=brand["brand"],
        )
        capability = capability_snapshot_from_source_class(
            entity=entity,
            source_class=CatalogSourceClass(brand["source_class"]),
            catalog_snapshot_id=fixture["catalog_snapshot_id"],
        )
        for _view in VIEWS:
            for source in SOURCES:
                for native_measure in MEASURES[source]:
                    resolution = parameter_resolution(capability, source, native_measure)
                    counts[resolution.status] += 1
                    cells += 1

    assert cells == fixture["expected"]["cells"] == 72
    assert counts[ParameterStatus.VALID] == fixture["expected"]["valid"] == 52
    assert counts[ParameterStatus.UNSUPPORTED_COMBINATION] == fixture["expected"]["invalid"] == 20


def test_invalid_responses_keep_entity_resolution_separate_from_parameters() -> None:
    dual = capability_snapshot_from_source_class(
        entity=EntityRef(kind=EntityKind.BRAND, canonical_id="가드렛", display_name="가드렛"),
        source_class=CatalogSourceClass.DUAL,
        catalog_snapshot_id="phase0a-20260802",
    )
    invalid_measure = parameter_resolution(
        dual,
        MarketSource.UBIST,
        NativeMarketMeasure.UNIT,
    )
    assert invalid_measure.resolution_status is ResolutionStatus.RESOLVED
    assert invalid_measure.status is ParameterStatus.UNSUPPORTED_COMBINATION
    assert invalid_measure.reason is ParameterIssueReason.INVALID_MEASURE_FOR_SOURCE
    assert invalid_measure.valid_measures == (
        NativeMarketMeasure.SALES,
        NativeMarketMeasure.VOLUME,
    )

    ubist_only = capability_snapshot_from_source_class(
        entity=EntityRef(kind=EntityKind.BRAND, canonical_id="리바로", display_name="리바로"),
        source_class=CatalogSourceClass.UBIST_ONLY,
        catalog_snapshot_id="phase0a-20260802",
    )
    brand_not_in_source = parameter_resolution(
        ubist_only,
        MarketSource.IQVIA,
        NativeMarketMeasure.SALES,
    )
    assert brand_not_in_source.resolution_status is ResolutionStatus.RESOLVED
    assert brand_not_in_source.status is ParameterStatus.UNSUPPORTED_COMBINATION
    assert brand_not_in_source.reason is ParameterIssueReason.BRAND_NOT_IN_SOURCE
    assert brand_not_in_source.valid_measures == ()


@pytest.mark.parametrize(
    "coordinate,changed_value",
    [
        ("market_id", "strategy_007"),
        ("market_definition", "이상지질혈증"),
        ("view", PortalMarketView.COMPETITIVE_DYNAMICS),
        ("source", MarketSource.IQVIA),
        ("native_measure", NativeMarketMeasure.VOLUME),
        ("measure", MeasureSpec(kind=MeasureKind.RATIO, name="share")),
        (
            "period",
            PeriodSpec(start="2026-Q1", end="2026-Q1", granularity="quarter"),
        ),
        ("unit", UnitSpec(code="KRW_100M", label="억원")),
        ("catalog_snapshot_id", "phase0a-20260803"),
        ("market_definition_version", "phase0a-v2"),
    ],
)
def test_axis_id_is_deterministic_and_sensitive_to_every_coordinate(
    coordinate: str,
    changed_value: object,
) -> None:
    baseline = _axis()
    assert len(baseline.axis_id) == 64
    assert baseline.axis_id == _axis().axis_id
    assert baseline.axis_id != _axis(**{coordinate: changed_value}).axis_id

    with pytest.raises(ValidationError, match="axis_id"):
        _axis(axis_id="0" * 64)
