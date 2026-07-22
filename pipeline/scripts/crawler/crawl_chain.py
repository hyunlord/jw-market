#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.scripts.crawler.crawl_temporal_contract import (
    StageGate,
    StageGateError,
    read_stage_gate,
)


class Stage(StrEnum):
    TIER1_COLLECT = "tier1_collect"
    TIER1_CLASSIFY = "tier1_classify_incremental"
    TIER2_COLLECT = "tier2_collect_exact"
    TIER2_CLASSIFY = "tier2_classify_v2_and_refresh"


STAGES: Final[tuple[Stage, ...]] = tuple(Stage)
RUN_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class Receipt:
    run_id: str
    stage: str
    attempt: int
    status: str
    started_at: str
    finished_at: str
    command_revision: str
    input_sha256: str
    output_sha256: str
    exit_code: int
    error_code: str
    failures: int = 0
    events_raw_gap: int = 0
    pending_gap: int = 0


@dataclass(frozen=True, slots=True)
class ChainConfig:
    run_id: str
    state_root: Path
    stage_script: Path
    resume: bool
    from_stage: Stage
    command_revision: str


class ChainError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _marker(event: str, **fields: str | int) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True, separators=(",", ":")), flush=True)


def _atomic_json(path: Path, receipt: Receipt) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(receipt), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_receipt(path: Path) -> Receipt | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return Receipt(**data)


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _timeout_for(stage: Stage) -> int:
    names = {
        Stage.TIER1_COLLECT: ("CRAWL_CHAIN_TIMEOUT_TIER1_COLLECT", 10_800),
        Stage.TIER1_CLASSIFY: ("CRAWL_CHAIN_TIMEOUT_TIER1_CLASSIFY", 900),
        Stage.TIER2_COLLECT: ("CRAWL_CHAIN_TIMEOUT_TIER2_COLLECT", 28_800),
        Stage.TIER2_CLASSIFY: ("CRAWL_CHAIN_TIMEOUT_TIER2_CLASSIFY", 1_800),
    }
    env_name, default = names[stage]
    return int(os.environ.get(env_name, str(default)))


def _receipt_path(run_root: Path, stage: Stage) -> Path:
    return run_root / "receipts" / f"{stage.value}.json"


def _output_path(run_root: Path, stage: Stage) -> Path:
    return run_root / "outputs" / stage.value


def _attempt_output_path(run_root: Path, stage: Stage, attempt: int) -> Path:
    return run_root / "attempts" / stage.value / f"attempt-{attempt}"


def _verify_complete(
    config: ChainConfig,
    run_root: Path,
    stage: Stage,
    expected_input_sha256: str,
) -> Receipt:
    receipt = _read_receipt(_receipt_path(run_root, stage))
    if receipt is None or receipt.status != "complete":
        raise ChainError(f"missing complete receipt for prior stage: {stage.value}")
    if receipt.command_revision != config.command_revision:
        raise ChainError(f"receipt command revision mismatch for stage: {stage.value}")
    if receipt.input_sha256 != expected_input_sha256:
        raise ChainError(f"receipt input hash mismatch for stage: {stage.value}")
    actual = _tree_sha256(_output_path(run_root, stage))
    if actual != receipt.output_sha256:
        raise ChainError(f"receipt output hash mismatch for stage: {stage.value}")
    return receipt


def _attempt_for(path: Path) -> int:
    receipt = _read_receipt(path)
    return 1 if receipt is None else receipt.attempt + 1


