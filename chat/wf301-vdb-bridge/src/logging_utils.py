"""Logging helpers with minimal secret redaction."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


logger = logging.getLogger("wf301-vdb-bridge")
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":%(message)s}',
)

_REDACT_KEYS = {"password", "pw", "token", "secret", "authorization", "key"}


def safe_log(event: str, **fields: Any) -> None:
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if any(part in key.lower() for part in _REDACT_KEYS):
            clean[key] = "***"
        else:
            clean[key] = value
    logger.info(json.dumps({"event": event, **clean}, ensure_ascii=False))
