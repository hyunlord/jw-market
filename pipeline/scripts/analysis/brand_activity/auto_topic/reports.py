from __future__ import annotations

# noqa: SIZE_OK - Markdown report renderer keeps artifact sections in one auditable surface.

from .models import JsonValue


def render_quality_md(payload: dict[str, JsonValue]) -> str:
    """Render AUTO_01 with measured 17-market axes, sampled shares, and grades."""
    quality = _dict(payload.get("quality_summary"))
    return "\n".join(
        [
            "# AUTO_01_QUALITY",
            "",
            f"실행 모드: {_string(payload.get('execution_mode'))}",
            f"인증 모드: {_string(payload.get('auth_mode'))}",
            f"시장 수: {_string(payload.get('market_count'))}",
            f"분류 브랜드 수: {_string(payload.get('sampled_brand_count'))}",
            f"계획 호출 수: {_string(_dict(payload.get('plan_summary')).get('planned_call_count'))}",
            f"계획 입력 토큰(추정): {_string(_dict(payload.get('plan_summary')).get('estimated_input_tokens'))}",
            "",
            "GenOS 호출 상태",
            "",
            _table(["상태", "호출 수"], [[key, str(value)] for key, value in _count_by(_list(payload.get("call_logs")), "status").items()]),
            "",
            f"대표 오류: {_representative_error(payload)}",
            "",
            "품질 등급 분포",
            "",
            _table(["등급", "시장 수"], [[grade, str(count)] for grade, count in _dict(quality.get("grade_distribution")).items()]),
            "",
            f"기타비율 평균: {_string(quality.get('average_etc_pct'))}%",
            f"복합 라벨 수(및/슬래시/쉼표): {_string(_dict(quality.get('label_quality')).get('complex_label_count'))}",
            f"특화 근접중복 쌍 수: {_string(_dict(quality.get('label_quality')).get('brand_specific_duplicate_pair_count'))}",
            "",
            "스케일 처리 요약",
            "",
            _scale_section(payload),
            "",
            "CSD 영문 시장명 플래그",
            "",
            f"- 드롭된 ATC4(CSD 시트 없음): {', '.join(_string(item) for item in _list(_dict(payload.get('group_map')).get('dropped_atc4_csd_missing'))) or '없음'}",
            f"- 키워드 데이터 없는 CSD 시장: {', '.join(_string(item) for item in _list(_dict(payload.get('group_map')).get('csd_markets_without_keyword_data'))) or '없음'}",
            "",
            "시장별 품질",
            "",
            _table(["시장", "scope", "등급", "축 n", "분류 브랜드", "평균 기타", "사유"], [[_string(row.get("display_name") or row.get("atc4")), _string(row.get("scope_id") or row.get("atc4")), _string(row.get("quality_grade")), _string(row.get("axis_row_count")), _string(row.get("sampled_brand_count")), _string(row.get("avg_etc_pct")), ", ".join(_string(reason) for reason in _list(row.get("reasons")))] for row in _list(quality.get("markets"))]),
            "",
            "Flash 시장축",
            "",
            _table(["시장", "scope", "축 n", "chunk", "토픽 수", "라벨"], _axis_rows(payload)),
            "",
            "표본 브랜드 비율",
            "",
            _table(["시장/브랜드", "source ATC4", "분류 n", "batch", "Top shares", "기타", "QC"], _brand_rows(payload)),
            "",
            "대형 시장 모델등급 재확인",
            "",
            _table(["비교", "Flash 유사도"], [[key, _string(_dict(value).get("vs_flash_similarity"))] for key, value in _dict(payload.get("tier_axis_similarity")).items()]),
            "",
            "MI Master 그룹 도출",
            "",
            _group_map_section(payload),
            "",
        ]
    )


