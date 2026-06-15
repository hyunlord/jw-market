#!/usr/bin/env python3
"""Build and load source-aware JSON Layer 3 general-view marts."""

from __future__ import annotations

from .general_compute import compute_general, main
from .general_config import load_env

__all__ = ["compute_general", "load_env", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
