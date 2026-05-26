"""Phase A-2-2-Side4 spec-vs-backend mismatch diagnosis.

Run as a script to generate /tmp/jw_spec_diagnosis, an audit directory, and a
zip artifact. The script is diagnostic-only: it performs local GET requests,
read-only DB SELECTs, and catalog parquet reads. It does not modify the spec,
backend, database, catalog, or mockup.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from backend_schema_extractor import extract_backend, write_outputs as write_backend_outputs  # noqa: E402
from spec_parser import parse_spec, write_outputs as write_spec_outputs  # noqa: E402


OUT_DIR = Path(os.getenv("JW_SPEC_DIAGNOSIS_OUT", "/tmp/jw_spec_diagnosis"))
SPEC_PATH = REPO_ROOT / "docs" / "reference" / "JW_Market_Analysis_API_Spec_20260520.html"
API_BASE = os.getenv("LOCAL_BACKEND_API_BASE", "http://127.0.0.1:8002").rstrip("/")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{X}", path)


def key_set_from_response(response: dict[str, Any]) -> set[str]:
    body = response.get("body")
    if isinstance(body, dict):
        return set(body.keys())
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return set(body[0].keys())
    return set()


def body_type(response: dict[str, Any]) -> str:
    body = response.get("body")
    if isinstance(body, dict):
        return "object"
    if isinstance(body, list):
        return "array"
    return type(body).__name__


def add_mismatch(
    mismatches: list[dict[str, Any]],
    *,
    category: str,
    endpoint: str,
    severity: str,
    title: str,
    spec: Any,
    backend: Any,
    evidence: str,
    fix_layer: str,
    impact: str,
    confidence: str = "high",
) -> None:
    mismatches.append(
        {
            "id": f"M{len(mismatches) + 1:03d}",
            "category": category,
            "endpoint": endpoint,
            "severity": severity,
            "title": title,
            "spec": spec,
            "backend": backend,
            "evidence": evidence,
            "fix_layer": fix_layer,
            "impact": impact,
            "confidence": confidence,
        }
    )


def openapi_endpoint_map(backend: dict[str, Any]) -> dict[str, dict[str, Any]]:
    endpoints = backend.get("openapi", {}).get("endpoints", [])
    return {f"{ep['method']} {normalize_path(ep['path'])}": ep for ep in endpoints}


def build_mismatch_table(parsed_spec: dict[str, Any], backend: dict[str, Any]) -> list[dict[str, Any]]:
    contract = parsed_spec["normalized_contract"]
    spec_eps = contract["endpoints"]
    backend_eps = openapi_endpoint_map(backend)
    calls = backend["spec_contract_calls"]
    backend_calls = backend["backend_contract_calls"]
    db_evidence = backend.get("db_evidence", {})

    mismatches: list[dict[str, Any]] = []

    # Endpoint path/query mismatches
    for name, spec_ep in spec_eps.items():
        if spec_ep.get("status") == "curl_example_only":
            continue
        key = f"{spec_ep['method']} {normalize_path(spec_ep['path'])}"
        if key not in backend_eps:
            add_mismatch(
                mismatches,
                category="endpoint_path_query",
                endpoint=name,
                severity="P0",
                title="Spec endpoint path is not implemented as written",
                spec=f"{spec_ep['method']} {spec_ep['path']}",
                backend=sorted(backend_eps.keys()),
                evidence="OpenAPI route set does not include the normalized spec path.",
                fix_layer="Backend code",
                impact="Frontend cannot call spec path without adaptation.",
            )

    if "GET /api/market-status/{X}" in backend_eps:
        add_mismatch(
            mismatches,
            category="endpoint_path_query",
            endpoint="market_status",
            severity="P0",
            title="Backend adds market_id path endpoint that the spec does not define",
            spec="/api/market-status with optional query market_id",
            backend="/api/market-status/{market_id} plus required view/source/measure",
            evidence="OpenAPI exposes GET /api/market-status/{market_id}.",
            fix_layer="Backend code + cache schema",
            impact="Current API is market/cache-cell scoped; spec expects page-level card list.",
        )

    for name, expected_queries in {
        "brands": {"allowed": {"q", "market_id"}, "required": set()},
        "market_status": {"allowed": {"market_id"}, "required": set()},
        "cause": {"allowed": {"view", "source", "measure"}, "required": set()},
        "deep_analysis": {"allowed": set(), "required": set()},
    }.items():
        spec_ep = spec_eps[name]
        key = f"{spec_ep['method']} {normalize_path(spec_ep['path'])}"
        backend_ep = backend_eps.get(key)
        if not backend_ep:
            continue
        params = [p for p in backend_ep.get("parameters", []) if p["in"] == "query"]
        names = {p["name"] for p in params}
        required = {p["name"] for p in params if p.get("required")}
        extra = names - expected_queries["allowed"]
        missing_allowed = expected_queries["allowed"] - names
        if extra or required != expected_queries["required"] or missing_allowed:
            add_mismatch(
                mismatches,
                category="endpoint_path_query",
                endpoint=name,
                severity="P0" if required else "P1",
                title="Query parameter contract differs from spec",
                spec={"allowed": sorted(expected_queries["allowed"]), "required": sorted(expected_queries["required"])},
                backend={"allowed": sorted(names), "required": sorted(required)},
                evidence=f"OpenAPI parameters for {key}: {params}",
                fix_layer="Backend code",
                impact="Spec clients get 422 or send backend-only parameters.",
            )

    # Response status and shape mismatches from spec-style calls.
    expected_status = {
        "spec_health": 200,
        "spec_brands": 200,
        "spec_market_status_no_query": 200,
        "spec_market_status_market": 200,
        "spec_cause_no_query": 200,
        "spec_cause_default_measure_only": 200,
        "spec_cause_market_landscape": 200,
        "spec_cause_invalid_measure": 400,
        "spec_deep_no_query": 200,
        "spec_cause_d3": 200,
    }
    for call_name, status in expected_status.items():
        actual = calls.get(call_name, {}).get("status")
        if actual != status:
            add_mismatch(
                mismatches,
                category="error_pattern",
                endpoint=call_name.replace("spec_", ""),
                severity="P0" if status == 200 else "P1",
                title="Spec-style request returns wrong HTTP status",
                spec=f"HTTP {status}",
                backend=f"HTTP {actual}",
                evidence=f"{call_name}: {calls.get(call_name, {}).get('url')}",
                fix_layer="Backend code" if call_name != "spec_cause_d3" else "Backend route addition",
                impact="Spec client behavior breaks or error handling differs.",
            )

    # Health response.
    health_keys = key_set_from_response(calls["spec_health"])
    expected_health = set(spec_eps["health"]["required_top_level"])
    missing = sorted(expected_health - health_keys)
    if missing:
        add_mismatch(
            mismatches,
            category="response_schema",
            endpoint="health",
            severity="P2",
            title="Health response omits spec fields",
            spec=sorted(expected_health),
            backend=sorted(health_keys),
            evidence="Live /api/health returns only the listed backend keys.",
            fix_layer="Backend code",
            impact="Readiness still works, but spec clients lose loaded-count/version metadata.",
        )

    # Brands response.
    brands_body = calls["spec_brands"]["body"]
    if body_type(calls["spec_brands"]) != spec_eps["brands"]["response_container"]:
        add_mismatch(
            mismatches,
            category="response_schema",
            endpoint="brands",
            severity="P0",
            title="Brands response container differs",
            spec="array of 25 JW brand objects",
            backend=f"{body_type(calls['spec_brands'])} with keys {sorted(key_set_from_response(calls['spec_brands']))}",
            evidence="/api/brands live response wraps brand list in an object.",
            fix_layer="Backend code + cache schema",
            impact="Original mockup/spec client expects iterable array directly.",
        )
    if isinstance(brands_body, dict):
        brands = brands_body.get("brands", [])
        jw_count = sum(1 for b in brands if isinstance(b, dict) and b.get("is_jw"))
        if len(brands) != 25 or jw_count != 25:
            add_mismatch(
                mismatches,
                category="response_schema",
                endpoint="brands",
                severity="P0",
                title="Brands endpoint returns full cache inventory rather than JW 25",
                spec="JW major 25 brands",
                backend={"total": len(brands), "jw": jw_count},
                evidence="Live /api/brands total_count and returned brands length.",
                fix_layer="Cache schema / backend filter",
                impact="Frontend count, dropdown, and card selection diverge from spec.",
            )
        first = brands[0] if brands and isinstance(brands[0], dict) else {}
        expected_item = set(spec_eps["brands"]["required_item_fields"])
        actual_item = set(first.keys())
        if expected_item - actual_item or actual_item - expected_item:
            add_mismatch(
                mismatches,
                category="response_schema",
                endpoint="brands",
                severity="P0",
                title="Brand item field names differ",
                spec=sorted(expected_item),
                backend=sorted(actual_item),
                evidence="Live /api/brands first item keys.",
                fix_layer="Cache schema / backend serializer",
                impact="Spec fields `brand`, `sources`, `rank`, market labels are absent or renamed.",
            )

    # Market-status response.
    backend_market = backend_calls["backend_market_status"]
    if body_type(backend_market) != spec_eps["market_status"]["response_container"]:
        add_mismatch(
            mismatches,
            category="response_schema",
            endpoint="market_status",
            severity="P0",
            title="Market-status response is single market object, not page card array",
            spec="array of brand cards",
            backend={"type": body_type(backend_market), "keys": sorted(key_set_from_response(backend_market))},
            evidence="Backend-valid /api/market-status/ml_006 response shape.",
            fix_layer="Cache schema + backend route",
            impact="Page 1 cannot boot from one spec call.",
        )

    # Cause response.
    backend_cause = backend_calls["backend_cause"]
    expected_cause = set(spec_eps["cause"]["required_top_level"])
    actual_cause = key_set_from_response(backend_cause)
    if expected_cause - actual_cause or {"brand_name", "brand_key"} & actual_cause:
        add_mismatch(
            mismatches,
            category="response_schema",
            endpoint="cause",
            severity="P0",
            title="Cause top-level fields differ",
            spec=sorted(expected_cause),
            backend=sorted(actual_cause),
            evidence="Backend-valid /api/cause/리바로 response top-level keys.",
            fix_layer="Backend composer + cache schema",
            impact="Spec client expects `brand`, `market_meta`, and direct measure-neutral data keys.",
        )
    data = backend_cause.get("body", {}).get("data", {}) if isinstance(backend_cause.get("body"), dict) else {}
    expected_data = set(spec_eps["cause"]["required_data_fields"])
    actual_data = set(data.keys()) if isinstance(data, dict) else set()
    if expected_data - actual_data or "sources_data" in actual_data:
        add_mismatch(
            mismatches,
            category="response_schema",
            endpoint="cause",
            severity="P0",
            title="Cause data object differs from measure-neutral spec schema",
            spec=sorted(expected_data),
            backend=sorted(actual_data),
            evidence="Backend-valid cause response nests metric history under data.sources_data and uses ranking/matrix keys with current cache shape.",
            fix_layer="Cache schema + response composer",
            impact="Legacy/spec chart paths fail or require frontend adapter.",
        )

    # Deep response.
    backend_deep = backend_calls["backend_deep"]
    expected_deep = set(spec_eps["deep_analysis"]["required_top_level"])
    actual_deep = key_set_from_response(backend_deep)
    if expected_deep - actual_deep or {"view", "source", "measure"} & actual_deep:
        add_mismatch(
            mismatches,
            category="response_schema",
            endpoint="deep_analysis",
            severity="P0",
            title="Deep-analysis top-level fields differ by query-scoped cache split",
            spec=sorted(expected_deep),
            backend=sorted(actual_deep),
            evidence="Backend-valid deep-analysis response includes view/source/measure and is cache-cell scoped.",
            fix_layer="Cache schema + backend route",
            impact="Spec expects all variants in one no-query payload.",
        )
    deep_body = backend_deep.get("body", {})
    combos = []
    if isinstance(deep_body, dict):
        combos = list(
            deep_body.get("data", {})
            .get("forecast", {})
            .get("by_combo", {})
            .keys()
        )
    if combos and not all("." in combo and "|" not in combo for combo in combos):
        add_mismatch(
            mismatches,
            category="response_schema",
            endpoint="deep_analysis",
            severity="P1",
            title="Deep-analysis combo keys differ from spec",
            spec=parsed_spec["normalized_contract"]["value_domains"]["deep_combo_keys"],
            backend=combos[:12],
            evidence="Backend uses view|source|measure combo keys rather than SOURCE.measure keys.",
            fix_layer="Cache schema / serializer",
            impact="Spec client cannot address `UBIST.sales` style combo keys.",
        )

    # Value domains.
    route_params = {
        ep["path"]: ep.get("parameters", [])
        for ep in backend.get("openapi", {}).get("endpoints", [])
    }
    for endpoint_name, path in {
        "market_status": "/api/market-status/{market_id}",
        "cause": "/api/cause/{brand_name}",
        "deep_analysis": "/api/deep-analysis/{brand_name}",
    }.items():
        params = route_params.get(path, [])
        view_schema = next((p["schema"] for p in params if p["name"] == "view"), {})
        source_schema = next((p["schema"] for p in params if p["name"] == "source"), {})
        if "general|strategic_ml|strategic_cd" in json.dumps(view_schema):
            add_mismatch(
                mismatches,
                category="view_source_measure_values",
                endpoint=endpoint_name,
                severity="P0",
                title="View value domain differs from design intent",
                spec=["market_landscape", "competitive_dynamics"],
                backend=view_schema,
                evidence=f"OpenAPI pattern for {path} view query.",
                fix_layer="Backend code + cache key mapping",
                impact="Spec clients using market_landscape/competitive_dynamics receive 422.",
            )
        if "iqvia_nsa" in json.dumps(source_schema):
            add_mismatch(
                mismatches,
                category="view_source_measure_values",
                endpoint=endpoint_name,
                severity="P1",
                title="Source value domain/casing differs",
                spec=["UBIST", "IQVIA"],
                backend=source_schema,
                evidence=f"OpenAPI pattern for {path} source query.",
                fix_layer="Backend code + serializer",
                impact="Backend leaks storage name iqvia_nsa and returns lowercase source values.",
            )

    # Measure validation and errors.
    invalid_backend = backend_calls["backend_cause_invalid_measure"]
    if invalid_backend["status"] != 400:
        add_mismatch(
            mismatches,
            category="error_pattern",
            endpoint="cause",
            severity="P1",
            title="Invalid source/measure combination does not return spec 400 detail object",
            spec=spec_eps["cause"]["invalid_measure_error"],
            backend={"status": invalid_backend["status"], "body": invalid_backend["body"]},
            evidence="Backend-valid view/source with UBIST counting_unit request.",
            fix_layer="Backend validator",
            impact="Clients cannot rely on valid_measures guidance.",
        )

    # Design-intent mismatch.
    add_mismatch(
        mismatches,
        category="design_intent",
        endpoint="deep_analysis",
        severity="P0",
        title="Backend deep-analysis design is opposite of spec no-query rationale",
        spec="No source/measure query; all variants in one payload for zero-network toggles.",
        backend="view/source/measure are required query parameters.",
        evidence="Spec design-intent section plus OpenAPI parameters.",
        fix_layer="Backend route + cache schema",
        impact="Frontend must refetch per toggle or adapt to backend cache cells.",
    )
    add_mismatch(
        mismatches,
        category="design_intent",
        endpoint="market_status",
        severity="P0",
        title="Backend market-status design is cache-cell scoped rather than page boot payload",
        spec="GET /api/market-status boots Page 1 with all cards.",
        backend="GET /api/market-status requires market_id/view/source/measure.",
        evidence="Spec sequence section plus OpenAPI parameters.",
        fix_layer="Backend route + cache schema",
        impact="Spec page boot sequence cannot run.",
    )

    # ETL/catalog/cache assessment.
    brand_rows = db_evidence.get("jw_brand_keys", [])
    split_keys = [
        row["brand_key"]
        for row in brand_rows
        if re.search(r"리바로젯\d|리바로페노\d", str(row.get("brand_key", "")))
    ]
    if split_keys:
        add_mismatch(
            mismatches,
            category="etl_catalog_assessment",
            endpoint="brands",
            severity="P1",
            title="Cache brand grain exposes dosage-split brand keys",
            spec="Canonical brand names such as 리바로젯 and 리바로페노",
            backend=split_keys,
            evidence="cache_cause JW brand_key distribution.",
            fix_layer="Catalog grain + cache ETL",
            impact="Spec 25-brand inventory and frontend card names diverge.",
        )

    return mismatches


def md_table(rows: list[list[Any]], headers: list[str]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(value).replace("\n", "<br>") for value in row) + " |")
    return "\n".join(lines) + "\n"


def mismatch_rows(mismatches: list[dict[str, Any]], category: str | None = None) -> list[list[Any]]:
    selected = [m for m in mismatches if category is None or m["category"] == category]
    return [[m["id"], m["severity"], m["endpoint"], m["title"], m["fix_layer"]] for m in selected]


def write_audit(audit_dir: Path, parsed_spec: dict[str, Any], backend: dict[str, Any], mismatches: list[dict[str, Any]]) -> None:
    audit_dir.mkdir(parents=True, exist_ok=True)
    raw_dest = audit_dir / "raw_data"
    if raw_dest.exists():
        shutil.rmtree(raw_dest)
    shutil.copytree(OUT_DIR, raw_dest, ignore=shutil.ignore_patterns("*.zip"))

    scripts_dest = audit_dir / "diagnostic_scripts"
    scripts_dest.mkdir(exist_ok=True)
    for src in [
        REPO_ROOT / "tools" / "spec_parser.py",
        REPO_ROOT / "tools" / "backend_schema_extractor.py",
        REPO_ROOT / "tests" / "integration" / "test_spec_vs_backend_mismatch.py",
    ]:
        shutil.copy2(src, scripts_dest / src.name)

    by_category = Counter(m["category"] for m in mismatches)
    by_severity = Counter(m["severity"] for m in mismatches)
    fix_layer_counts = Counter(m["fix_layer"] for m in mismatches)

    (audit_dir / "00_summary.md").write_text(
        "# Phase A-2-2-Side4 Spec vs Backend Mismatch Diagnosis Summary\n\n"
        f"Generated: {datetime.now().isoformat()}\n\n"
        "## Conclusion\n\n"
        "The 2026-05-20 HTML API spec is materially different from the live local backend. "
        "The largest divergences are not cosmetic: Page 1 boot shape, view/source value domains, "
        "query requirements, deep-analysis batching semantics, and cache/brand grain all differ.\n\n"
        "## Counts\n\n"
        + md_table(
            [["total", len(mismatches)]]
            + [[f"severity {k}", v] for k, v in sorted(by_severity.items())]
            + [[f"category {k}", v] for k, v in sorted(by_category.items())],
            ["metric", "count"],
        )
        + "\n## Highest Priority Mismatches\n\n"
        + md_table(mismatch_rows(mismatches)[:20], ["id", "severity", "endpoint", "title", "fix layer"])
        + "\n## Fix Layer Distribution\n\n"
        + md_table([[k, v] for k, v in fix_layer_counts.items()], ["fix layer", "mismatches"])
        + "\n## No-change Confirmation\n\n"
        "- Backend code, DB, mockup HTML, spec HTML, catalog parquet, and Live Demo processes were not modified.\n"
        "- No commit was created.\n",
        encoding="utf-8",
    )

    (audit_dir / "01_endpoint_path_query.md").write_text(
        "# 01. Endpoint Path + Query Mismatches\n\n"
        "## Mismatch Table\n\n"
        + md_table(mismatch_rows(mismatches, "endpoint_path_query"), ["id", "severity", "endpoint", "title", "fix layer"])
        + "\n## Backend OpenAPI Endpoints\n\n"
        + md_table(
            [
                [
                    ep["method"],
                    ep["path"],
                    ", ".join(
                        f"{p['name']}:{'required' if p.get('required') else 'optional'}"
                        for p in ep.get("parameters", [])
                    ),
                ]
                for ep in backend.get("openapi", {}).get("endpoints", [])
            ],
            ["method", "path", "parameters"],
        ),
        encoding="utf-8",
    )

    schema_rows = mismatch_rows(mismatches, "response_schema")
    (audit_dir / "02_response_schema_per_endpoint.md").write_text(
        "# 02. Response Schema Per Endpoint\n\n"
        + md_table(schema_rows, ["id", "severity", "endpoint", "title", "fix layer"])
        + "\n## Actual Sample Summaries\n\n"
        + md_table(
            [
                [
                    name,
                    summary.get("status"),
                    summary.get("body_type"),
                    ", ".join(summary.get("top_level_keys", [])[:18]),
                ]
                for name, summary in backend.get("response_summaries", {}).items()
                if name.startswith("backend_") or name in {"spec_health", "spec_brands"}
            ],
            ["sample", "status", "body_type", "top-level keys"],
        ),
        encoding="utf-8",
    )

    (audit_dir / "03_view_source_measure_values.md").write_text(
        "# 03. View / Source / Measure Values\n\n"
        "## Spec Domains\n\n"
        + json.dumps(parsed_spec["normalized_contract"]["value_domains"], ensure_ascii=False, indent=2)
        + "\n\n## Mismatches\n\n"
        + md_table(mismatch_rows(mismatches, "view_source_measure_values"), ["id", "severity", "endpoint", "title", "fix layer"])
        + "\n## Cache Evidence\n\n"
        + md_table(
            [
                [row.get("table_name"), row.get("view_type"), row.get("source"), row.get("measure"), row.get("row_count")]
                for row in backend.get("db_evidence", {}).get("cache_view_values", [])
            ],
            ["table", "view_type", "source", "measure", "rows"],
        ),
        encoding="utf-8",
    )

    (audit_dir / "04_error_pattern.md").write_text(
        "# 04. Error Pattern Mismatches\n\n"
        + md_table(mismatch_rows(mismatches, "error_pattern"), ["id", "severity", "endpoint", "title", "fix layer"])
        + "\n## Spec-style Call Statuses\n\n"
        + md_table(
            [
                [name, response["status"], response["url"], str(response["body"])[:180]]
                for name, response in backend["spec_contract_calls"].items()
            ],
            ["call", "status", "url", "body preview"],
        ),
        encoding="utf-8",
    )

    intent = parsed_spec.get("design_intent", {})
    (audit_dir / "05_design_intent.md").write_text(
        "# 05. Design Intent\n\n"
        "## Extracted Spec Intent Snippets\n\n"
        + json.dumps(intent.get("snippets", {}), ensure_ascii=False, indent=2)
        + "\n\n## Mismatches\n\n"
        + md_table(mismatch_rows(mismatches, "design_intent"), ["id", "severity", "endpoint", "title", "fix layer"])
        + "\n## Mentions\n\n"
        + json.dumps(intent.get("spec_mentions", {}), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )

    (audit_dir / "06_etl_catalog_assessment.md").write_text(
        "# 06. ETL / Catalog Assessment\n\n"
        + md_table(mismatch_rows(mismatches, "etl_catalog_assessment"), ["id", "severity", "endpoint", "title", "fix layer"])
        + "\n## Cache Brand Keys\n\n"
        + md_table(
            [
                [row.get("brand_key"), row.get("brand_name"), row.get("views"), row.get("sources"), row.get("market_ids")]
                for row in backend.get("db_evidence", {}).get("jw_brand_keys", [])[:80]
            ],
            ["brand_key", "brand_name", "views", "sources", "market_ids"],
        )
        + "\n## Catalog Sample\n\n"
        + md_table(
            [
                [
                    row.get("brand_id"),
                    row.get("name"),
                    row.get("merge_name"),
                    row.get("ml_id"),
                    row.get("cd_id"),
                    row.get("dosage_form"),
                    row.get("strength_pack"),
                ]
                for row in backend.get("catalog_evidence", {}).get("strategic_brand_sample", [])[:80]
            ],
            ["brand_id", "name", "merge_name", "ml_id", "cd_id", "dosage_form", "strength_pack"],
        ),
        encoding="utf-8",
    )

    (audit_dir / "07_fix_scope_estimate.md").write_text(
        "# 07. Fix Scope Estimate\n\n"
        "## Layer 1 — Backend Code\n\n"
        "- Restore spec paths/query optionality: `/api/market-status`, `/api/cause/{brand}`, `/api/deep-analysis/{brand}`.\n"
        "- Implement spec view/source validation and 400 invalid-measure detail object.\n"
        "- Add/confirm `/api/cause/{brand}/d3` if the curl example is binding.\n"
        "- Estimated effort: 2-4h if backed by compatible cache payloads; longer if serializers must reshape data.\n\n"
        "## Layer 2 — Cache Schema / Response Composition\n\n"
        "- Page-level market-status array rather than market/source/measure cell.\n"
        "- Cause top-level fields and `data` direct measure-neutral keys.\n"
        "- Deep-analysis no-query all-combo payload with `SOURCE.measure` combo keys.\n"
        "- Estimated effort: 5-10h plus cache rebuild.\n\n"
        "## Layer 3 — Catalog / ETL Brand Grain\n\n"
        "- Canonical brand key/display name for 25-brand inventory.\n"
        "- Product/dosage remains product grain; brand cards use canonical grain.\n"
        "- Estimated effort: 10-15h depending on catalog governance.\n\n"
        "## Layer 4 — Frontend v3\n\n"
        "- Build against the spec contract, not current cache-split backend.\n"
        "- Remove Side2 adapter assumptions after backend/cache are corrected.\n",
        encoding="utf-8",
    )

    (audit_dir / "08_next_phase_inputs.md").write_text(
        "# 08. Next Phase Inputs\n\n"
        "## Recommended Order\n\n"
        "1. Decide whether `/api/cause/{brand}/d3` curl example is binding or documentation residue.\n"
        "2. Backend code fix for path/query/value/error contract.\n"
        "3. Cache/schema builder fix for market-status, cause, and deep-analysis payload shapes.\n"
        "4. Catalog canonical brand grain fix if PL requires exact 25-brand display names.\n"
        "5. Regenerate cache and redo DB migration/promotion.\n"
        "6. Frontend v3 against spec contract.\n\n"
        "## Raw Inputs for Fix Phases\n\n"
        "- `raw_data/spec_contract_normalized.json`\n"
        "- `raw_data/backend_openapi.json`\n"
        "- `raw_data/backend_responses_sample.json`\n"
        "- `raw_data/mismatch_table.json`\n",
        encoding="utf-8",
    )


def make_zip(audit_dir: Path) -> Path:
    zip_path = audit_dir.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in audit_dir.rglob("*"):
            zf.write(path, path.relative_to(audit_dir.parent))
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad:
            raise RuntimeError(f"zip verification failed at {bad}")
    return zip_path


def collect_all(audit_dir: Path | None = None) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    parsed_spec = parse_spec(SPEC_PATH)
    write_spec_outputs(parsed_spec, OUT_DIR)
    backend = extract_backend(API_BASE)
    write_backend_outputs(backend, OUT_DIR)
    mismatches = build_mismatch_table(parsed_spec, backend)
    write_json(OUT_DIR / "mismatch_table.json", {"mismatches": mismatches})
    write_json(
        OUT_DIR / "diagnosis_summary.json",
        {
            "generated_at": datetime.now().isoformat(),
            "api_base": API_BASE,
            "mismatch_count": len(mismatches),
            "category_counts": Counter(m["category"] for m in mismatches),
            "severity_counts": Counter(m["severity"] for m in mismatches),
        },
    )
    if audit_dir is None:
        audit_dir = REPO_ROOT / f"phase_a2_2_side4_audit_{datetime.now().strftime('%Y%m%d_%H%M')}"
    write_audit(audit_dir, parsed_spec, backend, mismatches)
    zip_path = make_zip(audit_dir)
    elapsed = round(time.time() - started, 2)
    result = {
        "audit_dir": str(audit_dir),
        "audit_zip": str(zip_path),
        "elapsed_sec": elapsed,
        "mismatch_count": len(mismatches),
        "category_counts": dict(Counter(m["category"] for m in mismatches)),
        "severity_counts": dict(Counter(m["severity"] for m in mismatches)),
    }
    write_json(OUT_DIR / "diagnosis_summary.json", result)
    return result


def test_spec_parser_extracts_core_contract() -> None:
    parsed = parse_spec(SPEC_PATH)
    contract = parsed["normalized_contract"]["endpoints"]
    assert contract["market_status"]["path"] == "/api/market-status"
    assert contract["cause"]["query"][0]["values"] == ["market_landscape", "competitive_dynamics"]
    assert contract["deep_analysis"]["query"] == []


def test_existing_mismatch_table_is_valid() -> None:
    table_path = OUT_DIR / "mismatch_table.json"
    if not table_path.exists():
        pytest.skip("Run this file as a script to generate /tmp/jw_spec_diagnosis first.")
    table = read_json(table_path)
    mismatches = table["mismatches"]
    assert mismatches
    assert any(m["endpoint"] == "market_status" for m in mismatches)
    assert any(m["endpoint"] == "deep_analysis" for m in mismatches)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    result = collect_all(args.audit_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
