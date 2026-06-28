from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from .config import ValidatorConfig


NUMBER_PATTERNS = [
    {
        "name": "comma_raw_value",
        "pattern": re.compile(r"(?<![.\d])(\d{1,3}(?:,\d{3})+(?:\.\d+)?)(?!\d)"),
        "type": "float",
        "low_priority": False,
    },
    {
        "name": "percent",
        "pattern": re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%"),
        "type": "float",
        "low_priority": False,
    },
    {
        "name": "rank",
        "pattern": re.compile(r"(?:rank|순위)\s*[:=]?\s*(\d+)\s*(?:위)?|(?<!\d)(\d+)\s*위"),
        "type": "int",
        "low_priority": False,
    },
    {
        "name": "kpi_value",
        "pattern": re.compile(r"(?:EI|Momentum|CAGR|HHI)\s*[:=]?\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE),
        "type": "float",
        "low_priority": False,
    },
    {
        "name": "plain_int",
        "pattern": re.compile(r"(?<![\d,.])(\d{1,6})(?![\d,.])"),
        "type": "int",
        "low_priority": True,
    },
]


SIMULATION_FORBIDDEN_SCENARIO_RE = re.compile(
    r"(낙관\s*시나리오|비관\s*시나리오|낙관적\s*시나리오|비관적\s*시나리오|\bbest\b|\bworst\b)",
    re.IGNORECASE,
)
SIMULATION_UNIT_CONVERSION_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s*(?:억|만|천|k|K|m|M)(?![A-Za-z가-힣])")
SIMULATION_CI_RE = re.compile(r"(95\s*%\s*(?:신뢰구간|CI)|(?:신뢰구간|CI)[^\n.]{0,20}95\s*%)", re.IGNORECASE)
FORECAST_VIEW_PATH_RE = re.compile(r"forecast_simulation\.by_view\.([A-Z]+)\.([A-Z]+)\.([^.]+)")
MARKET_VIEW_PATH_RE = re.compile(r"market_views\[(\d+)\]")
PREDICTION_NEWS_TRIGGER_RE = re.compile(
    r"(뉴스|보도|급여|허가|출시|신약|경쟁사|진입|임상|학회|약가|정책|규제|공세|시장\s*변화)",
    re.IGNORECASE,
)
VIEW_DISPLAY = {
    "ML": "Market Landscape",
    "CD": "Competitive Dynamics",
    "market_landscape": "Market Landscape",
    "competitive_dynamics": "Competitive Dynamics",
}
VIEW_COMPACT = {
    "Market Landscape": "ML",
    "Competitive Dynamics": "CD",
}
SIMULATION_EVIDENCE_TITLE_RE = re.compile(r"(시뮬레이션|예측|forecast|simulation)", re.IGNORECASE)
COMPACT_VIEW_TAG_RE = re.compile(r"(?:ML|CD)\s*·\s*[A-Z]+\s*·", re.IGNORECASE)
STANDALONE_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
MARKET_METRIC_PATH_RE = re.compile(
    r"("
    r"\.(?:raw_value|value|raw_value_12m|growth_abs|growth_yoy_pct|ms_pct|yoy_pct|qoq_pct|mom_pct|mat_yoy_pct|rank)$"
    r"|\.market_size\.history\.[^.]+$"
    r"|\.hhi_5y\.[^.]+$"
    r"|\.kpi_extras\.(?:brand_cagr_5y_pct|market_cagr_5y_pct|ei|momentum_score|target_rank|total_brands_in_market)$"
    r"|\.momentum\.value_pct_per_period$"
    r"|\.horizon_(?:1y|3y|5y|10y)\.base$"
    r")"
)


DEFAULT_TOLERANCE_BY_TYPE = {
    "currency_krw": 0.01,
    "volume_rx": 0.5,
    "unit_pack": 0.5,
    "percent": 0.05,
    "percent_signed": 0.05,
    "kpi": 0.05,
    "rank": 0.0,
}


@dataclass
class StageValidation:
    valid: bool
    extracted: list[dict[str, Any]]
    unmatched: list[dict[str, Any]]
    warnings: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    valid: bool
    stage_results: dict[str, StageValidation]
    total_numbers_extracted: int
    total_numbers_matched: int
    unmatched_numbers: list[dict[str, Any]]
    warnings: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "stage_results": {stage: result.to_dict() for stage, result in self.stage_results.items()},
            "total_numbers_extracted": self.total_numbers_extracted,
            "total_numbers_matched": self.total_numbers_matched,
            "unmatched_numbers": self.unmatched_numbers,
            "warnings": self.warnings,
        }