def render_pipeline_md(payload: dict[str, JsonValue]) -> str:
    """Render AUTO_02 pipeline design and operational options."""
    return "\n".join(
        [
            "# AUTO_02_PIPELINE",
            "",
            "## 배치 흐름",
            "",
            "1. 최근 1년 또는 사용 가능한 10개월 Keyword 행을 시장/시장군별로 read-only 수집한다.",
            "2. 시장축은 전수 행을 토큰 예산 단위 chunk로 나누어 후보축을 생성한 뒤 raw-text-free merge로 5~8개 축을 통합한다.",
            "3. 이전 배치 축과 유사도 >= 0.8이면 이전 `axis_version`과 `topic_id`를 유지하고, 낮으면 `axis_version`을 증가시킨다.",
            "4. 브랜드별 전수 행은 토큰 예산 단위 batch로 분류한 뒤 성공 batch의 topic row_count를 합산한다. 분모는 실제 성공 분류 행 수 기준 주토픽 1개 배정이며 기타 포함 100%다.",
            "5. 기계적 가드, 급변 감지, REDESIGN 사전 교차검증을 수행하고 실패분은 플래그/격리한다.",
            "6. 시장축, 브랜드 비율, QC 결과, 배치 메타, 입력 해시를 저장한다.",
            "",
            "## 저장 스키마 초안",
            "",
            "```json",
            '{ "market_axis": { "scope_id": "atc4:C10C0", "axis_version": "v4", "topics": [], "stability": {} }, "brand_share": { "brand": "LIVALOZET", "topic_shares": [], "etc_pct": 0, "qc": {}, "input_hash": "sha256" }, "batch_meta": { "model": "flash", "prompt_version": "auto_topic_v1", "token_usage": {} } }',
            "```",
            "",
            "## 시장 그룹 메모",
            "",
            _table(["그룹", "ATC4", "규칙"], _group_scope_rows(payload)),
            "",
            "## 호출 계획",
            "",
            _table(["구분", "호출 수"], [[key, str(value)] for key, value in _count_by(_list(payload.get("call_plan")), "task").items()]),
            "",
            f"- 계획 입력 토큰(추정): {_string(_dict(payload.get('plan_summary')).get('estimated_input_tokens'))}",
            f"- 예상 시간(5s/call): {_string(_dict(payload.get('plan_summary')).get('rough_wall_time_minutes_at_5s_per_call'))}분",
            f"- 예상 시간(15s/call): {_string(_dict(payload.get('plan_summary')).get('rough_wall_time_minutes_at_15s_per_call'))}분",
            "",
            "## 운영 메모",
            "",
            "- stage 직접 집계는 PoC와 월간 배치에는 충분하지만, 축/비율 결과는 버전 이력과 QC 때문에 별도 결과 테이블 또는 cache JSON이 필요하다.",
            "- LLM 호출은 SSH 2-hop port-forward로 노출한 serving-direct OpenAI 호환 엔드포인트만 사용한다.",
            "- 완전 자동화는 유지하되 축 변경 이력과 QC 플래그를 사람이 사후 검토할 수 있게 저장한다.",
            "",
            "## 미결 질문",
            "",
            "\n".join(f"- {question}" for question in _list(payload.get("open_questions"))) or "- 없음",
            "",
        ]
    )


def render_stability_md(payload: dict[str, JsonValue]) -> str:
    """Render AUTO_03 stability and three-layer QC PoC evidence."""
    stability = _dict(payload.get("stability_results"))
    artificial = _dict(stability.get("quality_gate_artificial_anomalies"))
    return "\n".join(
        [
            "# AUTO_03_STABILITY_POC",
            "",
            "축 안정화 측정",
            "",
            _table(["ATC4", "repeat similarity", "action", "threshold"], _stability_rows(stability)),
            "",
            "적용 효과",
            "",
            "- 유사도 임계값 0.8 이상이면 이전 축의 라벨/topic_id를 유지해 시계열 비교 축을 보존한다.",
            "- 임계값 미만이면 `axis_version`을 증가시키고 변경 배치부터 새 축을 적용한다.",
            "- 이전 축 버전의 과거 시계열은 재라벨링하지 않아 혼용을 막는다.",
            "",
            "품질 자동검증 3겹 인위적 이상치 테스트",
            "",
            _table(["레이어", "상태", "측정값/사유"], [[key, _string(_dict(value).get("status")), _string(_dict(value).get("reasons") or _dict(value).get("max_delta_pp") or _dict(value).get("overlap"))] for key, value in artificial.items()]),
            "",
        ]
    )


