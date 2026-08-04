from __future__ import annotations

from jw_chat_agent_poc.tool_use.contracts import EvidenceFact, ToolEnvelope
from jw_chat_agent_poc.service.file_sql_query import SqlFileSource, SqlQueryOutcome
from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    ClinicalTrialFact,
    FileCellFact,
    MarketMetricFact,
    RegulatoryRuleFact,
    ToolExecutionRecord,
    fact_supports_fields,
)
from jw_chat_agent_poc.tool_use.v3_execution_conversion import convert_execution


def _convert(
    tool_name: str,
    domain: str,
    arguments: dict[str, object],
    raw_result: object,
) -> MarketMetricFact | RegulatoryRuleFact | ClinicalTrialFact | FileCellFact:
    fact, failure, deferred = convert_execution(
        ToolExecutionRecord(tool_name, arguments, raw_result, 1.0),
        domain,
    )
    assert failure is None
    assert deferred is None
    assert fact is not None
    return fact


def test_market_fact_projects_six_axes_from_explicit_result_paths() -> None:
    raw = {
        "render_data": {
            "brand": "리바로",
            "metric": "sales",
            "period": "2026-05",
            "unit_label": "KRW",
            "market_id": "ml_006",
            "query_spec": {"view": "market_landscape"},
        }
    }

    fact = _convert(
        "market.get_brand_metric",
        "market",
        {"brand": "리바로", "metric": "sales", "period": "latest"},
        raw,
    )

    assert isinstance(fact, MarketMetricFact)
    assert (
        fact.entity,
        fact.metric,
        fact.period,
        fact.unit,
        fact.view,
        fact.market,
    ) == ("리바로", "sales", "2026-05", "KRW", "market_landscape", "ml_006")
    assert fact.missing_required_fields == ()
    assert fact_supports_fields(
        fact,
        ("entity", "metric", "period", "unit", "view", "market"),
    )
    assert fact.raw_result is raw


def test_market_fact_preserves_partial_result_without_promoting_latest_to_period() -> None:
    raw = {
        "render_data": {
            "anchor_brand": "리바로",
            "market_id": "ml_006",
            "query_spec": {"view": "market_landscape"},
        }
    }

    fact = _convert(
        "market.get_market_members",
        "market",
        {"brand": "리바로", "period": "latest"},
        raw,
    )

    assert isinstance(fact, MarketMetricFact)
    assert fact.entity == "리바로"
    assert fact.period is None
    assert set(fact.missing_required_fields) == {"metric", "period", "unit"}
    assert dict(fact.projection_missing_reasons)["period"] == "placeholder_not_canonical"
    assert fact_supports_fields(fact, ("entity", "view", "market"))
    assert not fact_supports_fields(fact, ("entity", "period"))


def test_market_fact_uses_explicit_query_spec_filters_without_guessing() -> None:
    raw = {
        "render_data": {
            "unit_label": "%",
            "query_spec": {
                "view": "market_landscape",
                "market": "ml_009",
                "metrics": ["share"],
                "filters": {"brand": "가드렛", "period": "2026-Q1"},
            },
        }
    }

    fact = _convert("market.get_timeseries", "market", {}, raw)

    assert isinstance(fact, MarketMetricFact)
    assert (
        fact.entity,
        fact.metric,
        fact.period,
        fact.unit,
        fact.view,
        fact.market,
    ) == ("가드렛", "share", "2026-Q1", "%", "market_landscape", "ml_009")
    assert fact.missing_required_fields == ()


def test_channel_breakdown_uses_query_metric_instead_of_query_spec_sentinel() -> None:
    raw = {
        "render_data": {
            "brand": "리바로",
            "metric": "query_spec",
            "measure": "sales",
            "period": "2026-05",
            "unit_label": "KRW",
            "market_id": "ml_006",
            "query_spec": {
                "view": "market_landscape",
                "metrics": ["sales"],
            },
        }
    }

    fact = _convert("market.get_channel_breakdown", "market", {}, raw)

    assert isinstance(fact, MarketMetricFact)
    assert fact.metric == "sales"
    assert dict(fact.projection_sources)["metric"] == "raw.render_data.query_spec.metrics"