def _coerce_number(value: str, value_type: str) -> float | int:
    cleaned = value.replace(",", "")
    if value_type == "int":
        return int(float(cleaned))
    return float(cleaned)


def _match_group(match: re.Match[str]) -> str:
    for group in match.groups():
        if group is not None:
            return group
    return match.group(0)


def _default_config() -> ValidatorConfig:
    return ValidatorConfig(tolerance_default=0.01, tolerance_percent=0.05, tolerance_kpi=0.05)


def _nearby_context(raw_text: str, full_context: str, before: int = 30, after: int = 40) -> str:
    pos = (full_context or "").find(raw_text)
    if pos < 0:
        return full_context or ""
    return full_context[max(0, pos - before) : pos + len(raw_text) + after]


def _is_qualified_threshold(raw_text: str, full_context: str) -> bool:
    """Return true for approximate threshold phrases like "20% 이상"."""

    nearby = _nearby_context(raw_text, full_context, before=8, after=12)
    after = nearby.split(raw_text, 1)[1] if raw_text in nearby else ""
    return bool(re.match(r"^\s*(이상|이하|초과|미만|내외|대)", after))


def _is_ci_confidence_literal(raw_text: str, full_context: str, value: float | int) -> bool:
    if float(value) != 95.0 or "%" not in raw_text:
        return False
    nearby = _nearby_context(raw_text, full_context, before=20, after=20)
    return bool(re.search(r"(신뢰구간|CI)", nearby, flags=re.IGNORECASE))


def classify_number_context(raw_text: str, full_context: str, value: float | int) -> str:
    """Classify a rendered number so matching can use the right tolerance."""

    text = full_context or ""
    nearby = _nearby_context(raw_text, text)
    after = nearby.split(raw_text, 1)[1] if raw_text in nearby else nearby
    before = nearby.split(raw_text, 1)[0] if raw_text in nearby else nearby

    if "%" in raw_text:
        return "percent_signed" if raw_text.strip().startswith(("+", "-")) else "percent"

    if "위" in raw_text or re.search(r"(rank|순위)\s*[:=]?\s*$", before, flags=re.IGNORECASE):
        return "rank"
    if re.match(r"^\s*위", after):
        return "rank"

    if re.search(r"(EI|Momentum|CAGR|HHI)\s*[:=]?\s*$", before, flags=re.IGNORECASE):
        return "kpi"

    if re.match(r"^\s*(Rx|처방량|처방건수|건)", after):
        return "volume_rx"

    if re.match(r"^\s*(정|캡슐|바이알|포|병|개)", after):
        return "unit_pack"

    if re.match(r"^\s*(KRW|원|₩)", after):
        return "currency_krw"

    if any(ind in nearby for ind in ("Rx", "처방량", "처방 ", "처방건수")) and not any(
        ind in after[:20] for ind in ("KRW", "₩")
    ):
        return "volume_rx"

    if any(ind in nearby for ind in ("KRW", "₩")):
        return "currency_krw"

    if re.search(r"(EI|Momentum|CAGR|HHI)", nearby, flags=re.IGNORECASE):
        return "kpi"

    if "," in raw_text and float(value) >= 1000:
        return "currency_krw"

    return "currency_krw"


