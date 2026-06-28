#!/usr/bin/env python3
"""Load corpus matches and print the read-only analysis baseline."""

from __future__ import annotations

from common import load_matches, processed_files


def main() -> int:
    df = load_matches()
    print(f"Loaded {len(processed_files()):,} processed JSON, {len(df):,} matches")
    print(df["brand"].nunique(), "unique matched brands")
    print(df["brand_group"].value_counts().to_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
