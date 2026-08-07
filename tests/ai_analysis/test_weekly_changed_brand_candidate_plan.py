from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.scripts.ai_analysis.weekly_changed_brand_candidate_plan import (
    PlanningInputError,
    build_weekly_plan,
    main,
)

JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


def test_build_weekly_plan_emits_only_changed_brand_keys_in_stable_order(tmp_path: Path) -> None:
    manifest = tmp_path / "changed.json"
    output = tmp_path / "plan.json"
    brand_keys = tmp_path / "brand_keys.json"
    manifest.write_text(
        json.dumps(
            {
                "week": "2026-W32",
                "changed_brands": [
                    {"brand_key": "z-brand", "canonical_brand_name": "Zeta"},
                    {"brand_key": "a-brand", "canonical_brand_name": "Alpha"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    plan = build_weekly_plan(manifest, output, brand_keys)

    assert plan["brand_keys"] == ["a-brand", "z-brand"]
    assert json.loads(brand_keys.read_text(encoding="utf-8")) == ["a-brand", "z-brand"]
    assert json.loads(output.read_text(encoding="utf-8")) == plan
    for command in plan["commands"]:
        argv = command["argv"]
        assert "pipeline.scripts.ai_analysis.agent2_regen_orchestrator" in argv
        assert "--brand-keys-file" in argv
        assert str(brand_keys) in argv
        assert "--dry-run" in argv
        assert "--apply" not in argv
        assert "agent2_variant_promotion" not in " ".join(argv)


@pytest.mark.parametrize(
    "payload",
    (
        {"week": "2026-W32", "changed_brands": []},
        {"week": "2026-W32", "changed_brands": [{"brand_key": "", "canonical_brand_name": "Alpha"}]},
        {
            "week": "2026-W32",
            "changed_brands": [
                {"brand_key": "a-brand", "canonical_brand_name": "Alpha"},
                {"brand_key": "a-brand", "canonical_brand_name": "Alpha 2"},
            ],
        },
        {"week": "2026-08-07", "changed_brands": [{"brand_key": "a-brand", "canonical_brand_name": "Alpha"}]},
    ),
)
def test_build_weekly_plan_fails_closed_for_invalid_inputs(tmp_path: Path, payload: dict[str, JsonValue]) -> None:
    manifest = tmp_path / "changed.json"
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(PlanningInputError):
        build_weekly_plan(manifest, tmp_path / "plan.json", tmp_path / "brand_keys.json")

    assert not (tmp_path / "plan.json").exists()
    assert not (tmp_path / "brand_keys.json").exists()


def test_main_returns_nonzero_without_partial_outputs_on_invalid_input(tmp_path: Path) -> None:
    manifest = tmp_path / "changed.json"
    output = tmp_path / "plan.json"
    brand_keys = tmp_path / "brand_keys.json"
    manifest.write_text(json.dumps({"week": "2026-W32", "changed_brands": []}), encoding="utf-8")

    result = main(
        [
            "--changed-brands",
            str(manifest),
            "--output",
            str(output),
            "--brand-keys-output",
            str(brand_keys),
        ]
    )

    assert result == 2
    assert not output.exists()
    assert not brand_keys.exists()
