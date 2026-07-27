"""Single definition of the ``cache_cause`` row identity.

The producer (``pipeline/scripts/etl/build_cache_cause.py``) and the Agent2
reader (``bundle_builder``) previously agreed on this key only by coincidence:
the producer wrote ``market_id`` and the reader did not read it. Once the reader
started matching on ``market_id`` the two had to agree exactly, so the mapping
lives here and both import it.

Two distinct identifiers are in play and conflating them is the ③ defect:

``market_id``       what the PK column holds today. Derived from the *parent ML*
                    for both views, so two sibling CD markets under one ML
                    produce the same value. ``REPLACE INTO`` then silently
                    overwrites one with the other.
``view_source_id``  the market the row is actually about — ``ml_id`` for
                    market_landscape, ``cd_id`` for competitive_dynamics. It is
                    already carried inside ``response_json`` but is not
                    queryable, so it cannot disambiguate the key.

Known sibling CD splits: ml_008 -> cd_008/cd_009, ml_009 -> cd_010/cd_011,
ml_010 -> cd_012/cd_013.
"""

from __future__ import annotations

import re

VIEW_MARKET_LANDSCAPE = "market_landscape"
VIEW_COMPETITIVE_DYNAMICS = "competitive_dynamics"

#: Today's primary key.
CACHE_CAUSE_KEY_COLUMNS: tuple[str, ...] = (
    "brand",
    "view_type",
    "source",
    "measure",
    "market_id",
)

#: The key once ``view_source_id`` is added (designed, not applied — see
#: pipeline/scripts/deploy/sql/cache_cause_market_identity.sql).
CACHE_CAUSE_TARGET_KEY_COLUMNS: tuple[str, ...] = (
    *CACHE_CAUSE_KEY_COLUMNS,
    "view_source_id",
)


def strategy_id_for(ml_id: str | None) -> str | None:
    """``ml_007`` -> ``strategy_007``. Mirrors ``cache_build_common.ml_to_strategy``."""

    if not ml_id:
        return None
    match = re.search(r"(\d+)$", str(ml_id))
    return f"strategy_{int(match.group(1)):03d}" if match else str(ml_id)


def cache_market_id(view_type: str, ml_id: str | None, cd_id: str | None = None) -> str | None:
    """The value stored in, and matched against, ``cache_cause.market_id``.

    Both views key off the parent ML, which is why a CD read cannot currently
    distinguish sibling markets. Kept faithful to the producer on purpose: the
    reader must match what is actually stored, not what ought to be stored.
    """

    del cd_id  # parent-ML derived by contract; named to document the asymmetry
    return strategy_id_for(ml_id)


def cache_view_source_id(
    view_type: str,
    ml_id: str | None,
    cd_id: str | None = None,
) -> str | None:
    """The market the row is really about."""

    if view_type == VIEW_COMPETITIVE_DYNAMICS:
        return cd_id
    return ml_id


def cache_cause_identity(
    *,
    brand: str,
    view_type: str,
    source: str,
    measure: str,
    ml_id: str | None,
    cd_id: str | None = None,
) -> tuple:
    """Full target identity, including the CD child that today's PK omits."""

    return (
        brand,
        view_type,
        str(source).upper(),
        measure,
        cache_market_id(view_type, ml_id, cd_id),
        cache_view_source_id(view_type, ml_id, cd_id),
    )


#: Columns the producer fills when the target table has them. Everything here is
#: additive and nullable, so a producer built after the migration still writes
#: correctly against a table that has not been migrated yet.
CACHE_CAUSE_OPTIONAL_COLUMNS: tuple[str, ...] = (
    "view_source_id",
    "run_id",
    "build_sha",
    "input_manifest_json",
)


class CacheCauseKeyCollision(RuntimeError):
    """Two rows for different markets would occupy one primary key.

    ``REPLACE INTO`` resolves such a pair by dropping one row, silently. Raising
    turns a silent loss into a stopped build.
    """

    def __init__(self, collisions: dict) -> None:
        preview = "; ".join(
            f"{'/'.join(str(part) for part in key)} <- {sources}"
            for key, sources in sorted(collisions.items(), key=lambda item: str(item[0]))[:5]
        )
        super().__init__(
            f"{len(collisions)} cache_cause primary key collision(s); "
            f"REPLACE INTO would drop rows: {preview}"
        )
        self.collisions = collisions


def usable_optional_columns(existing_columns) -> tuple[str, ...]:
    """Intersect the desired provenance columns with what the table actually has."""

    present = {str(name) for name in existing_columns}
    return tuple(name for name in CACHE_CAUSE_OPTIONAL_COLUMNS if name in present)


def assert_no_key_collisions(identities) -> None:
    collisions = find_key_collisions(identities)
    if collisions:
        raise CacheCauseKeyCollision(collisions)


def find_key_collisions(identities) -> dict:
    """Group target identities that collapse onto the same *current* PK.

    A non-empty result means ``REPLACE INTO`` would drop rows: same PK, different
    ``view_source_id``. Used by the producer as a fail-closed pre-flight so the
    loss is loud instead of silent.
    """

    by_key: dict[tuple, set] = {}
    for identity in identities:
        key = tuple(identity[:5])
        by_key.setdefault(key, set()).add(identity[5])
    return {key: sorted(str(v) for v in sources) for key, sources in by_key.items() if len(sources) > 1}