def report_payload(
    *,
    execution_mode: str,
    auth_mode: str,
    market_count: int,
    sampled_brand_count: int,
    call_plan: list[JsonValue],
    execution_summary: dict[str, JsonValue],
    quality: dict[str, JsonValue],
    group_map: dict[str, JsonValue] | None = None,
    scope_metadata: dict[str, JsonValue] | None = None,
    sample_summary: dict[str, JsonValue] | None = None,
    plan_summary: dict[str, JsonValue] | None = None,
    csd_bridge: dict[str, JsonValue] | None = None,
    open_questions: list[str],
) -> dict[str, JsonValue]:
    """Assemble the common report payload from measured execution and static metadata."""
    return {
        **execution_summary,
        "execution_mode": execution_mode,
        "auth_mode": auth_mode,
        "market_count": market_count,
        "sampled_brand_count": sampled_brand_count,
        "call_plan": call_plan,
        "quality_summary": quality,
        "group_map": group_map or {},
        "scope_metadata": scope_metadata or {},
        "sample_summary": sample_summary or {},
        "plan_summary": plan_summary or {},
        "csd_bridge": csd_bridge or {},
        "open_questions": open_questions,
    }


def _axis_rows(payload: dict[str, JsonValue]) -> list[list[str]]:
    """Build Markdown rows for Flash market axes only."""
    rows: list[list[str]] = []
    for key, value in _dict(payload.get("axis_results")).items():
        if key.endswith(":pro") or key.endswith(":lite"):
            continue
        topics = _list(_dict(value).get("topics"))
        chunking = _dict(_dict(value).get("chunking"))
        rows.append([_string(_dict(value).get("display_name") or key), _string(_dict(value).get("scope_id") or key), _string(_dict(value).get("source_row_count")), _string(chunking.get("chunk_count")), str(len(topics)), ", ".join(_string(_dict(topic).get("label")) for topic in topics)])
    return rows


def _brand_rows(payload: dict[str, JsonValue]) -> list[list[str]]:
    """Build Markdown rows for sampled Flash brand shares."""
    rows: list[list[str]] = []
    for key, value in _dict(payload.get("brand_results")).items():
        if key.endswith(":pro") or key.endswith(":lite"):
            continue
        item = _dict(value)
        shares = [*_list(item.get("topic_shares")), *_list(item.get("brand_specific_topics"))]
        top = ", ".join(f"{_string(_dict(share).get('label'))}:{_string(_dict(share).get('share_pct'))}%" for share in shares[:5])
        qc = _string(_dict(_dict(item.get("qc")).get("guard")).get("status"))
        batching = _dict(item.get("batching"))
        rows.append([f"{_string(item.get('display_name') or item.get('scope_key'))}/{_string(item.get('brand'))}", _string(item.get("atc4")), _string(item.get("row_count")), _string(batching.get("batch_count") or batching.get("chunk_count")), top, f"{_string(item.get('etc_pct'))}%", qc])
    return rows


def _group_map_section(payload: dict[str, JsonValue]) -> str:
    """Render MI Master sanity and fallback evidence."""
    group_map = _dict(payload.get("group_map"))
    sanity = _dict(group_map.get("sanity_checks"))
    missing = ", ".join(_string(item) for item in _list(group_map.get("mi_master_missing_atc4"))) or "없음"
    return "\n".join(
        [
            f"- sanity: {_string(sanity.get('status'))} (리바로 C10A1+C10C0={_string(sanity.get('livalo_C10A1_C10C0_grouped'))}, 가드렛 A10N1+A10N3={_string(sanity.get('gardlet_A10N1_A10N3_grouped'))})",
            f"- 최종 시장 scope: {', '.join(_string(_dict(row).get('display_name') or key) for key, row in _dict(payload.get('scope_metadata')).items()) or '없음'}",
            f"- MI Master 없는 ATC4 standalone: {missing}",
            f"- 드롭된 ATC4(CSD 시트 없음): {', '.join(_string(item) for item in _list(group_map.get('dropped_atc4_csd_missing'))) or '없음'}",
            f"- 키워드 데이터 없는 CSD 시장: {', '.join(_string(item) for item in _list(group_map.get('csd_markets_without_keyword_data'))) or '없음'}",
        ]
    )


