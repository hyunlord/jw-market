from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypedDict


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
WEEK_RE: Final = re.compile(r"^20\d{2}-W(?:0[1-9]|[1-4]\d|5[0-3])$")
ORCHESTRATOR_MODULE: Final = "pipeline.scripts.ai_analysis.agent2_regen_orchestrator"
PLAN_VERSION: Final = "agent2-weekly-changed-brand-candidate-plan-v1"
PYTHON_BIN: Final = "python3"
VARIANTS: Final = ("short", "long")


class PlanningInputError(RuntimeError):
    """Raised when weekly changed-brand input is unsafe to plan from."""


class CommandPayload(TypedDict):
    description: str
    argv: list[str]


class PlanPayload(TypedDict):
    plan_version: str
    week: str
    brand_count: int
    brand_keys: list[str]
    brand_key_source: str
    promotion_invoked: bool
    commands: list[CommandPayload]


@dataclass(frozen=True, slots=True)
class ChangedBrand:
    brand_key: str
    canonical_brand_name: str


@dataclass(frozen=True, slots=True)
class WeeklyChangedBrands:
    week: str
    brands: tuple[ChangedBrand, ...]


def _load_json(path: Path) -> dict[str, JsonValue]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlanningInputError(f"changed-brand manifest does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PlanningInputError(f"changed-brand manifest is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PlanningInputError("changed-brand manifest must be a JSON object")
    return raw


def _string_field(row: dict[str, JsonValue], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PlanningInputError(f"changed_brands rows require non-empty {field}")
    return value.strip()


def _parse_changed_brand(value: JsonValue) -> ChangedBrand:
    if not isinstance(value, dict):
        raise PlanningInputError("changed_brands must contain objects")
    return ChangedBrand(
        brand_key=_string_field(value, "brand_key"),
        canonical_brand_name=_string_field(value, "canonical_brand_name"),
    )


def _parse_manifest(path: Path) -> WeeklyChangedBrands:
    raw = _load_json(path)
    week = raw.get("week")
    if not isinstance(week, str) or WEEK_RE.fullmatch(week) is None:
        raise PlanningInputError("week must use ISO form YYYY-Www")
    rows = raw.get("changed_brands")
    if not isinstance(rows, list) or not rows:
        raise PlanningInputError("changed_brands must be a non-empty array")
    brands = tuple(_parse_changed_brand(row) for row in rows)
    keys = [brand.brand_key for brand in brands]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise PlanningInputError(f"changed_brands contains duplicate brand_key values: {duplicates}")
    return WeeklyChangedBrands(week=week, brands=tuple(sorted(brands, key=lambda brand: brand.brand_key)))


def _command(variant: str, brand_keys_output: Path, work_dir_root: Path, week: str) -> CommandPayload:
    return {
        "description": f"Agent2 {variant} weekly changed-brand staging generation; promotion remains separate",
        "argv": [
            PYTHON_BIN,
            "-m",
            ORCHESTRATOR_MODULE,
            "--brand-source",
            "general-density",
            "--bundle-kind",
            "general",
            "--dry-run",
            "--analysis-variant",
            variant,
            "--brand-keys-file",
            str(brand_keys_output),
            "--work-dir",
            str(work_dir_root / f"{week}_{variant}"),
        ],
    }


def _plan_payload(manifest: WeeklyChangedBrands, brand_keys_output: Path, work_dir_root: Path) -> PlanPayload:
    brand_keys = [brand.brand_key for brand in manifest.brands]
    return {
        "plan_version": PLAN_VERSION,
        "week": manifest.week,
        "brand_count": len(brand_keys),
        "brand_keys": brand_keys,
        "brand_key_source": str(brand_keys_output),
        "promotion_invoked": False,
        "commands": [_command(variant, brand_keys_output, work_dir_root, manifest.week) for variant in VARIANTS],
    }


def build_weekly_plan(
    changed_brands: Path,
    output: Path,
    brand_keys_output: Path,
    work_dir_root: Path | None = None,
) -> PlanPayload:
    """Create deterministic Agent2 orchestrator candidate arguments for changed brands."""
    manifest = _parse_manifest(changed_brands)
    plan = _plan_payload(
        manifest,
        brand_keys_output,
        work_dir_root or Path("outputs/phase_zeta_agent2_regen_orchestrator/weekly_changed"),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    brand_keys_output.parent.mkdir(parents=True, exist_ok=True)
    brand_keys_output.write_text(
        json.dumps(plan["brand_keys"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return plan


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan Agent2 weekly changed-brand regeneration candidates without promotion."
    )
    parser.add_argument("--changed-brands", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--brand-keys-output", type=Path, required=True)
    parser.add_argument(
        "--work-dir-root",
        type=Path,
        default=Path("outputs/phase_zeta_agent2_regen_orchestrator/weekly_changed"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        plan = build_weekly_plan(
            args.changed_brands,
            args.output,
            args.brand_keys_output,
            args.work_dir_root,
        )
    except PlanningInputError as exc:
        print(f"agent2 weekly planning failed closed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"week": plan["week"], "brand_count": plan["brand_count"], "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
