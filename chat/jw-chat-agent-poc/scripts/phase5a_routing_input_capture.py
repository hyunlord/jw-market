from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import socket
from typing import Any
from unittest.mock import patch

from jw_chat_agent_poc.contracts.routing import RouteMode
from jw_chat_agent_poc.orchestrator.unified_router import (
    AppScopeSignals,
    MarketShortcutSignals,
    SecurityVerdict,
    UnifiedRouteInput,
    compare_with_legacy,
    route,
)
from jw_chat_agent_poc.tool_use.routing_v4_rules import classify_question_without_observation
from scripts.phase0b_characterization import ReplayCassette


ROUTE_POINTS = (
    "app_scope",
    "market_shortcut",
    "routing_v4_rules",
    "agent_loop_planner",
)
NON_AGENT_SNAPSHOT_ROUTES = {
    "file_context_scope_lock",
    "general_view",
    "market_scope",
}


class LiveDependencyAttempt(RuntimeError):
    """Raised when the local capture harness attempts network access."""


def _captured_point(
    *,
    capture_source: str,
    raw_input: dict[str, Any],
    route_input: UnifiedRouteInput,
    decided_by: str,
    legacy_domain: str,
    legacy_handler: str,
    legacy_mode: RouteMode,
) -> dict[str, Any]:
    canonical = route(route_input)
    comparison = compare_with_legacy(
        canonical,
        decided_by=decided_by,
        legacy_domain=legacy_domain,
        legacy_handler=legacy_handler,
        legacy_mode=legacy_mode,
    )
    return {
        "capture_status": "captured",
        "capture_source": capture_source,
        "raw_input": raw_input,
        "legacy_decision": {
            "domain": legacy_domain,
            "handler": legacy_handler,
            "mode": legacy_mode.value,
        },
        "canonical_route": canonical.model_dump(mode="json"),
        "comparison": comparison.model_dump(mode="json"),
    }


def _unavailable(status: str, reason: str, **details: Any) -> dict[str, Any]:
    return {
        "capture_status": status,
        "reason": reason,
        **details,
    }


def _capture_app_scope(kwargs: dict[str, Any]) -> dict[str, Any]:
    signals = AppScopeSignals(
        file_question=kwargs["file_question"],
        effective_question=kwargs["effective_question"],
        has_file=kwargs["has_file"],
        is_fresh_upload=kwargs["is_fresh_upload"],
        has_market_intent=kwargs["has_market_intent"],
        has_market_anchor=kwargs["has_market_anchor"],
        file_schema_columns=tuple(kwargs["file_schema_columns"]),
        needs_brand_clarification=kwargs["needs_brand_clarification"],
        needs_market_clarification=kwargs["needs_market_clarification"],
    )
    raw_input = {
        "question": kwargs["question"],
        "file_question": signals.file_question,
        "effective_question": signals.effective_question,
        "has_file": signals.has_file,
        "is_fresh_upload": signals.is_fresh_upload,
        "has_market_intent": signals.has_market_intent,
        "has_market_anchor": signals.has_market_anchor,
        "file_schema_columns": list(signals.file_schema_columns),
        "needs_brand_clarification": signals.needs_brand_clarification,
        "needs_market_clarification": signals.needs_market_clarification,
        "deep_research": kwargs["deep_research"],
    }
    return _captured_point(
        capture_source="service_fixture_observer",
        raw_input=raw_input,
        route_input=UnifiedRouteInput(
            question=kwargs["question"],
            security_verdict=SecurityVerdict.ALLOW,
            app_scope=signals,
            deep_research=kwargs["deep_research"],
        ),
        decided_by="app_scope",
        legacy_domain=kwargs["legacy_domain"],
        legacy_handler=kwargs["legacy_handler"],
        legacy_mode=kwargs["legacy_mode"],
    )