def _scale_section(payload: dict[str, JsonValue]) -> str:
    """Render full-row/chunk scale evidence from sanitized summaries."""
    sample = _dict(payload.get("sample_summary"))
    axis_rows = _dict(sample.get("axis"))
    brand_rows = _dict(sample.get("brand"))
    largest_axis = sorted(axis_rows.items(), key=lambda item: int(_dict(item[1]).get("row_count") or 0), reverse=True)[:5]
    largest_brand = sorted(brand_rows.items(), key=lambda item: int(_dict(item[1]).get("row_count") or 0), reverse=True)[:8]
    return "\n".join(
        [
            f"- sample mode: {_string(sample.get('mode'))}",
            "",
            _table(["축 scope", "축 메시지 n", "토큰 추정"], [[key, _string(_dict(value).get("row_count")), _string(_dict(value).get("estimated_input_tokens"))] for key, value in largest_axis]),
            "",
            _table(["브랜드 scope", "분류 메시지 n", "토큰 추정"], [[key, _string(_dict(value).get("row_count")), _string(_dict(value).get("estimated_input_tokens"))] for key, value in largest_brand]),
        ]
    )


def _group_scope_rows(payload: dict[str, JsonValue]) -> list[list[str]]:
    """Render group scope rows from the generated group_map."""
    rows: list[list[str]] = []
    for group in _list(_dict(payload.get("group_map")).get("groups")):
        item = _dict(group)
        current_atc4 = ", ".join(_string(value) for value in _list(item.get("current_atc4")))
        if len(_list(item.get("current_atc4"))) > 1:
            rule = "MI Master 묶음 하나의 시장 + 원천 ATC4 보존"
        else:
            rule = "CSD 범위 내 단일 시장"
        rows.append([_string(item.get("group_name")), current_atc4, rule])
    return rows


def _stability_rows(stability: dict[str, JsonValue]) -> list[list[str]]:
    """Build stability rows while skipping the artificial anomaly bundle."""
    rows: list[list[str]] = []
    for atc4, value in stability.items():
        if atc4 == "quality_gate_artificial_anomalies":
            continue
        item = _dict(value)
        stabilized = _dict(item.get("stabilized"))
        stability_meta = _dict(stabilized.get("stability"))
        rows.append([atc4, _string(item.get("repeat_similarity")), _string(stability_meta.get("action")), _string(stability_meta.get("threshold"))])
    return rows


def _count_by(items: list[JsonValue], key: str) -> dict[str, int]:
    """Count JSON object rows by one string field."""
    result: dict[str, int] = {}
    for item in items:
        value = _string(_dict(item).get(key))
        result[value] = result.get(value, 0) + 1
    return result


def _representative_error(payload: dict[str, JsonValue]) -> str:
    """Render one representative GenOS error message when calls failed."""
    for item in _list(payload.get("call_logs")):
        row = _dict(item)
        if row.get("status") != "ok" and row.get("error_message"):
            return _string(row.get("error_message"))
    return "-"


def _dict(value: JsonValue) -> dict[str, JsonValue]:
    """Return a JSON object or an empty object."""
    return value if isinstance(value, dict) else {}


def _list(value: JsonValue) -> list[JsonValue]:
    """Return a JSON array or an empty array."""
    return value if isinstance(value, list) else []


def _string(value: JsonValue) -> str:
    """Render a JSON value for a Markdown table cell."""
    return "" if value is None else str(value).replace("\n", " ").replace("|", "/")


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a compact GitHub-flavored Markdown table."""
    safe_rows = rows or [["-" for _ in headers]]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(cell for cell in row) + " |" for row in safe_rows)
    return "\n".join(lines)