def _tolerance_for_type(number_type: str, config: ValidatorConfig | None = None) -> float:
    if config is None:
        config = _default_config()
    table = getattr(config, "tolerance_by_type", None) or DEFAULT_TOLERANCE_BY_TYPE
    if number_type in table:
        return float(table[number_type])
    return float(getattr(config, "tolerance_default", DEFAULT_TOLERANCE_BY_TYPE["currency_krw"]))


def extract_numbers(text: str, config: ValidatorConfig | None = None) -> list[dict[str, Any]]:
    if config is None:
        config = _default_config()
    extracted: list[dict[str, Any]] = []
    for spec in NUMBER_PATTERNS:
        for match in spec["pattern"].finditer(text or ""):
            value_text = _match_group(match)
            try:
                value = _coerce_number(value_text, spec["type"])
            except ValueError:
                continue
            raw_text = match.group(0)
            number_type = classify_number_context(raw_text, text or "", value)
            extracted.append(
                {
                    "value": value,
                    "raw_text": raw_text,
                    "pattern": spec["name"],
                    "number_type": number_type,
                    "tolerance": _tolerance_for_type(number_type, config),
                    "low_priority": bool(spec.get("low_priority", False))
                    or _is_qualified_threshold(raw_text, text or ""),
                    "ci_confidence_literal": _is_ci_confidence_literal(raw_text, text or "", value),
                }
            )
            if extracted[-1]["ci_confidence_literal"]:
                extracted[-1]["low_priority"] = True
    return extracted


def build_bundle_path_index(bundle: dict) -> dict[float, list[str]]:
    index: dict[float, list[str]] = {}

    def traverse(obj: Any, path: str = "") -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                next_path = f"{path}.{key}" if path else str(key)
                traverse(value, next_path)
        elif isinstance(obj, list):
            for idx, value in enumerate(obj):
                traverse(value, f"{path}[{idx}]")
        elif isinstance(obj, bool):
            return
        elif isinstance(obj, (int, float)):
            numeric = float(obj)
            index.setdefault(numeric, []).append(path)
        elif isinstance(obj, str):
            for item in extract_numbers(obj):
                index.setdefault(float(item["value"]), []).append(f"{path}::{item['raw_text']}")

    traverse(bundle)
    return index


def find_match(value: float | int, bundle_index: dict[float, list[str]], tolerance: float) -> str | None:
    numeric = float(value)
    best_path: str | None = None
    best_delta: float | None = None
    for bundle_value, paths in bundle_index.items():
        delta = abs(bundle_value - numeric)
        if delta <= tolerance and (best_delta is None or delta < best_delta):
            best_delta = delta
            best_path = paths[0]
    return best_path


def find_match_unit_aware(
    value: float | int,
    bundle_index: dict[float, list[str]],
    number_type: str,
    config: ValidatorConfig | None = None,
) -> str | None:
    if config is None:
        config = _default_config()

    numeric = float(value)
    tolerance = _tolerance_for_type(number_type, config)
    relative_tolerance = float(getattr(config, "relative_tolerance", 0.001))
    best_path: str | None = None
    best_delta: float | None = None

    for bundle_value, paths in bundle_index.items():
        delta = abs(bundle_value - numeric)
        matches_absolute = delta <= tolerance
        matches_relative = abs(bundle_value) > 1000 and delta / abs(bundle_value) <= relative_tolerance
        if (matches_absolute or matches_relative) and (best_delta is None or delta < best_delta):
            best_delta = delta
            best_path = paths[0]
    return best_path


def _stage_contexts(stage_dict: dict[str, Any]) -> dict[str, str]:
    contexts = {
        "title": str(stage_dict.get("title", "")),
        "body": str(stage_dict.get("body", "")),
    }
    for idx, bullet in enumerate(stage_dict.get("bullets", []) or []):
        contexts[f"bullets[{idx}]"] = str(bullet)
    return contexts


