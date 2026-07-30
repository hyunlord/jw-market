from __future__ import annotations

import json
from pathlib import Path

from agent2_regen_orchestrator import validate_formatter_contract
from phase_zeta_runner.genos_caller import parse_genos_response, validate_genos_output


FIXTURE = Path(__file__).parent / "fixtures" / "ubist_2026_05_twelve_brand_contracts.json"
DOMINA_TEXT_FIXTURE = (
    Path(__file__).parent / "fixtures" / "ubist_2026_05_domina_genos_text.txt"
)
MODE_COUNTS = {
    "full": (4, 4),
    "compact": (2, 2),
    "recap": (1, 1),
}


def _stage(mode: str, body: str = "") -> dict:
    bullet_count, sentence_count = MODE_COUNTS[mode]
    sentences = " ".join("문장입니다." for _ in range(sentence_count))
    return {
        "title": "제목",
        "body": f"{sentences} {body}".strip(),
        "bullets": [f"항목 {index}" for index in range(bullet_count)],
        "evidence": [],
    }


def _parsed(mode: str, body: str = "") -> dict:
    return {
        stage: _stage(mode, body)
        for stage in ("phenomenon", "cause", "prediction", "recommendation")
    }


def _response_text(mode: str, recommendation_body: str, recommendation_bullets: int | None = None) -> str:
    parsed = _parsed(mode)
    parsed["recommendation"]["body"] = recommendation_body
    if recommendation_bullets is not None:
        parsed["recommendation"]["bullets"] = [
            f"항목 {index}" for index in range(recommendation_bullets)
        ]
    text = json.dumps(parsed, ensure_ascii=False)
    return text.replace(
        '\\"기미 크림, 제대로 바르고 있나요?\\"',
        '"기미 크림, 제대로 바르고 있나요?"',
    )


def test_previous_six_are_pinned_to_actual_validated_rerun_records() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert [item["brand"] for item in fixture["previously_fixed"]] == [
        "가리온",
        "게보린릴랙스",
        "뉴베인",
        "글리파엠",
        "뉴부틴",
        "누리그라",
    ]
    assert all(item["status"] == "validated" for item in fixture["previously_fixed"])
    assert all(item["bundle_hash"].startswith("sha256:") for item in fixture["previously_fixed"])


def test_six_new_failure_fixtures_pass_the_corrected_contracts() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    passed: list[str] = []

    for item in fixture["new_failures"]:
        mode = item["mode"]
        if item["kind"] == "tiny_percentage":
            result = validate_formatter_contract(
                _parsed(mode, " ".join(item["values"])),
                brand=item["brand"],
                mode=mode,
            )
            assert not [
                error for error in result.errors if error["type"] == "three_plus_decimal"
            ], item["brand"]
        elif item["kind"] == "bullet_overflow":
            parsed = parse_genos_response(
                {
                    "data": {
                        "text": _response_text(
                            mode,
                            "요약입니다.",
                            recommendation_bullets=item["bullet_count"],
                        )
                    }
                },
                mode=mode,
            )
            assert len(parsed["recommendation"]["bullets"]) == 2
            assert validate_genos_output(parsed, mode=mode)["valid"]
        elif item["kind"] == "unescaped_inner_quote":
            parsed = parse_genos_response(
                {
                    "data": {
                        "text": DOMINA_TEXT_FIXTURE.read_text(encoding="utf-8"),
                    }
                },
                mode=mode,
            )
            assert '"기미 크림, 제대로 바르고 있나요?"' in parsed["recommendation"]["body"]
        else:
            raise AssertionError(f"unknown fixture kind: {item['kind']}")
        passed.append(item["brand"])

    assert passed == [
        "글로틴 듀오",
        "네오칼시돌",
        "니세르코드",
        "니페디온CR",
        "덱사메타",
        "도미나",
    ]
