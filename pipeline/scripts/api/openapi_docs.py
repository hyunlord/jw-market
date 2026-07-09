from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


AI_ANALYSIS_STAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "title": {"type": "string"},
        "body": {"type": "string"},
        "bullets": {"type": "array", "items": {}},
        "evidence": {"type": "array", "items": {}},
    },
}

AI_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "phenomenon": {"$ref": "#/components/schemas/AIAnalysisStage"},
        "cause": {"$ref": "#/components/schemas/AIAnalysisStage"},
        "prediction": {"$ref": "#/components/schemas/AIAnalysisStage"},
        "recommendation": {"$ref": "#/components/schemas/AIAnalysisStage"},
        "evidence_pool": {"type": "array", "items": {}},
    },
}

AI_ANALYSIS_UNAVAILABLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "available": {"type": "boolean", "const": False},
        "reason": {"type": "string"},
    },
}

AI_ANALYSIS_FIELD_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {"$ref": "#/components/schemas/AIAnalysis"},
        {"$ref": "#/components/schemas/AIAnalysisUnavailable"},
    ]
}

BRAND_FACTORS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "atc": {"type": "array", "items": {"type": "string"}},
        "ubist": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "seller": {"type": "array", "items": {"type": "string"}},
                "molecule_strength": {"type": "array", "items": {"type": "string"}},
                "form": {"type": "array", "items": {"type": "string"}},
                "route": {"type": "array", "items": {"type": "string"}},
                "reimbursement": {"type": "array", "items": {"type": "string"}},
            },
        },
        "iqvia": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mfr_name_kor": {"type": "array", "items": {"type": "string"}},
                "molecule_type": {"type": "array", "items": {"type": "string"}},
                "molecule_desc": {"type": "array", "items": {"type": "string"}},
                "pack_desc": {"type": "array", "items": {"type": "string"}},
                "strength": {"type": "array", "items": {"type": "string"}},
                "nhi_type": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}

DEEP_ANALYSIS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "data": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "ai_analysis": deepcopy(AI_ANALYSIS_FIELD_SCHEMA),
                "ai_analysis_short": deepcopy(AI_ANALYSIS_FIELD_SCHEMA),
                "ai_analysis_long": deepcopy(AI_ANALYSIS_FIELD_SCHEMA),
                "brand_factors": {"$ref": "#/components/schemas/BrandFactors"},
            },
        }
    },
}


def _ensure_components(openapi_schema: dict[str, Any]) -> None:
    components = openapi_schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["AIAnalysisStage"] = deepcopy(AI_ANALYSIS_STAGE_SCHEMA)
    schemas["AIAnalysis"] = deepcopy(AI_ANALYSIS_SCHEMA)
    schemas["AIAnalysisUnavailable"] = deepcopy(AI_ANALYSIS_UNAVAILABLE_SCHEMA)
    schemas["BrandFactors"] = deepcopy(BRAND_FACTORS_SCHEMA)


def _apply_deep_analysis_response(openapi_schema: dict[str, Any]) -> None:
    paths = openapi_schema.setdefault("paths", {})
    route = paths.setdefault("/api/deep-analysis/{brand_name}", {}).setdefault("get", {})
    responses = route.setdefault("responses", {})
    ok_response = responses.setdefault("200", {"description": "Successful Response"})
    content = ok_response.setdefault("content", {})
    application_json = content.setdefault("application/json", {})
    application_json["schema"] = deepcopy(DEEP_ANALYSIS_RESPONSE_SCHEMA)


def build_openapi_schema(app: FastAPI) -> dict[str, Any]:
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
        description=app.description,
    )
    _ensure_components(openapi_schema)
    _apply_deep_analysis_response(openapi_schema)
    return openapi_schema


def install_openapi_overrides(app: FastAPI) -> None:
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        app.openapi_schema = build_openapi_schema(app)
        return app.openapi_schema

    app.openapi = custom_openapi
