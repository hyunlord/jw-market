from __future__ import annotations

from jw_chat_agent_poc.service import answer_pipeline


def _identity(answer: str) -> str:
    return answer


def test_all_answer_stages_expose_engine_metadata_without_changing_order() -> None:
    names = answer_pipeline.PRE_CHART_STAGE_NAMES + answer_pipeline.POST_CHART_STAGE_NAMES
    transforms = {name: _identity for name in names}

    stages = answer_pipeline.ordered_stages(transforms, names)

    assert tuple(stage.name for stage in stages) == names
    assert tuple(stage.engines for stage in stages) == tuple(
        answer_pipeline.validation_engines_for_stage(name) for name in names
    )
    assert answer_pipeline.run_answer_pipeline("unchanged", stages) == "unchanged"


def test_stage_classification_preserves_explicit_unclassified_and_overlap() -> None:
    engine = answer_pipeline.ValidationEngine

    assert answer_pipeline.validation_engines_for_stage("natural_fact_lead") == ()
    assert answer_pipeline.validation_engines_for_stage("deferred_prescription_notice") == ()
    assert answer_pipeline.validation_engines_for_stage("market_answer_contract") == (
        engine.RELATIONAL_CLAIM,
        engine.EVIDENCE_BINDING,
    )
    assert answer_pipeline.validation_engines_for_stage("file_page_evidence") == (
        engine.EVIDENCE_BINDING,
        engine.COVERAGE_VALIDATION,
    )


def test_answer_stage_registry_covers_exactly_the_declared_thirty_stages() -> None:
    declared = answer_pipeline.PRE_CHART_STAGE_NAMES + answer_pipeline.POST_CHART_STAGE_NAMES
    registered = tuple(name for name, _engines in answer_pipeline.ANSWER_STAGE_ENGINE_ASSIGNMENTS)

    assert len(declared) == 30
    assert registered == declared
    assert len(set(registered)) == len(registered)


def test_outer_and_upstream_boundaries_are_programmatically_queryable() -> None:
    engine = answer_pipeline.ValidationEngine

    assert tuple(boundary.name for boundary in answer_pipeline.OUTER_DELIVERY_BOUNDARIES) == (
        "final_surface_assembly",
        "conversation_notice_prepend",
        "actual_coverage_observation",
        "response_format_contract",
        "sec12_output_leakage",
        "surface_coverage_observation",
        "typed_failure_model_observation",
    )
    assert answer_pipeline.OUTER_DELIVERY_BOUNDARIES[0].engines == (
        engine.COVERAGE_VALIDATION,
    )
    assert answer_pipeline.OUTER_DELIVERY_BOUNDARIES[1].engines == ()
    assert answer_pipeline.OUTER_DELIVERY_BOUNDARIES[-1].engines == ()
    assert answer_pipeline.UPSTREAM_VALIDATION_BOUNDARIES == (
        answer_pipeline.ValidationBoundary(
            "sec12_input_policy",
            (engine.INPUT_SECURITY,),
        ),
    )


def test_all_five_engines_have_an_explicit_boundary_without_merging_gates() -> None:
    assignments = tuple(
        engines for _name, engines in answer_pipeline.ANSWER_STAGE_ENGINE_ASSIGNMENTS
    ) + tuple(boundary.engines for boundary in answer_pipeline.OUTER_DELIVERY_BOUNDARIES) + tuple(
        boundary.engines for boundary in answer_pipeline.UPSTREAM_VALIDATION_BOUNDARIES
    )
    observed = {engine for engines in assignments for engine in engines}

    assert observed == set(answer_pipeline.ValidationEngine)
    assert answer_pipeline.PRE_CHART_STAGE_NAMES[19] == "relational_claim_pre_market"
    assert answer_pipeline.PRE_CHART_STAGE_NAMES[24] == "relational_claim_final"
    assert answer_pipeline.PRE_CHART_STAGE_NAMES[26] == "evidence_binding"
