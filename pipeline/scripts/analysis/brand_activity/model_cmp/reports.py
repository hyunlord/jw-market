from __future__ import annotations

from .market_groups import filter_options_for_brand
from .models import CsdPresence, JsonValue, MarketGroupModel


def render_group_model_md(model: MarketGroupModel) -> str:
    """Render the PL-approved five-group market model for human review."""
    group_rows = []
    for group in model.groups.values():
        present = [member.kr_brand for member in group.members if member.status is CsdPresence.PRESENT]
        absent = [member.kr_brand for member in group.members if member.status is CsdPresence.ABSENT_IN_CSD]
        group_rows.append(
            [
                group.label,
                ", ".join(market.source_market for market in group.source_markets),
                ", ".join(group.atc4_set),
                ", ".join(present) or "-",
                ", ".join(absent) or "-",
            ]
        )
    option_rows = []
    for brand in ("LIVALO", "LIVALOZET", "LIVALO V", "THRUPAS", "FERINJECT", "VENOFERRUM", "WINUF A PLUS"):
        options = filter_options_for_brand(model, brand)
        option_rows.append([brand, " / ".join(f"{option.option_id}={option.label}" for option in options) or "데이터 없음"])
    return "\n".join(
        [
            "# GROUP_01_MARKET_MODEL",
            "",
            "원칙: CSD 원천 `market`은 덮어쓰지 않고, 그룹은 표시/집계용 메타데이터로만 추가한다.",
            "",
            _table(["그룹", "원천 CSD market", "ATC4", "present", "absent_in_csd"], group_rows),
            "",
            "필터 옵션은 선택 브랜드의 원천 시장 옵션을 먼저 보여주고, 그룹 소속이면 그룹 합집합 옵션을 추가한다.",
            "",
            _table(["선택 IQVIA EN", "옵션 전개"], option_rows),
            "",
            "Keyword/Meeting은 `therapeutic_class`(ATC4)를 그룹의 `atc4_set`과 bridge한다. 리바로 시장군은 C10A1+C10C0 멀티-ATC4 그룹으로 별도 취급한다.",
            "",
        ]
    )


def render_design_md(model: MarketGroupModel, payload: dict[str, JsonValue]) -> str:
    """Render comparison design, sampling, prompt, and call-plan notes."""
    call_plan = _list(payload.get("call_plan"))
    grouped_calls = _count_by(call_plan, "task")
    model_calls = _count_by(call_plan, "model_key")
    absent_count = sum(1 for group in model.groups.values() for member in group.members if member.status is CsdPresence.ABSENT_IN_CSD)
    return "\n".join(
        [
            "# MODEL_CMP_01_DESIGN",
            "",
            "목적: MI Master 5개 시장군 모델을 원천 CSD market 보존 구조로 고정하고, 동일 표본으로 Pro/Flash/Lite GenOS 3모델의 토픽 축과 브랜드 비율을 비교한다.",
            "",
            f"- 그룹 수: {len(model.groups)}",
            f"- absent_in_csd 멤버 수: {absent_count}",
            f"- 실행 모드: {_string(payload.get('execution_mode'))}",
            f"- 인증 모드: {_string(payload.get('auth_mode'))}",
            f"- 프롬프트 버전: {_string(payload.get('prompt_version'))}",
            "- 분모: 브랜드 표본 행 수 기준 주토픽 1개 배정, 기타 포함 100%",
            "",
            "표본 호출 계획",
            "",
            _table(["구분", "호출 수"], [[key, str(value)] for key, value in sorted(grouped_calls.items())]),
            "",
            _table(["모델", "호출 수"], [[key, str(value)] for key, value in sorted(model_calls.items())]),
            "",
            "시장군 축 처리",
            "",
            "- 단일 ATC4: 해당 ATC4 행으로 공통축 생성.",
            "- 리바로 시장군: C10A1+C10C0 그룹 전체 메시지로 공통축 1개를 생성해 브랜드×토픽 매트릭스의 축 일관성을 우선한다.",
            "- 대안: ATC4별 축 생성 후 병합. 운영 후보이나 이번 표본에서는 모델 간 비교 변수를 줄이기 위해 선택하지 않았다.",
            "",
            "원문 정책",
            "",
            "- 프롬프트에는 표본 원문을 포함한다.",
            "- audit/docs에는 원문을 저장하지 않고 row_ref, SHA256, 길이, 집계만 저장한다.",
            "- 생성물 전체에 대해 표본 원문 exact leakage scan을 수행한다.",
            "",
        ]
    )


def render_results_md(payload: dict[str, JsonValue]) -> str:
    """Render the three-model PoC result tables without raw message text."""
    return "\n".join(
        [
            "# MODEL_CMP_02_RESULTS",
            "",
            f"실행 상태: {_string(payload.get('execution_status'))}",
            "",
            "모델별 비용/지연",
            "",
            _table(["모델", "호출", "prompt", "completion", "total", "평균 지연(ms)"], _token_rows(payload)),
            "",
            "비결정성(temperature 0 동일 입력 2회)",
            "",
            _table(["모델", "대표 브랜드", "최대 변동폭(pp)", "상태"], _nondet_rows(payload)),
            "",
            "시장/시장군 공통 토픽 축",
            "",
            _table(["scope", "모델", "토픽 수", "라벨"], _axis_rows(payload)),
            "",
            "브랜드별 비율 샘플",
            "",
            _table(["scope/brand", "모델", "Top shares", "기타"], _brand_rows(payload)),
            "",
            "사전 baseline 요약",
            "",
            _table(["표본", "행 수", "상위 사전 hit"], _dictionary_rows(payload)),
            "",
        ]
    )


