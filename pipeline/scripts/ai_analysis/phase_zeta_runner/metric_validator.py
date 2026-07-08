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
        "name": "plain_decimal",
        "pattern": re.compile(r"(?<![\d,])(-?\d+\.\d+)(?!\s*[%\d,])"),
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
VIEW_LABEL_IN_TEXT_RE = re.compile(
    r"(Market\s+Landscape|Competitive\s+Dynamics)\s*·\s*(UBIST|IQVIA)\s*기준",
    re.IGNORECASE,
)
SOURCE_IN_TEXT_RE = re.compile(r"(?<![A-Z])(UBIST|IQVIA)(?![A-Z])", re.IGNORECASE)
HORIZON_TEXT_RE = {
    "1y": re.compile(r"(?:1\s*년\s*후|1y|horizon_1y)", re.IGNORECASE),
    "3y": re.compile(r"(?:3\s*년\s*후|3y|horizon_3y)", re.IGNORECASE),
    "5y": re.compile(r"(?:5\s*년\s*후|5y|horizon_5y)", re.IGNORECASE),
}
PREDICTION_NEWS_TRIGGER_RE = re.compile(
    r"(뉴스|보도|급여|허가|출시|신약|경쟁사|진입|임상|학회|약가|정책|규제|공세|시장\s*변화)",
    re.IGNORECASE,
)
NUMERIC_EVIDENCE_TAG_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:KRW|원|₩)?\s*\([^)]*(?:ML|CD|Market\s+Landscape|Competitive\s+Dynamics|UBIST|IQVIA)",
    re.IGNORECASE,
)
NUMERIC_EVIDENCE_VALUE_RE = re.compile(r"(?<![\d,.])(\d+(?:,\d{3})*(?:\.\d+)?)(?![\d,.])")
MIN_EVENT_TITLE_MATCH_CHARS = 8
VIEW_DISPLAY = {
    "ML": "Market Landscape",
    "CD": "Competitive Dynamics",
    "market_landscape": "Market Landscape",
    "competitive_dynamics": "Competitive Dynamics",
}


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


def _is_approximate_rank_expression(raw_text: str, full_context: str) -> bool:
    nearby = _nearby_context(raw_text, full_context, before=12, after=12)
    after = nearby.split(raw_text, 1)[1] if raw_text in nearby else ""
    return bool(re.match(r"^\s*권", after))


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
                    or _is_qualified_threshold(raw_text, text or "")
                    or (number_type == "rank" and _is_approximate_rank_expression(raw_text, text or "")),
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


def _path_taxonomy(path: str | None) -> str:
    value = str(path or "")
    if not value:
        return "unknown"
    if (
        ".horizon_ci_levels." in value
        or ".ci_lower_95" in value
        or ".ci_upper_95" in value
        or value.endswith(".confidence_level")
    ):
        return "ci_metadata"
    if (
        "bundle_meta" in value
        or "config_version" in value
        or "bundle_hash" in value
        or "snapshot_at" in value
        or "created_at" in value
        or "updated_at" in value
    ):
        return "bundle_meta"
    if re.search(r"(?:^|\.)(?:period|date|published_date|year|month|quarter)(?:$|[.:])", value):
        return "date_or_period"
    if re.search(r"(?:rank|rank_in_market|순위)", value, flags=re.IGNORECASE):
        return "rank"
    if value.startswith("forecast_simulation.by_view."):
        return "forecast_base"
    if value.startswith("market_views["):
        return "metric"
    if value.startswith("event_bundle.") or value.startswith("competitor_events."):
        return "event_text"
    return "low_priority"


BLOCKING_NUMERIC_TAXONOMIES = {"metric", "forecast_base", "rank", "event_text"}
PATH_TAXONOMY_PRIORITY = {
    "forecast_base": 0,
    "metric": 1,
    "rank": 2,
    "event_text": 3,
    "ci_metadata": 8,
    "date_or_period": 9,
    "low_priority": 10,
    "bundle_meta": 11,
    "unknown": 12,
}


