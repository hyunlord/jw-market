"""Checkpoint state for idempotent runs and partial-run dependency checks."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE_ENV = "JW_PIPELINE_STATE_FILE"
DEFAULT_STATE_FILE = "~/.jw_pipeline/state.json"


def default_state_path() -> Path:
    return Path(os.environ.get(STATE_FILE_ENV) or DEFAULT_STATE_FILE).expanduser()


@dataclass
class StageRecord:
    status: str  # completed | failed
    epoch: str
    finished_at: str
    forced: bool = False

    def to_json(self) -> dict:
        return {"status": self.status, "epoch": self.epoch, "finished_at": self.finished_at, "forced": self.forced}

    @classmethod
    def from_json(cls, raw: dict) -> "StageRecord":
        return cls(
            status=str(raw.get("status", "")),
            epoch=str(raw.get("epoch", "")),
            finished_at=str(raw.get("finished_at", "")),
            forced=bool(raw.get("forced", False)),
        )


class StateStore:
    """One JSON file: per-stage completion records keyed by stage name."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stages: dict[str, StageRecord] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
            for key, value in (raw.get("stages") or {}).items():
                self._stages[key] = StageRecord.from_json(value)

    def record(self, stage: str, *, status: str, epoch: str, forced: bool = False) -> None:
        self._stages[stage] = StageRecord(
            status=status,
            epoch=epoch,
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            forced=forced,
        )
        self._save()

    def get(self, stage: str) -> StageRecord | None:
        return self._stages.get(stage)

    def completed_at_epoch(self, stage: str, epoch: str) -> bool:
        record = self._stages.get(stage)
        return record is not None and record.status == "completed" and record.epoch == epoch

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"stages": {key: record.to_json() for key, record in self._stages.items()}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