def _capture_market_shortcut(kwargs: dict[str, Any]) -> dict[str, Any]:
    resolver = kwargs["market_scope_resolver"]
    question = kwargs["question"]
    signals = MarketShortcutSignals(
        has_documents=kwargs["has_documents"],
        use_direct_agent_loop=kwargs["use_direct_agent_loop"],
        market_scope_resolver=resolver,
    )
    raw_input = {
        "question": question,
        "has_documents": signals.has_documents,
        "use_direct_agent_loop": signals.use_direct_agent_loop,
        "resolver_state": {
            "fixture_id": "static_metrics_market_scope_v1",
            "has_explicit_brand_anchor": resolver.has_explicit_brand_anchor(question),
            "has_explicit_named_market": resolver.has_explicit_named_market(question),
        },
    }
    return _captured_point(
        capture_source="service_fixture_observer",
        raw_input=raw_input,
        route_input=UnifiedRouteInput(
            question=question,
            security_verdict=SecurityVerdict.ALLOW,
            market_shortcut=signals,
        ),
        decided_by="market_shortcut",
        legacy_domain=kwargs["legacy_domain"],
        legacy_handler=kwargs["legacy_handler"],
        legacy_mode=kwargs["legacy_mode"],
    )


def _capture_routing_v4(question: str) -> dict[str, Any]:
    legacy = classify_question_without_observation(question)
    legacy_mode = (
        RouteMode.AGENTIC
        if legacy.domain_decision_source.value == "LLM"
        else RouteMode.DETERMINISTIC
    )
    return _captured_point(
        capture_source="deterministic_classifier_harness",
        raw_input={"question": question},
        route_input=UnifiedRouteInput(
            question=question,
            security_verdict=SecurityVerdict.ALLOW,
        ),
        decided_by="routing_v4_rules",
        legacy_domain=legacy.source_domain,
        legacy_handler=legacy.requested_capability,
        legacy_mode=legacy_mode,
    )