def _path_priority(path: str | None) -> int:
    return PATH_TAXONOMY_PRIORITY.get(_path_taxonomy(path), PATH_TAXONOMY_PRIORITY["unknown"])


def _is_blocking_numeric_path(path: str | None) -> bool:
    return _path_taxonomy(path) in BLOCKING_NUMERIC_TAXONOMIES


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
    best_priority: int | None = None

    for bundle_value, paths in bundle_index.items():
        delta = abs(bundle_value - numeric)
        matches_absolute = delta <= tolerance
        matches_relative = abs(bundle_value) > 1000 and delta / abs(bundle_value) <= relative_tolerance
        if not (matches_absolute or matches_relative):
            continue
        path = min(paths, key=_path_priority)
        priority = _path_priority(path)
        if (
            best_delta is None
            or delta < best_delta
            or (delta == best_delta and (best_priority is None or priority < best_priority))
        ):
            best_delta = delta
            best_priority = priority
            best_path = path
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
                "matched_path_taxonomy": _path_taxonomy(matched_path),
                "low_priority": item["low_priority"],
            }
            extracted.append(record)
            if matched_path is None:
                if item["low_priority"]:
                    warnings.append(record)
                else:
                    unmatched.append(record)
            elif not _is_blocking_numeric_path(matched_path):
                warnings.append(record)

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
    del display
    return bool(re.search(rf"(?<![A-Z0-9]){re.escape(source.upper())}(?![A-Z0-9])", text or "", re.I))


def _is_ci_metadata_path(matched_path: str | None) -> bool:
    return _path_taxonomy(matched_path) == "ci_metadata"


def _view_label_from_text(text: str) -> tuple[str | None, str | None]:
    label_match = VIEW_LABEL_IN_TEXT_RE.search(text or "")
    if label_match:
        return VIEW_DISPLAY.get(label_match.group(1), label_match.group(1)), label_match.group(2).upper()
    sources = {match.group(1).upper() for match in SOURCE_IN_TEXT_RE.finditer(text or "")}
    if len(sources) == 1:
        return None, next(iter(sources))
    return None, None


def _candidate_paths_for_extracted_number(
    item: dict[str, Any],
    bundle_index: dict[float, list[str]],
) -> list[str]:
    value = item.get("value")
    if value is None:
        return []
    numeric = float(value)
    tolerance = float(item.get("tolerance") or 0.0)
    paths: list[str] = []
    for bundle_value, bundle_paths in bundle_index.items():
        delta = abs(bundle_value - numeric)
        matches_absolute = delta <= tolerance
        matches_relative = abs(bundle_value) > 1000 and delta / abs(bundle_value) <= 0.001
        if matches_absolute or matches_relative:
            paths.extend(bundle_paths)
    return sorted(paths, key=_path_priority)


def _path_matches_source_hint(bundle: dict, path: str, source_hint: str | None, display_hint: str | None) -> bool:
    if not source_hint:
        return True
    parts = _view_label_parts_from_path(bundle, path)
    if not parts:
        return False
    display, source = parts
    return source == source_hint and (display_hint is None or display == display_hint)


def _source_aware_matched_path(
    bundle: dict,
    item: dict[str, Any],
    bundle_index: dict[float, list[str]],
) -> str | None:
    matched_path = item.get("matched_path")
    display_hint, source_hint = _view_label_from_text(str(item.get("context_text") or ""))
    if not source_hint:
        return str(matched_path) if matched_path else None

    current_parts = _view_label_parts_from_path(bundle, str(matched_path) if matched_path else None)
    if current_parts and current_parts[1] == source_hint and (display_hint is None or current_parts[0] == display_hint):
        return str(matched_path)

    for candidate in _candidate_paths_for_extracted_number(item, bundle_index):
        if _is_ci_metadata_path(candidate):
            continue
        if _path_matches_source_hint(bundle, candidate, source_hint, display_hint):
            return candidate
    return str(matched_path) if matched_path else None


def _horizon_from_path(path: str) -> str | None:
    for horizon in HORIZON_TEXT_RE:
        if f"horizon_{horizon}" in path:
            return horizon
    return None