def validate_stage_output(
    stage: str,
    stage_dict: dict[str, Any],
    bundle_index: dict[float, list[str]],
    config: ValidatorConfig,
) -> StageValidation:
    extracted: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for context_name, text in _stage_contexts(stage_dict).items():
        for item in extract_numbers(text, config):
            matched_path = find_match_unit_aware(item["value"], bundle_index, item["number_type"], config)
            record = {
                "stage": stage,
                "context": context_name,
                "context_text": text,
                "value": item["value"],
                "raw_text": item["raw_text"],
                "pattern": item["pattern"],
                "number_type": item["number_type"],
                "tolerance": item["tolerance"],
                "matched_path": matched_path,
                "low_priority": item["low_priority"],
                "ci_confidence_literal": item["ci_confidence_literal"],
            }
            extracted.append(record)
            if matched_path is None:
                if item["low_priority"]:
                    warnings.append(record)
                else:
                    unmatched.append(record)

    return StageValidation(valid=not unmatched, extracted=extracted, unmatched=unmatched, warnings=warnings)


def _policy_issue(stage: str, context: str, pattern: str, raw_text: str, message: str) -> dict[str, Any]:
    return {
        "stage": stage,
        "context": context,
        "value": None,
        "raw_text": raw_text,
        "pattern": pattern,
        "number_type": "policy",
        "tolerance": 0.0,
        "matched_path": None,
        "low_priority": False,
        "message": message,
    }


def _prediction_policy_issue(pattern: str, raw_text: str, message: str) -> dict[str, Any]:
    return _policy_issue("prediction", "simulation_policy", pattern, raw_text, message)


def _prediction_text(parsed_output: dict) -> str:
    prediction = parsed_output.get("prediction", {}) or {}
    parts = [str(prediction.get("title") or ""), str(prediction.get("body") or "")]
    parts.extend(str(item) for item in prediction.get("bullets", []) or [])
    return "\n".join(part for part in parts if part)


def _forecast_simulation_available(bundle: dict) -> bool:
    forecast = bundle.get("forecast_simulation") or {}
    return bool(forecast.get("available") and forecast.get("by_view"))


def _view_label_parts_from_path(bundle: dict, matched_path: str | None) -> tuple[str, str] | None:
    path = str(matched_path or "")
    market_match = MARKET_VIEW_PATH_RE.search(path)
    if market_match:
        views = bundle.get("market_views") or []
        idx = int(market_match.group(1))
        if idx >= len(views):
            return None
        view = views[idx] or {}
        view_id = str(view.get("view_id") or "")
        if view_id:
            parts = view_id.split(".")
            if len(parts) >= 2:
                return VIEW_DISPLAY.get(parts[0], parts[0]), parts[1].upper()
        view_name = str(view.get("view") or "")
        source = str(view.get("source") or "")
        if view_name and source:
            return VIEW_DISPLAY.get(view_name, view_name), source.upper()
        return None

    forecast_match = FORECAST_VIEW_PATH_RE.search(path)
    if forecast_match:
        view_short, source, _measure = forecast_match.groups()
        return VIEW_DISPLAY.get(view_short, view_short), source.upper()
    return None


def _has_view_label(text: str, display: str, source: str) -> bool:
    readable = rf"{re.escape(display)}\s*·\s*{re.escape(source.upper())}\s*기준"
    compact_view = VIEW_COMPACT.get(display, display)
    compact = rf"{re.escape(compact_view)}\s*·\s*{re.escape(source.upper())}\s*·"
    return bool(re.search(readable, text or "", re.I) or re.search(compact, text or "", re.I))


def _requires_view_label(item: dict[str, Any]) -> bool:
    raw_text = str(item.get("raw_text") or "").strip()
    if STANDALONE_YEAR_RE.fullmatch(raw_text):
        return False
    if bool(item.get("ci_confidence_literal")):
        return False

    matched_path = str(item.get("matched_path") or "")
    metric_path = matched_path.split("::", 1)[0]
    if not metric_path:
        return False
    if any(part in metric_path for part in (".latest_period", ".period", ".ci_lower_95", ".ci_upper_95")):
        return False
    return bool(MARKET_METRIC_PATH_RE.search(metric_path))


