from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, Protocol


GENERAL_MEMBERSHIP_PAGE_QUERY: Final = """
    SELECT general.id AS membership_id,
           general.brand_name AS brand,
           NULLIF(general.brand_key, '') AS brand_alias,
           NULL AS market_id,
           NULL AS market_name,
           'general_mart' AS support_source
    FROM mart_general_brand_metric AS general FORCE INDEX (PRIMARY)
    WHERE general.id > %s
      AND general.brand_name IS NOT NULL
      AND general.brand_name <> ''
    ORDER BY general.id
    LIMIT %s
"""


class CatalogMembershipCursor(Protocol):
    def execute(self, query: str, args: tuple[int, int]) -> int | None: ...

    def fetchall(self) -> Sequence[Mapping[str, object]]: ...


def load_general_membership_rows(
    cursor: CatalogMembershipCursor,
    *,
    page_size: int,
    max_rows: int,
) -> tuple[dict[str, object], ...]:
    if page_size < 1 or max_rows < 1:
        raise ValueError("catalog membership page limits must be positive")

    rows: list[dict[str, object]] = []
    last_id = 0
    while True:
        remaining = max_rows - len(rows)
        page_limit = min(page_size, max(remaining, 1))
        cursor.execute(GENERAL_MEMBERSHIP_PAGE_QUERY, (last_id, page_limit))
        page = tuple(dict(row) for row in cursor.fetchall())
        if not page:
            break
        if remaining < 1:
            raise RuntimeError(f"general brand membership exceeds configured row limit {max_rows}")

        next_id = max(int(row["membership_id"]) for row in page)
        if next_id <= last_id:
            raise RuntimeError("general brand membership page did not advance")
        rows.extend(page)
        last_id = next_id
        if len(page) < page_limit:
            break
    return tuple(rows)