def _context_mentions_horizon(context_text: str, raw_text: str, horizon: str) -> bool:
    nearby = _nearby_context(raw_text, context_text, before=24, after=24)
    return bool(HORIZON_TEXT_RE[horizon].search(nearby))


def _simulation_candidate_paths(
    bundle: dict,
    bundle_index: dict[float, list[str]],
    item: dict[str, Any],
) -> list[tuple[str, bool]]:
    display_hint, source_hint = _view_label_from_text(str(item.get("context_text") or ""))
    paths: list[tuple[str, bool]] = []
    seen: set[str] = set()

    matched_path = str(item.get("matched_path") or "")
    if matched_path.startswith("forecast_simulation.by_view.") and _path_matches_source_hint(
        bundle, matched_path, source_hint, display_hint
    ):
        paths.append((matched_path, True))
        seen.add(matched_path)

    for candidate in _candidate_paths_for_extracted_number(item, bundle_index):
        if candidate in seen or not candidate.startswith("forecast_simulation.by_view."):
            continue
        if not _path_matches_source_hint(bundle, candidate, source_hint, display_hint):
            continue
        paths.append((candidate, False))
        seen.add(candidate)
    return paths


def _simulation_horizons_used(
    bundle: dict,
    bundle_index: dict[float, list[str]],
    extracted: list[dict[str, Any]],
) -> tuple[bool, set[str]]:
    used_horizons: set[str] = set()
    has_simulation_value = False
    for item in extracted:
        for path, is_primary_match in _simulation_candidate_paths(bundle, bundle_index, item):
            horizon = _horizon_from_path(path)
            if not horizon:
                continue
            has_simulation_value = True
            if is_primary_match or _context_mentions_horizon(
                str(item.get("context_text") or ""), str(item.get("raw_text") or ""), horizon
            ):
                used_horizons.add(horizon)
    return has_simulation_value, used_horizons