def test_market_size_projects_metric_and_unit_from_explicit_result_field() -> None:
    raw = {
        "render_data": {
            "anchor_brand": "리바로",
            "market_size_recent_krw": 213_925_043_319.36,
            "period": "2026-05",
            "market_id": "ml_006",
            "view_type": "market_landscape",
        }
    }

    fact = _convert("market.get_market_size", "market", {"brand": "리바로"}, raw)

    assert isinstance(fact, MarketMetricFact)
    assert (
        fact.entity,
        fact.metric,
        fact.period,
        fact.unit,
        fact.view,
        fact.market,
    ) == ("리바로", "market_size", "2026-05", "KRW", "market_landscape", "ml_006")
    assert fact.missing_required_fields == ()


def test_regulatory_fact_projects_only_explicit_freshness_fields() -> None:
    raw = ToolEnvelope(
        ok=True,
        preview="ok",
        evidence=(
            EvidenceFact(
                fact_id="r1",
                subject="리바로",
                metric="급여기준",
                value=None,
                unit=None,
                period="2026-01-01",
                source_name="HIRA",
                source_locator="fixture",
                raw_ref=None,
            ),
        ),
        raw={"effective_date": "2026-01-01", "last_checked": "2026-08-04"},
        error_code=None,
        error_message=None,
    )

    fact = _convert("hira_reimbursement_criteria", "regulatory", {}, raw)

    assert isinstance(fact, RegulatoryRuleFact)
    assert fact.effective_date == "2026-01-01"
    assert fact.last_checked == "2026-08-04"
    assert fact.missing_required_fields == ()


def test_regulatory_fact_does_not_relabel_generic_period_as_effective_date() -> None:
    raw = ToolEnvelope(
        ok=True,
        preview="ok",
        evidence=(),
        raw={"period": "2026-01-01", "summary_text": "checked yesterday"},
        error_code=None,
        error_message=None,
    )

    fact = _convert("mfds_permission_search", "regulatory", {}, raw)

    assert isinstance(fact, RegulatoryRuleFact)
    assert fact.effective_date is None
    assert fact.last_checked is None
    assert fact.missing_required_fields == ("effective_date", "last_checked")


def test_clinical_fact_projects_nested_explicit_status_and_update() -> None:
    raw = ToolEnvelope(
        ok=True,
        preview="ok",
        evidence=(),
        raw={
            "render_data": {
                "detail": {
                    "status": "RECRUITING",
                    "last_update_posted": "2026-07-31",
                }
            }
        },
        error_code=None,
        error_message=None,
    )

    fact = _convert("clinicaltrials_study_details", "clinical", {}, raw)

    assert isinstance(fact, ClinicalTrialFact)
    assert fact.status == "RECRUITING"
    assert fact.last_update_posted == "2026-07-31"
    assert fact.missing_required_fields == ()


def test_clinical_search_projects_explicit_overall_status_alias() -> None:
    raw = ToolEnvelope(
        ok=True,
        preview="ok",
        evidence=(),
        raw={"render_data": {"overallStatus": "RECRUITING"}},
        error_code=None,
        error_message=None,
    )

    fact = _convert("clinicaltrials_v2_search", "clinical", {}, raw)

    assert isinstance(fact, ClinicalTrialFact)
    assert fact.status == "RECRUITING"
    assert fact.last_update_posted is None
    assert fact.missing_required_fields == ("last_update_posted",)
    assert dict(fact.projection_missing_reasons) == {
        "last_update_posted": "not_present_in_explicit_sources"
    }


def test_file_fact_projects_exact_scope_and_preserves_partial_scope() -> None:
    complete = _convert(
        "file.query",
        "file",
        {},
        {"file_id": "doc-7", "sheet": "Sheet1", "range": "A1:B4"},
    )
    partial = _convert(
        "file.query",
        "file",
        {"file_id": "doc-8"},
        {"rows": [["리바로", 10]]},
    )

    assert isinstance(complete, FileCellFact)
    assert (complete.file_id, complete.sheet, complete.range) == (
        "doc-7",
        "Sheet1",
        "A1:B4",
    )
    assert complete.missing_required_fields == ()
    assert isinstance(partial, FileCellFact)
    assert partial.file_id == "doc-8"
    assert partial.sheet is None
    assert partial.range is None
    assert partial.missing_required_fields == ("sheet", "range")


