"""Runtime configuration and successful-anchor lookup for full source scans."""
from __future__ import annotations

import json
import os
from pathlib import Path

from pipeline.scripts.ingest_hook.source_inventory import (
    ScanSnapshot,
    SourceInventoryError,
    SourceScanPolicy,
    read_scan_snapshot,
)

ENV_SOURCE_SCAN_POLICIES = "INGEST_SOURCE_SCAN_POLICIES_JSON"


def load_scan_policy(category: str, *, required: bool) -> SourceScanPolicy | None:
    raw = os.environ.get(ENV_SOURCE_SCAN_POLICIES, "").strip()
    if not raw:
        if required:
            raise SourceInventoryError(f"{ENV_SOURCE_SCAN_POLICIES} is required")
        return None
    try:
        payload = json.loads(raw)
        item = payload.get(category)
    except (AttributeError, json.JSONDecodeError) as exc:
        raise SourceInventoryError(f"invalid {ENV_SOURCE_SCAN_POLICIES}: {exc}") from exc
    if item is None:
        if required:
            raise SourceInventoryError(f"source scan policy is absent for {category}")
        return None
    if not isinstance(item, dict):
        raise SourceInventoryError(f"source scan policy must be an object for {category}")
    root_value = item.get("root")
    period_unit = item.get("period_unit")
    excluded = item.get("excluded_relative_roots", [])
    if not isinstance(root_value, str) or not Path(root_value).is_absolute():
        raise SourceInventoryError(f"source scan root must be absolute for {category}")
    if period_unit not in {"month", "quarter"}:
        raise SourceInventoryError(f"source scan period_unit is invalid for {category}")
    if not isinstance(excluded, list) or any(not isinstance(value, str) for value in excluded):
        raise SourceInventoryError(f"source scan exclusions are invalid for {category}")
    return SourceScanPolicy(
        category=category,
        root=Path(root_value).resolve(),
        period_unit=period_unit,
        excluded_relative_roots=tuple(excluded),
        rebuild_periods={"ubist": 61, "iqvia_nsa": 24}.get(category),
    )


def latest_successful_snapshot(output_root: Path, category: str) -> ScanSnapshot | None:
    category_root = output_root / category
    if not category_root.is_dir():
        return None
    snapshots = tuple(read_scan_snapshot(path) for path in category_root.glob("*/*/*.json"))
    if not snapshots:
        return None
    return max(snapshots, key=lambda snapshot: (snapshot.observed_at, snapshot.run_id))