def render_reco_md(payload: dict[str, JsonValue]) -> str:
    """Render operational recommendation and unresolved PL questions."""
    open_questions = _list(payload.get("open_questions"))
    return "\n".join(
        [
            "# MODEL_CMP_03_RECO",
            "",
            f"튜닝 vs 로직개선 판단: {_string(payload.get('tuning_vs_logic'))}",
            "",
            f"모델 권고: {_string(payload.get('model_recommendation'))}",
            "",
            "사전 대비 결론",
            "",
            _string(payload.get("dictionary_comparison")),
            "",
            "운영 메모",
            "",
            "- 시장 공통축은 월별 데이터 스냅샷 해시와 프롬프트 버전으로 캐시한다.",
            "- 브랜드 비율은 `(브랜드, scope, axis_version, input_hash, model)` 단위 캐시가 필요하다.",
            "- dev gateway no-auth 허용 여부는 PoC 사실로만 기록하고 운영 bearer 정책은 PL/GenOS 확인 항목으로 남긴다.",
            "",
            "미결 질문",
            "",
            "\n".join(f"- {question}" for question in open_questions) if open_questions else "- 없음",
            "",
        ]
    )


def _token_rows(payload: dict[str, JsonValue]) -> list[list[str]]:
    """Build model cost rows from sanitized call logs."""
    rows = []
    for model, metrics in _dict(payload.get("token_latency_by_model")).items():
        item = _dict(metrics)
        rows.append(
            [
                model,
                str(item.get("calls", 0)),
                str(item.get("prompt_tokens", 0)),
                str(item.get("completion_tokens", 0)),
                str(item.get("total_tokens", 0)),
                str(item.get("avg_latency_ms", 0)),
            ]
        )
    return rows


def _nondet_rows(payload: dict[str, JsonValue]) -> list[list[str]]:
    """Build nondeterminism rows from repeat-call comparisons."""
    rows = []
    for model, value in _dict(payload.get("nondeterminism")).items():
        item = _dict(value)
        rows.append([model, _string(item.get("sample_key")), _string(item.get("max_delta_pp")), _string(item.get("status"))])
    return rows


def _axis_rows(payload: dict[str, JsonValue]) -> list[list[str]]:
    """Build model axis rows from sanitized topic payloads."""
    rows = []
    for model, scopes in _dict(payload.get("axis_results")).items():
        for scope, axis_payload in _dict(scopes).items():
            topics = _list(_dict(axis_payload).get("topics"))
            rows.append([scope, model, str(len(topics)), ", ".join(_string(_dict(topic).get("label")) for topic in topics)])
    return rows


def _brand_rows(payload: dict[str, JsonValue]) -> list[list[str]]:
    """Build brand-share rows from normalized model payloads."""
    rows = []
    for model, brands in _dict(payload.get("brand_results")).items():
        for sample_key, brand_payload in _dict(brands).items():
            item = _dict(brand_payload)
            shares = _list(item.get("topic_shares"))
            summary = ", ".join(f"{_string(_dict(share).get('label'))}:{_string(_dict(share).get('share_pct'))}%" for share in shares[:4])
            rows.append([sample_key, model, summary, f"{_string(item.get('etc_pct'))}%"])
    return rows


def _dictionary_rows(payload: dict[str, JsonValue]) -> list[list[str]]:
    """Build dictionary baseline rows for sampled brand inputs."""
    rows = []
    for sample_key, value in _dict(payload.get("dictionary_baseline")).items():
        item = _dict(value)
        topics = _list(item.get("topics"))
        top = ", ".join(f"{_string(_dict(topic).get('label'))}:{_string(_dict(topic).get('share_pct'))}%" for topic in topics[:3])
        rows.append([sample_key, str(item.get("row_count", 0)), top or "-"])
    return rows[:18]


def _count_by(items: list[JsonValue], key: str) -> dict[str, int]:
    """Count JSON dict items by a stable string key."""
    counts: dict[str, int] = {}
    for item in items:
        value = _string(_dict(item).get(key))
        counts[value] = counts.get(value, 0) + 1
    return counts


def _dict(value: JsonValue) -> dict[str, JsonValue]:
    """Return a JSON dict or an empty dict."""
    return value if isinstance(value, dict) else {}


def _list(value: JsonValue) -> list[JsonValue]:
    """Return a JSON list or an empty list."""
    return value if isinstance(value, list) else []


def _string(value: JsonValue) -> str:
    """Render JSON scalar values for Markdown cells."""
    return "" if value is None else str(value).replace("\n", " ")


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a compact GitHub-flavored Markdown table."""
    safe_rows = rows or [["-" for _ in headers]]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(cell.replace("|", "/") for cell in row) + " |" for row in safe_rows)
    return "\n".join(lines)