def _capture_service_points(question: str, lanes: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    if "multiturn" in lanes:
        missing = _unavailable(
            "missing_input",
            "prior_conversation_state_not_preserved_in_corpus_v1",
        )
        return {"app_scope": missing, "market_shortcut": dict(missing)}

    # Imported lazily because these are test doubles, never runtime dependencies.
    from test_service import FakeAgent, _market_scope_resolver
    from jw_chat_agent_poc.orchestrator import unified_router_shadow
    from jw_chat_agent_poc.service import app as service_app

    observations: dict[str, list[dict[str, Any]]] = {
        "app_scope": [],
        "market_shortcut": [],
    }

    def record(point: str):
        def recorder(**kwargs: Any) -> None:
            observations[point].append(kwargs)

        return recorder

    documents = None
    if "file" in lanes:
        suffix = ".pdf" if "PDF" in question else ".docx" if "워드" in question else ".xlsx"
        documents = [Path(f"/fixture/corpus-upload{suffix}")]

    def block_network(*_args: Any, **_kwargs: Any) -> None:
        raise LiveDependencyAttempt("network access is disabled during corpus capture")

    try:
        with (
            patch.object(unified_router_shadow, "observe_app_scope_route", record("app_scope")),
            patch.object(
                unified_router_shadow,
                "observe_market_shortcut_route",
                record("market_shortcut"),
            ),
            patch.object(socket, "create_connection", block_network),
            patch.object(socket.socket, "connect", block_network),
            redirect_stdout(io.StringIO()),
        ):
            service_app._answer_question(
                service_app.SessionStore(),
                _market_scope_resolver(),
                lambda **_kwargs: FakeAgent(external_mode="fixture"),
                question,
                "fixture",
                None,
                documents=documents,
            )
    except LiveDependencyAttempt:
        raise
    except Exception as exc:  # capture absence is evidence; invented replacement values are not
        missing = _unavailable(
            "missing_input",
            "local_fixture_execution_failed",
            exception_type=type(exc).__name__,
        )
        return {"app_scope": missing, "market_shortcut": dict(missing)}

    result: dict[str, dict[str, Any]] = {}
    for point, captured in observations.items():
        if not captured:
            result[point] = _unavailable(
                "unfired",
                "local_fixture_path_did_not_emit_route_point",
            )
        elif len(captured) > 1:
            result[point] = _unavailable(
                "missing_input",
                "multiple_route_point_emissions_require_sequence_context",
                emission_count=len(captured),
            )
        elif point == "app_scope":
            result[point] = _capture_app_scope(captured[0])
        else:
            result[point] = _capture_market_shortcut(captured[0])
    return result


def _planner_point(snapshot: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    historical_route = str(snapshot.get("router_planner") or "")
    if historical_route in NON_AGENT_SNAPSHOT_ROUTES:
        return _unavailable(
            "unfired",
            "historical_snapshot_selected_non_agent_handler",
            historical_router_planner=historical_route,
            snapshot_id=snapshot_id,
        )
    return _unavailable(
        "missing_input",
        "planner_signals_not_preserved_in_source_capture",
        historical_router_planner=historical_route,
        snapshot_id=snapshot_id,
    )


def capture_corpus(corpus_path: Path, snapshots_path: Path) -> dict[str, Any]:
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    snapshot_bundle = json.loads(snapshots_path.read_text(encoding="utf-8"))
    cassette = ReplayCassette.from_path(corpus_path.parent / "external_calls.v1.json")
    snapshots = {snapshot["case_id"]: snapshot for snapshot in snapshot_bundle["snapshots"]}
    cases: list[dict[str, Any]] = []

    for case in corpus["cases"]:
        question = case["question"]
        lanes = tuple(case["lanes"])
        points = _capture_service_points(question, lanes)
        points["routing_v4_rules"] = _capture_routing_v4(question)
        snapshot_id = case["snapshot_ids"][0]
        points["agent_loop_planner"] = _planner_point(snapshots[snapshot_id], snapshot_id)
        cases.append(
            {
                "question": question,
                "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
                "lanes": list(lanes),
                "route_points": {point: points[point] for point in ROUTE_POINTS},
            }
        )

    capture_totals = {"captured": 0, "unfired": 0, "missing_input": 0}
    comparison_totals = {"match": 0, "mismatch": 0, "unavailable": 0}
    mismatches: list[dict[str, Any]] = []
    for case in cases:
        for point_name, point in case["route_points"].items():
            status = point["capture_status"]
            capture_totals[status] += 1
            if status != "captured":
                comparison_totals["unavailable"] += 1
                continue
            if point["comparison"]["matches"]:
                comparison_totals["match"] += 1
            else:
                comparison_totals["mismatch"] += 1
                mismatches.append(
                    {
                        "question_sha256": case["question_sha256"],
                        "question": case["question"],
                        "route_point": point_name,
                        "legacy_decision": point["legacy_decision"],
                        "canonical_route": point["canonical_route"],
                        "comparison": point["comparison"],
                    }
                )

    return {
        "schema": "phase5a_routing_inputs_v2",
        "source_corpus": corpus_path.name,
        "source_corpus_sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
        "case_count": len(cases),
        "route_point_count": len(ROUTE_POINTS),
        "cell_count": len(cases) * len(ROUTE_POINTS),
        "capture_environment": {
            "mode": "local_fixture",
            "live_chat_calls": 0,
            "external_calls": 0,
            "database_writes": 0,
            "cassette_entry_count": len(cassette.entries),
            "external_dependency_mode": "exact_replay_cassette_plus_network_block",
        },
        "capture_totals": capture_totals,
        "comparison_totals": comparison_totals,
        "mismatches": mismatches,
        "cases": cases,
    }


def main() -> None:
    fixture_dir = Path(__file__).resolve().parents[1] / "tests" / "characterization" / "fixtures"
    payload = capture_corpus(
        fixture_dir / "corpus.v1.json",
        fixture_dir / "observed_snapshots.v1.json",
    )
    output = fixture_dir / "routing_inputs.v2.json"
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: payload[key] for key in ("capture_totals", "comparison_totals")}))


if __name__ == "__main__":
    main()