def validate_view_label_policy(bundle: dict, stage_results: dict[str, StageValidation]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    bundle_index = build_bundle_path_index(bundle)
    for stage, result in stage_results.items():
        for item in result.extracted:
            if item.get("low_priority"):
                continue
            matched_path = _source_aware_matched_path(bundle, item, bundle_index)
            if _is_ci_metadata_path(matched_path):
                continue
            parts = _view_label_parts_from_path(bundle, matched_path)
            if not parts:
                continue
            display, source = parts
            if _has_view_label(str(item.get("context_text") or ""), display, source):
                continue
            warnings.append(
                _policy_issue(
                    stage,
                    item.get("context") or "view_label_policy",
                    "market_metric_missing_view_label",
                    str(item.get("raw_text") or ""),
                    f"market metric 수치에는 '{source}' 출처 표기가 필요합니다.",
                )
            )
    return warnings


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


def _evidence_texts(item: dict[str, Any]) -> list[str]:
    return [
        str(item.get(key) or "")
        for key in ("news_id", "title", "basis", "source")
        if str(item.get(key) or "").strip()
    ]


def _matches_event_evidence(item: dict[str, Any], source_ids: set[str], source_titles: set[str]) -> bool:
    news_id = str(item.get("news_id") or "")
    if news_id and news_id in source_ids:
        return True
    for text in _evidence_texts(item):
        if text in source_titles:
            return True
        for title in source_titles:
            if len(title) >= MIN_EVENT_TITLE_MATCH_CHARS and title in text:
                return True
    return False


def _is_metric_or_simulation_path(path: str | None) -> bool:
    return _path_taxonomy(path) in {"metric", "forecast_base"}


def _evidence_numbers(text: str, config: ValidatorConfig) -> list[dict[str, Any]]:
    numbers = extract_numbers(text, config)
    seen = {str(item.get("raw_text") or "") for item in numbers}
    for match in NUMERIC_EVIDENCE_VALUE_RE.finditer(text):
        raw_text = match.group(1)
        if raw_text in seen:
            continue
        value = _coerce_number(raw_text, "float")
        number_type = classify_number_context(raw_text, text, value)
        numbers.append(
            {
                "value": value,
                "raw_text": raw_text,
                "pattern": "evidence_numeric_value",
                "number_type": number_type,
                "tolerance": _tolerance_for_type(number_type, config),
                "low_priority": False,
            }
        )
    return numbers


def _matches_numeric_evidence(item: dict[str, Any], bundle_index: dict[float, list[str]], config: ValidatorConfig) -> bool:
    text = "\n".join(_evidence_texts(item))
    _display_hint, source_hint = _view_label_from_text(text)
    if not NUMERIC_EVIDENCE_TAG_RE.search(text) and not source_hint:
        return False
    meaningful_numbers = []
    for number in _evidence_numbers(text, config):
        if number.get("low_priority"):
            continue
        meaningful_numbers.append(number)
        matched_path = find_match_unit_aware(number["value"], bundle_index, number["number_type"], config)
        if not _is_metric_or_simulation_path(matched_path):
            return False
    return bool(meaningful_numbers)


def validate_prediction_evidence_policy(parsed_output: dict, bundle: dict, config: ValidatorConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    text = _prediction_text(parsed_output)
    bundle_events = _iter_bundle_events(bundle)
    evidence = _prediction_evidence(parsed_output)
    bundle_index = build_bundle_path_index(bundle)
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if PREDICTION_NEWS_TRIGGER_RE.search(text) and bundle_events and not evidence:
        warnings.append(
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
            if _matches_event_evidence(item, source_ids, source_titles):
                continue
            if _matches_numeric_evidence(item, bundle_index, config):
                continue
            issues.append(
                _prediction_policy_issue(
                    "prediction_evidence_not_in_bundle",
                    " | ".join(_evidence_texts(item)),
                    "prediction evidence는 bundle에 포함된 source event에서만 인용할 수 있습니다.",
                )
            )
    return issues, warnings


def validate_simulation_prediction_policy(
    parsed_output: dict,
    bundle: dict,
    prediction_result: StageValidation,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not _forecast_simulation_available(bundle):
        return [], []

    text = _prediction_text(parsed_output)
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not text.strip():
        warnings.append(
            _prediction_policy_issue(
                "simulation_prediction_missing",
                "",
                "forecast_simulation.available=true 이지만 prediction stage 본문이 비어 있습니다.",
            )
        )
        return issues, warnings

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

    bundle_index = build_bundle_path_index(bundle)
    has_simulation_value, used_horizons = _simulation_horizons_used(bundle, bundle_index, prediction_result.extracted)
    if not has_simulation_value:
        warnings.append(
            _prediction_policy_issue(
                "simulation_not_used",
                "",
                "forecast_simulation.available=true 이지만 prediction stage에서 simulation 수치를 사용하지 않았습니다.",
            )
        )
        return issues, warnings

    if not SIMULATION_CI_RE.search(text):
        warnings.append(
            _prediction_policy_issue(
                "simulation_missing_ci_wording",
                "",
                "simulation 수치를 사용할 때는 95% 신뢰구간임을 명시해야 합니다.",
            )
        )

    for horizon in ["1y", "3y", "5y"]:
        if horizon not in used_horizons:
            warnings.append(
                _prediction_policy_issue(
                    f"simulation_missing_horizon_{horizon}",
                    "",
                    f"prediction stage에서 forecast_simulation horizon_{horizon} 수치를 사용해야 합니다.",
                )
            )
    return issues, warnings


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
    simulation_issues, simulation_warnings = validate_simulation_prediction_policy(
        parsed_output,
        bundle,
        stage_results.get("prediction") or StageValidation(False, [], [], []),
    )
    policy_issues.extend(simulation_issues)
    all_warnings.extend(simulation_warnings)
    all_warnings.extend(validate_view_label_policy(bundle, stage_results))
    evidence_issues, evidence_warnings = validate_prediction_evidence_policy(parsed_output, bundle, config)
    policy_issues.extend(evidence_issues)
    all_warnings.extend(evidence_warnings)

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
