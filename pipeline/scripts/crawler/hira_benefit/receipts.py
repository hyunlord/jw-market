from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .contract import GateResult, HiraRunMetrics


def run_dir(state_root: str, run_id: str) -> Path:
    path = Path(state_root) / "hira-benefit" / "runs" / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: object) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_stage_receipt(
    path: Path,
    *,
    stage: str,
    metrics: HiraRunMetrics,
    gate: GateResult,
    detail: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "stage": stage,
        "status": "complete" if gate.passed else "failed",
        **asdict(metrics),
        "gate_failures": list(gate.failures),
        "alerts": list(gate.alerts),
    }
    if detail:
        payload.update(detail)
    write_json(path, payload)
    return payload
