from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Final, Sequence


EXPECTED_STAGE_ORDER: Final[tuple[str, ...]] = (
    "mart",
    "cache",
    "forecast",
    "strength",
    "short_long",
    "events",
    "elements",
)
_MODES: Final[tuple[str, ...]] = ("full", "incremental")


class PipelineError(RuntimeError):
    """Raised when the monthly pipeline cannot proceed safely."""


@dataclass(frozen=True)
class StageSpec:
    name: str
    dry_command: tuple[str, ...]
    full_command: tuple[str, ...]
    incremental_command: tuple[str, ...]

    def command_for(self, *, mode: str, dry_run: bool) -> tuple[str, ...]:
        if dry_run:
            return self.dry_command
        if mode == "full":
            return self.full_command
        return self.incremental_command


def _command(value: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(part, str) and part for part in value):
        raise PipelineError(f"{field} must be a non-empty string array")
    return tuple(value)


def load_spec(path: Path) -> tuple[StageSpec, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot load pipeline spec {path}: {exc}") from exc

    rows = payload.get("stages") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise PipelineError("pipeline spec stages must be an array")

    names = [row.get("name") for row in rows if isinstance(row, dict)]
    if names != list(EXPECTED_STAGE_ORDER):
        raise PipelineError(
            f"stage order must be {list(EXPECTED_STAGE_ORDER)}, got {names}"
        )

    stages: list[StageSpec] = []
    for row in rows:
        assert isinstance(row, dict)
        name = str(row["name"])
        stages.append(
            StageSpec(
                name=name,
                dry_command=_command(row.get("dry_command"), field=f"{name}.dry_command"),
                full_command=_command(row.get("full_command"), field=f"{name}.full_command"),
                incremental_command=_command(
                    row.get("incremental_command"),
                    field=f"{name}.incremental_command",
                ),
            )
        )
    return tuple(stages)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read state {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"state {path} must contain a JSON object")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _last_json_object(stdout: str, *, stage: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise PipelineError(f"{stage} failed: child emitted no JSON result")


def _validate_child_result(stage: str, result: dict[str, Any]) -> None:
    if result.get("stage") != stage:
        raise PipelineError(f"{stage} failed: child reported stage={result.get('stage')!r}")
    if result.get("status") not in {"complete", "noop"}:
        raise PipelineError(f"{stage} failed: child status={result.get('status')!r}")
    counts = tuple(result.get(key) for key in ("requested", "generated", "validated"))
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in counts):
        raise PipelineError(f"{stage} completion gate requires non-negative integer counts")
    if len(set(counts)) != 1:
        raise PipelineError(
            f"{stage} completion gate failed: requested={counts[0]} "
            f"generated={counts[1]} validated={counts[2]}"
        )


def _validate_changed_brands(path: Path | None) -> Path:
    if path is None or not path.is_file():
        raise PipelineError("incremental mode requires a non-empty --changed-brands file")
    if not any(line.strip() for line in path.read_text(encoding="utf-8").splitlines()):
        raise PipelineError("incremental mode requires a non-empty --changed-brands file")
    return path


def execute_pipeline(
    stages: Sequence[StageSpec],
    *,
    mode: str,
    source_epoch: str,
    dry_run: bool,
    state_path: Path,
    checkpoint_path: Path,
    changed_brands_path: Path | None,
) -> dict[str, Any]:
    if mode not in _MODES:
        raise PipelineError(f"unsupported mode: {mode}")
    if not source_epoch.strip():
        raise PipelineError("source epoch is required")
    if tuple(stage.name for stage in stages) != EXPECTED_STAGE_ORDER:
        raise PipelineError("stage order does not match the canonical dependency order")

    changed_brands = None
    if mode == "incremental":
        changed_brands = _validate_changed_brands(changed_brands_path)

    if not dry_run:
        state = _read_json(state_path)
        if state and all(
            (
                state.get("source_epoch") == source_epoch,
                state.get("mode") == mode,
                state.get("status") == "complete",
            )
        ):
            return {"status": "noop", "source_epoch": source_epoch, "mode": mode, "stages": []}

    completed: list[str] = []
    if not dry_run:
        checkpoint = _read_json(checkpoint_path)
        if checkpoint and (
            checkpoint.get("source_epoch") == source_epoch and checkpoint.get("mode") == mode
        ):
            candidate = checkpoint.get("completed_stages", [])
            if not isinstance(candidate, list) or candidate != list(EXPECTED_STAGE_ORDER[: len(candidate)]):
                raise PipelineError("checkpoint contains a non-prefix completed stage list")
            completed = list(candidate)

    results: list[dict[str, Any]] = []
    environment = os.environ.copy()
    environment.update(
        {
            "PIPELINE_SOURCE_EPOCH": source_epoch,
            "PIPELINE_MODE": mode,
            "PIPELINE_DRY_RUN": "1" if dry_run else "0",
        }
    )
    if changed_brands is not None:
        environment["PIPELINE_CHANGED_BRANDS_FILE"] = str(changed_brands)

    for stage in stages[len(completed) :]:
        command = stage.command_for(mode=mode, dry_run=dry_run)
        process = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip() or "no child output"
            raise PipelineError(f"{stage.name} failed with rc={process.returncode}: {detail}")
        result = _last_json_object(process.stdout, stage=stage.name)
        _validate_child_result(stage.name, result)
        results.append(result)
        completed.append(stage.name)
        if not dry_run:
            _write_json_atomic(
                checkpoint_path,
                {
                    "source_epoch": source_epoch,
                    "mode": mode,
                    "completed_stages": completed,
                },
            )

    final = {
        "status": "complete",
        "source_epoch": source_epoch,
        "mode": mode,
        "stages": results,
    }
    if not dry_run:
        _write_json_atomic(
            state_path,
            {"source_epoch": source_epoch, "mode": mode, "status": "complete"},
        )
        checkpoint_path.unlink(missing_ok=True)
    return final


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the source-epoch monthly pipeline")
    parser.add_argument("--spec", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--full", action="store_true")
    mode.add_argument("--incremental", action="store_true")
    parser.add_argument("--source-epoch", default=os.getenv("MART_SOURCE_EPOCH"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--checkpoint-file", type=Path, required=True)
    parser.add_argument("--changed-brands", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    mode = "full" if args.full else "incremental"
    try:
        result = execute_pipeline(
            load_spec(args.spec),
            mode=mode,
            source_epoch=args.source_epoch or "",
            dry_run=args.dry_run,
            state_path=args.state_file,
            checkpoint_path=args.checkpoint_file,
            changed_brands_path=args.changed_brands,
        )
    except PipelineError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
