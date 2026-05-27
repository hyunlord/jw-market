from __future__ import annotations

import hashlib
import json
from typing import Any


def deterministic_json_dumps(obj: Any) -> str:
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def compute_bundle_hash(bundle: dict) -> str:
    payload = {k: v for k, v in bundle.items() if k != "bundle_meta"}
    meta = {k: v for k, v in bundle.get("bundle_meta", {}).items() if k != "bundle_hash"}
    full = {"bundle_meta_excluding_hash": meta, "payload": payload}
    serialized = deterministic_json_dumps(full)
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()
