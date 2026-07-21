from __future__ import annotations

import json
from pathlib import Path

from scripts.high_risk_repeat_gate import evaluate_repeats, load_manifest


MANIFEST = Path(__file__).parent / "contracts/external_tool_routing_v4/high_risk_repeat_manifest.json"


def _row(
    *,
    route: str = "llm",
    tools: tuple[str, ...] = ("get_brand_metric",),
    answer_chars: int = 1762,
    answer: str = "grounded answer",
    numeric_tokens: tuple[str, ...] = (),
    cache_hit: bool = False,
) -> dict[str, object]:
    return {
        "scope": "strategic_view",
        "route": route,
        "tools_called": list(tools),
        "tool_contracts": [
            {
                "name": name,
                "status": "ok",
                "row_count": 1,
                "data_as_of": "2026-05",
                "cache_hit": cache_hit,
            }
            for name in tools
        ],
        "answer_chars": answer_chars,
        "answer": answer,
        "answer_sha256": f"answer-{answer_chars}",
        "numeric_tokens": list(numeric_tokens),
    }


def _five(row: dict[str, object]) -> list[dict[str, object]]:
    return [dict(row) for _ in range(5)]


def _payload(overrides: dict[str, list[dict[str, object]]] | None = None) -> dict[str, object]:
    rows = {
        "B-05": _five(_row()),
        "B-03": _five(_row()),
        "market_news_negative": _five(_row(route="tool_use_agent", tools=("web_search",), answer_chars=620)),
        "C_03": _five(_row(tools=("web_search",), answer_chars=900)),
        "owner_brand_share": _five(_row(numeric_tokens=("3.76",))),
        "A_03": _five(_row(answer="HHI 253.62, UBIST 2026-05", numeric_tokens=("253.62", "2026", "05"))),
        "E1_market_hhi": _five(_row(answer="HHI 262.42", numeric_tokens=("262.42",))),
    }
    rows.update(overrides or {})
    return {"cases": rows}


def test_rpt_b05_rejects_any_web_search_escape() -> None:
    manifest = load_manifest(MANIFEST)
    escaped = _five(_row())
    escaped[3] = _row(route="tool_use_agent", tools=("web_search",), answer_chars=239)

    result = evaluate_repeats(manifest, _payload(), _payload({"B-05": escaped}))

    assert result["passed"] is False
    assert "B-05" in result["failed_candidate_cases"]
    assert "forbidden_tool:web_search" in result["cases"]["B-05"]["candidate"]["failures"]


def test_rpt_b05_accepts_five_identical_grounded_runs() -> None:
    manifest = load_manifest(MANIFEST)

    result = evaluate_repeats(manifest, _payload(), _payload())

    assert result["passed"] is True
    assert result["cases"]["B-05"]["candidate"]["variant_count"] == 1
    assert len(result["repeat_table"]) == 7 * 2 * 5


def test_rpt_news_negative_keeps_web_path_eligible() -> None:
    manifest = load_manifest(MANIFEST)
    result = evaluate_repeats(manifest, _payload(), _payload())

    news = result["cases"]["market_news_negative"]["candidate"]
    assert news["failures"] == []
    assert news["runs"][0]["tools"] == ["web_search"]


def test_rpt_only_two_approved_presentation_exceptions_exist() -> None:
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert raw["approved_presentation_exceptions"] == ["C_03", "owner_brand_share"]


def test_rpt_approved_numeric_variation_does_not_hide_contract_drift() -> None:
    manifest = load_manifest(MANIFEST)
    varied = _five(_row(tools=("web_search",), answer_chars=900))
    varied[1] = _row(tools=("web_search",), answer_chars=940)
    candidate = _payload({"C_03": varied})

    result = evaluate_repeats(manifest, _payload(), candidate)

    assert result["passed"] is True
    assert result["cases"]["C_03"]["candidate"]["presentation_variant_count"] == 2
    assert result["cases"]["C_03"]["candidate"]["contract_variant_count"] == 1


def test_rpt_approved_numeric_variation_still_rejects_tool_drift() -> None:
    manifest = load_manifest(MANIFEST)
    drifted = _five(_row(tools=("web_search",), answer_chars=900))
    drifted[2] = _row(tools=("get_brand_metric",), answer_chars=900)

    result = evaluate_repeats(manifest, _payload(), _payload({"C_03": drifted}))

    assert result["passed"] is False
    assert "contract_variants:2" in result["cases"]["C_03"]["candidate"]["failures"]


def test_rpt_golden_tokens_are_not_relaxed() -> None:
    manifest = load_manifest(MANIFEST)
    wrong_hhi = _five(_row(numeric_tokens=("260.00", "2025")))

    result = evaluate_repeats(manifest, _payload(), _payload({"E1_market_hhi": wrong_hhi}))

    assert result["passed"] is False
    assert "missing_numeric_token:262.42" in result["cases"]["E1_market_hhi"]["candidate"]["failures"]


def test_rpt_golden_period_uses_answer_surface_not_split_numeric_tokens() -> None:
    manifest = load_manifest(MANIFEST)
    missing_period = _five(_row(answer="HHI 253.62", numeric_tokens=("253.62", "2026", "05")))

    result = evaluate_repeats(manifest, _payload(), _payload({"A_03": missing_period}))

    assert result["passed"] is False
    assert "missing_answer_substring:2026-05" in result["cases"]["A_03"]["candidate"]["failures"]


def test_rpt_cache_state_does_not_change_the_semantic_tool_contract() -> None:
    manifest = load_manifest(MANIFEST)
    warm = _five(_row(cache_hit=True))
    cold = _five(_row(cache_hit=False))
    mixed = [warm[0], cold[0], warm[1], cold[1], warm[2]]

    result = evaluate_repeats(
        manifest,
        _payload({"B-03": warm}),
        _payload({"B-03": mixed}),
    )

    assert result["passed"] is True
    assert result["cases"]["B-03"]["baseline"]["contract_variant_count"] == 1
    assert result["cases"]["B-03"]["candidate"]["contract_variant_count"] == 1