def _run_stage(
    config: ChainConfig,
    run_root: Path,
    stage: Stage,
    input_sha256: str,
) -> int:
    receipt_path = _receipt_path(run_root, stage)
    output_path = _output_path(run_root, stage)
    attempt = _attempt_for(receipt_path)
    attempt_output_path = _attempt_output_path(run_root, stage, attempt)
    attempt_output_path.mkdir(parents=True, exist_ok=False)
    started_at = _utc_now()
    _marker("CHAIN_STAGE_START", run_id=config.run_id, stage=stage.value, attempt=attempt)
    env = os.environ.copy()
    env.update(
        {
            "CHAIN_RUN_ID": config.run_id,
            "CHAIN_RUN_ROOT": str(run_root),
            "CHAIN_STAGE": stage.value,
            "CHAIN_STAGE_OUTPUT_DIR": str(attempt_output_path),
        }
    )
    gate: StageGate | None = None
    try:
        completed = subprocess.run(
            [str(config.stage_script), stage.value],
            check=False,
            env=env,
            timeout=_timeout_for(stage),
        )
        exit_code = completed.returncode
        error_code = "" if exit_code == 0 else "stage_nonzero_exit"
        if exit_code == 0:
            try:
                gate = read_stage_gate(
                    attempt_output_path / "stage_gate.json",
                    expected_stage=stage.value,
                )
            except StageGateError as exc:
                gate = exc.gate
                exit_code = 78
                error_code = exc.error_code
    except subprocess.TimeoutExpired:
        exit_code = 124
        error_code = "stage_timeout"
    finished_at = _utc_now()
    status = "complete" if exit_code == 0 else "failed"
    output_sha256 = ""
    if exit_code == 0:
        if output_path.exists():
            raise ChainError(f"final output already exists for incomplete stage: {stage.value}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        attempt_output_path.replace(output_path)
        output_sha256 = _tree_sha256(output_path)
    receipt = Receipt(
        run_id=config.run_id,
        stage=stage.value,
        attempt=attempt,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        command_revision=config.command_revision,
        input_sha256=input_sha256,
        output_sha256=output_sha256,
        exit_code=exit_code,
        error_code=error_code,
        failures=gate.failures if gate is not None else 0,
        events_raw_gap=gate.events_raw_gap if gate is not None else 0,
        pending_gap=gate.pending_gap if gate is not None else 0,
    )
    _atomic_json(receipt_path, receipt)
    event = "CHAIN_STAGE_COMPLETE" if exit_code == 0 else "CHAIN_STAGE_FAILED"
    _marker(event, run_id=config.run_id, stage=stage.value, attempt=attempt, exit_code=exit_code)
    return exit_code


def run_stage(config: ChainConfig, stage: Stage) -> int:
    """Run one stage only after verifying every durable predecessor."""

    run_root = config.state_root / "runs" / config.run_id
    run_root.mkdir(parents=True, exist_ok=True)
    input_sha256 = "root"
    stage_index = STAGES.index(stage)
    for prior in STAGES[:stage_index]:
        input_sha256 = _verify_complete(
            config,
            run_root,
            prior,
            input_sha256,
        ).output_sha256
    receipt = _read_receipt(_receipt_path(run_root, stage))
    if receipt is not None and receipt.status == "complete":
        _verify_complete(config, run_root, stage, input_sha256)
        _marker("CHAIN_STAGE_SKIPPED_COMPLETE", run_id=config.run_id, stage=stage.value)
        return 0
    return _run_stage(config, run_root, stage, input_sha256)


def run(config: ChainConfig) -> int:
    run_root = config.state_root / "runs" / config.run_id
    run_root.mkdir(parents=True, exist_ok=True)
    start_index = STAGES.index(config.from_stage)
    receipts_dir = run_root / "receipts"
    if receipts_dir.exists() and any(receipts_dir.iterdir()) and not config.resume:
        raise ChainError("existing receipts require --resume")
    input_sha256 = "root"
    for prior in STAGES[:start_index]:
        input_sha256 = _verify_complete(
            config,
            run_root,
            prior,
            input_sha256,
        ).output_sha256
    for stage in STAGES[start_index:]:
        receipt = _read_receipt(_receipt_path(run_root, stage))
        if receipt is not None and receipt.status == "complete":
            input_sha256 = _verify_complete(
                config,
                run_root,
                stage,
                input_sha256,
            ).output_sha256
            _marker("CHAIN_STAGE_SKIPPED_COMPLETE", run_id=config.run_id, stage=stage.value)
            continue
        exit_code = _run_stage(config, run_root, stage, input_sha256)
        if exit_code != 0:
            return exit_code
        input_sha256 = _verify_complete(
            config,
            run_root,
            stage,
            input_sha256,
        ).output_sha256
    _marker("CHAIN_RUN_COMPLETE", run_id=config.run_id, stages=len(STAGES))
    return 0


def report_status(state_root: Path, run_id: str) -> int:
    run_root = state_root / "runs" / run_id
    receipts = {
        stage: _read_receipt(_receipt_path(run_root, stage))
        for stage in STAGES
    }
    failed_stage = next(
        (stage.value for stage, receipt in receipts.items() if receipt and receipt.status == "failed"),
        "",
    )
    completed = [
        stage.value
        for stage, receipt in receipts.items()
        if receipt and receipt.status == "complete"
    ]
    if failed_stage:
        run_status = "failed"
        exit_code = 1
    elif len(completed) == len(STAGES):
        run_status = "complete"
        exit_code = 0
    else:
        run_status = "incomplete"
        exit_code = 3
    print(
        json.dumps(
            {
                "run_id": run_id,
                "run_status": run_status,
                "completed_stages": completed,
                "failed_stage": failed_stage,
            },
            sort_keys=True,
        )
    )
    return exit_code


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the durable four-stage crawl chain")
    subparsers = parser.add_subparsers(dest="action", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--run-id", required=True)
    run_parser.add_argument("--state-root", type=Path, default=Path("/var/lib/jw-crawl-chain"))
    run_parser.add_argument("--stage-script", type=Path, required=True)
    run_parser.add_argument("--resume", action="store_true")
    run_parser.add_argument("--from-stage", type=Stage, choices=STAGES, default=STAGES[0])
    stage_parser = subparsers.add_parser("run-stage")
    stage_parser.add_argument("--run-id", required=True)
    stage_parser.add_argument("--state-root", type=Path, default=Path("/var/lib/jw-crawl-chain"))
    stage_parser.add_argument("--stage-script", type=Path, required=True)
    stage_parser.add_argument("--stage", type=Stage, choices=STAGES, required=True)
    stage_parser.add_argument("--command-revision", required=True)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--run-id", required=True)
    status_parser.add_argument("--state-root", type=Path, default=Path("/var/lib/jw-crawl-chain"))
    return parser.parse_args()


def _execute(args: argparse.Namespace) -> int:
    if args.action == "status":
        return report_status(args.state_root, args.run_id)
    revision = getattr(args, "command_revision", "") or os.environ.get(
        "CRAWL_CHAIN_COMMAND_REVISION"
    ) or hashlib.sha256(args.stage_script.read_bytes() + Path(__file__).read_bytes()).hexdigest()
    config = ChainConfig(
        run_id=args.run_id,
        state_root=args.state_root,
        stage_script=args.stage_script,
        resume=getattr(args, "resume", True),
        from_stage=getattr(args, "from_stage", getattr(args, "stage", STAGES[0])),
        command_revision=revision,
    )
    config.state_root.mkdir(parents=True, exist_ok=True)
    lock_path = config.state_root / ".chain.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _marker("CHAIN_SCHEDULE_SKIPPED_ACTIVE", run_id=config.run_id)
            return 75
        if args.action == "run-stage":
            return run_stage(config, args.stage)
        return run(config)


def main() -> int:
    args = _parse_args()
    if not RUN_ID_PATTERN.fullmatch(args.run_id):
        print("invalid run id", file=sys.stderr)
        return 2
    try:
        return _execute(args)
    except (ChainError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