def validate_view_label_policy(bundle: dict, stage_results: dict[str, StageValidation]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for stage, result in stage_results.items():
        for item in result.extracted:
            if not _requires_view_label(item):
                continue
            parts = _view_label_parts_from_path(bundle, item.get("matched_path"))
            if not parts:
                continue
            display, source = parts
            if _has_view_label(str(item.get("context_text") or ""), display, source):
                continue
            issues.append(
                _policy_issue(
                    stage,
                    item.get("context") or "view_label_policy",
                    "market_metric_missing_view_label",
                    str(item.get("raw_text") or ""),
                    f"market metric 수치에는 '{display} · {source} 기준' View/source 표기가 필요합니다.",
                )
            )
    return issues


def _iter_bundle_events(bundle: dict) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    event_bundle = bundle.get("event_bundle") or {}
    for key in ["events_brand_centric", "events_market_trend", "cross_match_events"]:
        events.extend(item for item in event_bundle.get(key, []) or [] if isinstance(item, dict))

    competitor_events = bundle.get("competitor_events") or {}
    for group_key in ["by_source", "by_view"]:
        for payload in (competitor_events.get(group_key) or {}).values():
            for comp in payload.get("competitors", []) or []:
                events.extend(item for item in comp.get("events", []) or [] if isinstance(item, dict))
    return events


def _prediction_evidence(parsed_output: dict) -> list[dict[str, Any]]:
    prediction = parsed_output.get("prediction", {}) or {}
    return [item for item in prediction.get("evidence", []) or [] if isinstance(item, dict)]


def _simulation_evidence_matches_bundle(item: dict[str, Any], bundle: dict, config: ValidatorConfig) -> bool:
    title = str(item.get("title") or "")
    basis = str(item.get("basis") or "")
    if not basis.strip():
        return False
    if not (SIMULATION_EVIDENCE_TITLE_RE.search(title) or COMPACT_VIEW_TAG_RE.search(basis)):
        return False

    basis_numbers = [
        number
        for number in extract_numbers(basis, config)
        if not bool(number.get("low_priority")) and not bool(number.get("ci_confidence_literal"))
    ]
    if not basis_numbers:
        return False

    bundle_index = build_bundle_path_index(bundle)
    return all(
        find_match_unit_aware(number["value"], bundle_index, number["number_type"], config) is not None
        for number in basis_numbers
    )


def validate_prediction_evidence_policy(parsed_output: dict, bundle: dict, config: ValidatorConfig) -> list[dict[str, Any]]:
    text = _prediction_text(parsed_output)
    bundle_events = _iter_bundle_events(bundle)
    evidence = _prediction_evidence(parsed_output)
    issues: list[dict[str, Any]] = []

    if PREDICTION_NEWS_TRIGGER_RE.search(text) and bundle_events and not evidence:
        issues.append(
            _prediction_policy_issue(
                "prediction_evidence_required",
                "",
                "prediction stage에서 뉴스/급여/허가 등 사건성 근거를 언급하면 bundle evidence를 함께 제시해야 합니다.",
            )
        )

    if evidence:
        source_ids = {str(item.get("news_id")) for item in bundle_events if item.get("news_id")}
        source_titles = {str(item.get("title")) for item in bundle_events if item.get("title")}
        for item in evidence:
            news_id = str(item.get("news_id") or "")
            title = str(item.get("title") or "")
            if (news_id and news_id in source_ids) or (title and title in source_titles):
                continue
            if _simulation_evidence_matches_bundle(item, bundle, config):
                continue
            issues.append(
                _prediction_policy_issue(
                    "prediction_evidence_not_in_bundle",
                    news_id or title or str(item.get("basis") or ""),
                    "prediction evidence는 bundle에 포함된 source event 또는 bundle 수치 basis에서만 인용할 수 있습니다.",
                )
            )
    return issues


def validate_simulation_prediction_policy(
    parsed_output: dict,
    bundle: dict,
    prediction_result: StageValidation,
) -> list[dict[str, Any]]:
    if not _forecast_simulation_available(bundle):
        return []

    text = _prediction_text(parsed_output)
    issues: list[dict[str, Any]] = []
    if not text.strip():
        issues.append(
            _prediction_policy_issue(
                "simulation_prediction_missing",
                "",
                "forecast_simulation.available=true 이지만 prediction stage 본문이 비어 있습니다.",
            )
        )
        return issues

    forbidden = SIMULATION_FORBIDDEN_SCENARIO_RE.search(text)
    if forbidden:
        issues.append(
            _prediction_policy_issue(
                "simulation_forbidden_scenario_phrase",
                forbidden.group(0),
                "forecast_simulation의 upper/lower는 95% 신뢰구간입니다.",
            )
        )

    conversion = SIMULATION_UNIT_CONVERSION_RE.search(text)
    if conversion:
        issues.append(
            _prediction_policy_issue(
                "simulation_unit_conversion",
                conversion.group(0),
                "forecast_simulation 값은 raw KRW 그대로 사용해야 하며 억/만/k/M 변환은 금지됩니다.",
            )
        )

    simulation_matches = [
        item
        for item in prediction_result.extracted
        if str(item.get("matched_path") or "").startswith("forecast_simulation.by_view.")
    ]
    if not simulation_matches:
        issues.append(
            _prediction_policy_issue(
                "simulation_not_used",
                "",
                "forecast_simulation.available=true 이지만 prediction stage에서 simulation 수치를 사용하지 않았습니다.",
            )
        )
        return issues

    if not SIMULATION_CI_RE.search(text):
        issues.append(
            _prediction_policy_issue(
                "simulation_missing_ci_wording",
                "",
                "simulation 수치를 사용할 때는 95% 신뢰구간임을 명시해야 합니다.",
            )
        )

    matched_paths = [str(item.get("matched_path") or "") for item in simulation_matches]
    for horizon in ["1y", "3y", "5y"]:
        if not any(f"horizon_{horizon}" in path for path in matched_paths):
            issues.append(
                _prediction_policy_issue(
                    f"simulation_missing_horizon_{horizon}",
                    "",
                    f"prediction stage에서 forecast_simulation horizon_{horizon} 수치를 사용해야 합니다.",
                )
            )
    return issues


def validate_output(parsed_output: dict, bundle: dict, config: ValidatorConfig) -> ValidationResult:
    bundle_index = build_bundle_path_index(bundle)
    stage_results: dict[str, StageValidation] = {}
    all_unmatched: list[dict[str, Any]] = []
    all_warnings: list[dict[str, Any]] = []
    total_extracted = 0
    total_matched = 0

    for stage in ["phenomenon", "cause", "prediction", "recommendation"]:
        stage_result = validate_stage_output(stage, parsed_output.get(stage, {}) or {}, bundle_index, config)
        stage_results[stage] = stage_result
        all_unmatched.extend(stage_result.unmatched)
        all_warnings.extend(stage_result.warnings)
        total_extracted += len(stage_result.extracted)
        total_matched += sum(1 for item in stage_result.extracted if item.get("matched_path"))

    policy_issues: list[dict[str, Any]] = []
    policy_issues.extend(
        validate_simulation_prediction_policy(
            parsed_output,
            bundle,
            stage_results.get("prediction") or StageValidation(False, [], [], []),
        )
    )
    policy_issues.extend(validate_view_label_policy(bundle, stage_results))
    policy_issues.extend(validate_prediction_evidence_policy(parsed_output, bundle, config))

    if policy_issues:
        prediction_result = stage_results.get("prediction")
        for issue in policy_issues:
            target_stage = str(issue.get("stage") or "prediction")
            target_result = stage_results.get(target_stage) or prediction_result
            if target_result:
                target_result.unmatched.append(issue)
                target_result.valid = False
        all_unmatched.extend(policy_issues)

    return ValidationResult(
        valid=not all_unmatched,
        stage_results=stage_results,
        total_numbers_extracted=total_extracted,
        total_numbers_matched=total_matched,
        unmatched_numbers=all_unmatched,
        warnings=all_warnings,
    )
