"""Active market-membership selection policy for the strategic catalog.

`brand + view_type -> market` is not a functional dependency. Recomputed from the
MI Master canon (parquet/strategic_brand, 3,874 rows / 3,403 names):

    market_landscape, all rows    3,403 names, 471 in two ml_id
    market_landscape, active only 3,338 names, 264 in two ml_id
    competitive_dynamics, active  1,307 names,   0 in two cd_id

The CD figure being 0 is an observation about today's catalog, not a rule, so
selection must be defined for the multi-membership case in both views.

Two separate defects motivated this module, and they need different answers:

1. A market split leaves the superseded rows in the catalog with
   ``is_excluded=1``. 26 brand names (리바로페노 and 25 siblings) were moved from
   ml_006 to ml_007 that way. An unfiltered ``LIMIT 1`` kept returning the
   superseded ml_006 row. That is a *wrong* answer and is fixed by filtering.

2. 264 brand names hold two genuinely active memberships, every one of them the
   pair (ml_005, ml_008) — two overlapping cardiovascular market definitions that
   share ATC C7A/C8A. Neither membership is wrong. Collapsing them to one is a
   *lossy* answer, so callers get the whole ordered set and pick by the market
   they are actually asking about.

The single-market accessors that remain take ``primary_membership`` purely for
backward compatibility, and its order is total (see ``membership_order_key``) so
the answer never depends on storage order.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

#: A row is usable only when MI Master has not withdrawn it from the market.
#: ``COALESCE`` because the catalog carries NULL for never-set flags.
ACTIVE_PREDICATE_SQL = (
    "COALESCE(is_excluded, 0) = 0 AND COALESCE(is_class_excluded, 0) = 0"
)

#: Total order. Every term is justified, and the last one guarantees no ties:
#:
#:   1. canonical rows first — ``sb_canonical_*`` ids are the identity MI Master
#:      mints when it splits a market, so they are the post-split answer by
#:      construction. Verified against the canon: 25 canonical rows exist, and
#:      none of them belong to a brand with two active memberships, so this term
#:      only ever resolves supersession and never arbitrates a genuine pair.
#:   2. ml_id ASC — zero-padded ``ml_NNN``, so lexicographic equals numeric
#:      equals MI Master registration order.
#:   3. brand_id ASC — verified unique across all 3,874 catalog rows, which makes
#:      the order total. Without it a tie would fall back to storage order, which
#:      is exactly the non-determinism being removed.
MEMBERSHIP_ORDER_SQL = (
    "CASE WHEN brand_id LIKE 'sb\\_canonical\\_%' THEN 0 ELSE 1 END ASC, "
    "ml_id ASC, "
    "brand_id ASC"
)

CANONICAL_BRAND_ID_PREFIX = "sb_canonical_"


class NoActiveMarketMembership(LookupError):
    """Every catalog row for this brand is excluded.

    Raised instead of falling back to an excluded row. An excluded row means MI
    Master deliberately withdrew the brand from that market, so analysing it
    against that market is the defect, not the recovery. 65 brand names are in
    this state; none of them is an ``is_jw`` (25) or ``is_target`` (16) brand, so
    no Agent2 subject is affected today.
    """

    def __init__(self, brand_name: str, excluded_rows: int) -> None:
        super().__init__(
            f"no active market membership for brand={brand_name!r}: "
            f"{excluded_rows} catalog row(s), all excluded"
        )
        self.brand_name = brand_name
        self.excluded_rows = excluded_rows


def _flag(row: Mapping, key: str) -> bool:
    value = row.get(key)
    if value is None:
        return False
    return bool(value)


def is_active_row(row: Mapping) -> bool:
    """Mirror of ``ACTIVE_PREDICATE_SQL`` for rows already in memory."""

    return not _flag(row, "is_excluded") and not _flag(row, "is_class_excluded")


def is_canonical_row(row: Mapping) -> bool:
    return str(row.get("brand_id") or "").startswith(CANONICAL_BRAND_ID_PREFIX)


def membership_order_key(row: Mapping) -> tuple[int, str, str]:
    """Mirror of ``MEMBERSHIP_ORDER_SQL``; keep the two in step."""

    return (
        0 if is_canonical_row(row) else 1,
        str(row.get("ml_id") or ""),
        str(row.get("brand_id") or ""),
    )


def active_memberships(rows: Iterable[Mapping]) -> tuple[Mapping, ...]:
    """Every active membership for one brand, in the total order.

    Applying the filter and the order here as well as in SQL is deliberate: the
    mart fallback paths cannot express the catalog's exclusion flags, and a
    caller that hands us pre-fetched rows still gets the same contract.
    """

    return tuple(sorted((row for row in rows if is_active_row(row)), key=membership_order_key))


def primary_membership(rows: Sequence[Mapping], brand_name: str) -> Mapping:
    """The one membership a single-market caller gets. Fails closed when none.

    Callers that can handle several markets should use ``active_memberships``
    and pass the market they are actually asking about; this collapses 264
    brands' second membership and is only correct for a caller that has no
    market in hand.
    """

    active = active_memberships(rows)
    if not active:
        raise NoActiveMarketMembership(brand_name, excluded_rows=len(tuple(rows)))
    return active[0]


def membership_market_ids(rows: Iterable[Mapping], key: str = "ml_id") -> tuple[str, ...]:
    """Distinct market ids of the active memberships, in the total order."""

    seen: list[str] = []
    for row in active_memberships(rows):
        value = row.get(key)
        if value is None:
            continue
        text = str(value)
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)
