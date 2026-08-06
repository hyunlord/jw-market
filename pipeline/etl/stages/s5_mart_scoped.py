from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class StrategicTable:
    name: str
    id_column: str
    id_kind: str


STRATEGIC_TABLES = (
    StrategicTable("mart_strategic_ml_brand_metric", "ml_id", "ml"),
    StrategicTable("mart_strategic_ml_market_metric", "ml_id", "ml"),
    StrategicTable("mart_strategic_cd_brand_metric", "cd_market_id", "cd"),
    StrategicTable("mart_strategic_cd_market_metric", "cd_market_id", "cd"),
)


def scoped_refresh_ids(params: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    ml_ids = _ids_from(params, "affected_ml_ids", "ml_ids")
    cd_ids = _ids_from(params, "affected_cd_ids", "cd_ids")
    legacy_id = str(params.get("ml_id") or "").strip()
    if legacy_id.startswith("ml_"):
        ml_ids = _sorted_unique((*ml_ids, legacy_id))
    if legacy_id.startswith("cd_"):
        cd_ids = _sorted_unique((*cd_ids, legacy_id))
    return ml_ids, cd_ids


def ensure_scoped_schema(
    conn: Any,
    target_db: str,
    source_db: str,
    affected_ml_ids: Sequence[str],
    affected_cd_ids: Sequence[str],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{target_db}` "
            "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        )
        for table in STRATEGIC_TABLES:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS `{target_db}`.`{table.name}` "
                f"LIKE `{source_db}`.`{table.name}`"
            )
            cur.execute(
                f"SELECT COUNT(*) AS row_count FROM `{target_db}`.`{table.name}`"
            )
            if int(cur.fetchone()["row_count"]) == 0:
                cur.execute(
                    f"INSERT INTO `{target_db}`.`{table.name}` "
                    f"SELECT * FROM `{source_db}`.`{table.name}`"
                )
        _delete_sql(cur, target_db, affected_ml_ids, affected_cd_ids)
    conn.commit()


def delete_affected_rows(
    rows: MutableMapping[tuple[str, str], list[dict[str, object]]],
    *,
    affected_ml_ids: Sequence[str],
    affected_cd_ids: Sequence[str],
) -> None:
    affected = {"ml": set(affected_ml_ids), "cd": set(affected_cd_ids)}
    for table in STRATEGIC_TABLES:
        key = ("target", table.name)
        rows[key] = [
            row for row in rows[key] if str(row.get(table.id_column)) not in affected[table.id_kind]
        ]


def unaffected_strategic_signatures(
    rows: Mapping[tuple[str, str], Sequence[Mapping[str, object]]],
    *,
    affected_ml_ids: Sequence[str],
    affected_cd_ids: Sequence[str],
) -> dict[str, tuple[int, str]]:
    affected = {"ml": set(affected_ml_ids), "cd": set(affected_cd_ids)}
    signatures: dict[str, tuple[int, str]] = {}
    for table in STRATEGIC_TABLES:
        stable_rows = [
            dict(row)
            for row in rows[("target", table.name)]
            if str(row.get(table.id_column)) not in affected[table.id_kind]
        ]
        signatures[table.name] = (len(stable_rows), _rows_hash(stable_rows))
    return signatures


def _delete_sql(
    cur: Any,
    target_db: str,
    affected_ml_ids: Sequence[str],
    affected_cd_ids: Sequence[str],
) -> None:
    for table in STRATEGIC_TABLES:
        ids = affected_ml_ids if table.id_kind == "ml" else affected_cd_ids
        if not ids:
            continue
        placeholders = ", ".join(["%s"] * len(ids))
        cur.execute(
            f"DELETE FROM `{target_db}`.`{table.name}` "
            f"WHERE `{table.id_column}` IN ({placeholders})",
            tuple(ids),
        )


def _ids_from(params: Mapping[str, Any], *keys: str) -> tuple[str, ...]:
    values: list[str] = []
    for key in keys:
        raw = params.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            values.append(raw)
            continue
        values.extend(str(item) for item in raw)
    return _sorted_unique(values)


def _sorted_unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if value.strip()}))


def _rows_hash(rows: Sequence[Mapping[str, object]]) -> str:
    payload = sorted(rows, key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True))
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
