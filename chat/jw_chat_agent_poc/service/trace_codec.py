"""Store a turn's trace so that a large one still fits inside the write budget.

A turn's trace grew from tens of kilobytes to megabytes: ``claim_ir_shadow``
alone measured 12.1 MB of a 13.6 MB trace, and the write that carries it has to
be certified by every node of a three-node Galera cluster before the client is
answered. Measured server-side, those inserts took 11.5 to 50.9 seconds against
a five second client budget, so the row was thrown away after the server had
already paid for it.

Compression is applied here rather than at the call site because the column it
lands in is ``longtext … CHECK (json_valid(trace_json))``. Compressed bytes are
not JSON, so they are base64-encoded and stored as a *JSON string scalar*:
``json_valid('"jwtz1:H4sI…"')`` is 1, which keeps the constraint satisfied and
needs no schema change. The magic prefix is what tells a compressed row from a
plain one, so rows written before this module -- and rows small enough that it
declines to compress -- are read back by exactly the same call.

Nothing is dropped. The trace that goes in is the trace that comes out; only
its representation on disk changes.
"""
from __future__ import annotations

import base64
from collections.abc import Mapping
import json
import os
from typing import Any, Final
import zlib

# "jw trace, zlib, version 1". Versioned so a later codec can be told apart
# rather than guessed at: a reader that meets an unknown version must fail
# loudly instead of returning half a trace.
MAGIC: Final = "jwtz1:"

COMPRESS_THRESHOLD_ENV: Final = "CHAT_TRACE_COMPRESS_THRESHOLD_BYTES"
COMPRESS_ENABLED_ENV: Final = "CHAT_TRACE_COMPRESS_ENABLED"

# Below this the row stays plain text. Two reasons, neither of them speed: a
# small trace is not what breaks the write budget, and a plain row can still be
# read by JSON_EXTRACT in a SQL session, which is how this table has always been
# inspected. Above it the write budget is the thing that matters and the
# inspection has to move to the decoder.
DEFAULT_COMPRESS_THRESHOLD_BYTES: Final = 262_144

_TRUE_VALUES: Final = frozenset({"1", "true", "on", "enabled", "yes"})


class TraceDecodeError(ValueError):
    """A stored trace announced an encoding this reader cannot honour."""


def compression_enabled() -> bool:
    raw = os.getenv(COMPRESS_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().casefold() in _TRUE_VALUES


def compress_threshold_bytes() -> int:
    raw = os.getenv(COMPRESS_THRESHOLD_ENV, "").strip()
    if not raw:
        return DEFAULT_COMPRESS_THRESHOLD_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_COMPRESS_THRESHOLD_BYTES
    return value if value >= 0 else DEFAULT_COMPRESS_THRESHOLD_BYTES


def encode_trace(serialized_json: str) -> str:
    """Return what belongs in ``trace_json`` for an already-serialized trace.

    Takes the serialized form rather than the mapping so that the caller keeps
    ownership of how a trace is turned into JSON -- the ``default=`` fallback
    this table has always had stays in one place.
    """
    if not compression_enabled():
        return serialized_json
    raw = serialized_json.encode("utf-8")
    if len(raw) < compress_threshold_bytes():
        return serialized_json
    packed = base64.b64encode(zlib.compress(raw)).decode("ascii")
    # json.dumps of a str yields a quoted JSON scalar, which is what keeps the
    # CHECK (json_valid(...)) constraint satisfied.
    return json.dumps(MAGIC + packed)


def decode_trace(stored: object) -> Any:
    """Return the trace a stored ``trace_json`` value stands for.

    Handles all three shapes this column holds: rows written before this module
    (a JSON object), rows this module declined to compress (also a JSON object),
    and rows it compressed (a JSON string carrying the magic prefix).
    """
    if isinstance(stored, Mapping):
        return dict(stored)
    if isinstance(stored, bytes | bytearray):
        stored = bytes(stored).decode("utf-8", errors="replace")
    if not isinstance(stored, str):
        return None
    try:
        parsed = json.loads(stored)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, str) or not parsed.startswith(MAGIC):
        return parsed
    payload = parsed[len(MAGIC) :]
    try:
        raw = zlib.decompress(base64.b64decode(payload, validate=True))
    except Exception as exc:  # noqa: BLE001 - re-raised as a typed failure below
        # Not swallowed: a trace that announced itself compressed and then would
        # not decompress is a lost trace, and reporting it as "no trace" would
        # hide that.
        raise TraceDecodeError(f"compressed trace could not be decoded: {type(exc).__name__}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise TraceDecodeError("decompressed trace was not valid JSON") from exc


__all__ = [
    "COMPRESS_ENABLED_ENV",
    "COMPRESS_THRESHOLD_ENV",
    "DEFAULT_COMPRESS_THRESHOLD_BYTES",
    "MAGIC",
    "TraceDecodeError",
    "compress_threshold_bytes",
    "compression_enabled",
    "decode_trace",
    "encode_trace",
]