def test_file_query_projects_explicit_source_identity_from_real_contract() -> None:
    source = SqlFileSource(
        logical_name="doc-91:sheet-1",
        file_name="sample.xlsx",
        sheet_name="Sell Out Standard",
        document_id=91,
    )
    raw = SqlQueryOutcome(
        file_context="query-result",
        file_source_items=(
            {
                "document_id": 91,
                "file_name": "sample.xlsx",
                "sheet_name": "Sell Out Standard",
            },
        ),
        errors=(),
    )

    fact = _convert(
        "file.query",
        "file",
        {"conversation_id": "owned-session", "question": "조회", "sources": (source,)},
        raw,
    )

    assert isinstance(fact, FileCellFact)
    assert fact.file_id == 91
    assert fact.sheet == "Sell Out Standard"
    assert fact.range is None
    assert fact.missing_required_fields == ("range",)
    assert dict(fact.projection_missing_reasons) == {
        "range": "not_present_in_explicit_sources"
    }
    assert fact.raw_result is raw


def test_file_query_projects_the_selected_source_from_multiple_candidates() -> None:
    sources = (
        SqlFileSource(
            logical_name="doc-1:sheet-a",
            file_name="a.xlsx",
            sheet_name="A",
            document_id=1,
        ),
        SqlFileSource(
            logical_name="doc-2:sheet-b",
            file_name="b.xlsx",
            sheet_name="B",
            document_id=2,
        ),
    )
    raw = SqlQueryOutcome(
        file_context="query-result",
        file_source_items=({"file_name": "b.xlsx", "document_id": 2},),
        errors=(),
    )

    fact = _convert(
        "file.query",
        "file",
        {"conversation_id": "owned-session", "question": "조회", "sources": sources},
        raw,
    )

    assert isinstance(fact, FileCellFact)
    assert fact.file_id == 2
    assert fact.sheet == "B"
    assert fact.range is None
    assert fact.missing_required_fields == ("range",)


def test_file_query_uses_explicit_logical_name_when_document_id_is_absent() -> None:
    source = SqlFileSource(
        logical_name="doc-public:sheet-1",
        file_name="public.xlsx",
        sheet_name="Sheet1",
        document_id=None,
    )
    raw = SqlQueryOutcome(
        file_context="query-result",
        file_source_items=({"file_name": "public.xlsx"},),
        errors=(),
    )

    fact = _convert(
        "file.query",
        "file",
        {"conversation_id": "owned-session", "question": "조회", "sources": (source,)},
        raw,
    )

    assert isinstance(fact, FileCellFact)
    assert fact.file_id == "doc-public:sheet-1"
    assert fact.sheet == "Sheet1"
    assert fact.range is None
    assert fact.missing_required_fields == ("range",)


def test_file_query_does_not_guess_between_ambiguous_public_sheets() -> None:
    sources = (
        SqlFileSource(
            logical_name="doc-bpi:string",
            file_name="BPI.xlsx",
            sheet_name="String",
            document_id=None,
        ),
        SqlFileSource(
            logical_name="doc-bpi:numeric",
            file_name="BPI.xlsx",
            sheet_name="Numeric",
            document_id=None,
        ),
    )
    raw = SqlQueryOutcome(
        file_context="query-result",
        file_source_items=({"file_name": "BPI.xlsx"},),
        errors=(),
    )

    fact = _convert(
        "file.query",
        "file",
        {"conversation_id": "owned-session", "question": "조회", "sources": sources},
        raw,
    )

    assert isinstance(fact, FileCellFact)
    assert fact.file_id is None
    assert fact.sheet is None
    assert fact.range is None
    assert fact.missing_required_fields == ("file_id", "sheet", "range")
    assert dict(fact.projection_missing_reasons) == {
        "file_id": "not_present_in_explicit_sources",
        "sheet": "not_present_in_explicit_sources",
        "range": "not_present_in_explicit_sources",
    }
