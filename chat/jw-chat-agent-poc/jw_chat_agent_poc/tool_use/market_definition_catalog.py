from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from jw_chat_agent_poc.resolver.catalog_membership import (
    MariaDbCatalogMembershipReader,
)


CatalogRow = dict[str, object]


class MarketDefinitionCatalogReader(Protocol):
    def market_landscape(self, market_id: str) -> CatalogRow | None: ...

    def competitive_dynamics(self, market_id: str) -> CatalogRow | None: ...

    def competitive_markets(self, parent_market_id: str) -> tuple[CatalogRow, ...]: ...

    def strategic_brands(
        self,
        *,
        brand: str | None = None,
        market_id: str | None = None,
        competitive_market_id: str | None = None,
    ) -> tuple[CatalogRow, ...]: ...

    def atc4(self, code: str) -> CatalogRow | None: ...


@dataclass(frozen=True, slots=True)
class StaticMarketDefinitionCatalogReader:
    market_landscape_rows: tuple[CatalogRow, ...]
    competitive_dynamics_rows: tuple[CatalogRow, ...]
    strategic_brand_rows: tuple[CatalogRow, ...]
    atc4_rows: tuple[CatalogRow, ...] = ()

    def market_landscape(self, market_id: str) -> CatalogRow | None:
        return _find(self.market_landscape_rows, "ml_id", market_id)

    def competitive_dynamics(self, market_id: str) -> CatalogRow | None:
        return _find(self.competitive_dynamics_rows, "cd_id", market_id)

    def competitive_markets(self, parent_market_id: str) -> tuple[CatalogRow, ...]:
        return tuple(
            row
            for row in self.competitive_dynamics_rows
            if _text(row.get("ml_id")) == parent_market_id
        )

    def strategic_brands(
        self,
        *,
        brand: str | None = None,
        market_id: str | None = None,
        competitive_market_id: str | None = None,
    ) -> tuple[CatalogRow, ...]:
        return tuple(
            row
            for row in self.strategic_brand_rows
            if (brand is None or _matches_brand(row, brand))
            and (market_id is None or _text(row.get("ml_id")) == market_id)
            and (
                competitive_market_id is None
                or _text(row.get("cd_id")) == competitive_market_id
            )
        )

    def atc4(self, code: str) -> CatalogRow | None:
        return _find(self.atc4_rows, "atc4_code", code)


@dataclass(frozen=True, slots=True)
class MariaDbMarketDefinitionCatalogReader:
    """Read the existing runtime market catalog without creating another store."""

    connection: MariaDbCatalogMembershipReader = field(
        default_factory=MariaDbCatalogMembershipReader
    )

    def market_landscape(self, market_id: str) -> CatalogRow | None:
        return self._fetch_one(
            "SELECT * FROM catalog_ml_market WHERE ml_id = %s",
            (market_id,),
        )

    def competitive_dynamics(self, market_id: str) -> CatalogRow | None:
        return self._fetch_one(
            "SELECT * FROM catalog_cd_market WHERE cd_id = %s",
            (market_id,),
        )

    def competitive_markets(self, parent_market_id: str) -> tuple[CatalogRow, ...]:
        return self._fetch_all(
            "SELECT * FROM catalog_cd_market WHERE ml_id = %s ORDER BY cd_id",
            (parent_market_id,),
        )

    def strategic_brands(
        self,
        *,
        brand: str | None = None,
        market_id: str | None = None,
        competitive_market_id: str | None = None,
    ) -> tuple[CatalogRow, ...]:
        clauses = ["is_excluded = 0"]
        values: list[object] = []
        if brand is not None:
            clauses.append(
                "LOWER(%s) IN (LOWER(name), LOWER(merge_name), "
                "LOWER(canonical_name), LOWER(general_brand_key))"
            )
            values.append(brand)
        if market_id is not None:
            clauses.append("ml_id = %s")
            values.append(market_id)
        if competitive_market_id is not None:
            clauses.append("cd_id = %s")
            values.append(competitive_market_id)
        return self._fetch_all(
            "SELECT * FROM catalog_strategic_brand WHERE "
            + " AND ".join(clauses)
            + " ORDER BY brand_id",
            tuple(values),
        )

    def atc4(self, code: str) -> CatalogRow | None:
        return self._fetch_one(
            "SELECT atc4_code, MAX(atc4_desc) AS atc4_desc "
            "FROM mart_general_market_metric WHERE atc4_code = %s "
            "GROUP BY atc4_code",
            (code,),
        )

    def _fetch_one(self, query: str, values: tuple[object, ...]) -> CatalogRow | None:
        rows = self._fetch_all(query, values)
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError(f"market definition lookup returned {len(rows)} rows")
        return rows[0]

    def _fetch_all(self, query: str, values: tuple[object, ...]) -> tuple[CatalogRow, ...]:
        import pymysql

        config = self.connection
        with pymysql.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database=config.database,
            connect_timeout=config.connect_timeout_s,
            read_timeout=config.read_timeout_s,
            write_timeout=config.read_timeout_s,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("START TRANSACTION READ ONLY")
                cursor.execute(query, values)
                rows = tuple(dict(row) for row in cursor.fetchall())
                connection.rollback()
        return rows


def _find(rows: tuple[CatalogRow, ...], key: str, value: str) -> CatalogRow | None:
    matches = [row for row in rows if _text(row.get(key)) == value]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(f"market definition lookup returned {len(matches)} rows")
    return matches[0]


def _matches_brand(row: Mapping[str, object], brand: str) -> bool:
    target = brand.casefold()
    return target in {
        _text(row.get(key)).casefold()
        for key in ("name", "merge_name", "canonical_name", "general_brand_key")
        if _text(row.get(key))
    }


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""
