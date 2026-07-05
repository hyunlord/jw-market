from __future__ import annotations

from typing import Any

from .db import DbConfig, connect
from .json_util import parse_history, parse_json_object
from .profile_provider import MoleculeRow
from .strength_candidate_extractor import MetricRow


BrandSource = str


class Agent3Repository:
    def __init__(self, config: DbConfig | None = None) -> None:
        self.config = config or DbConfig.from_env()

    def load_general_rows(self, brand_name: str) -> list[dict[str, Any]]:
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT brand_key, brand_name, atc4_code, atc4_desc, source, measure, unit_label,
                           raw_value_history, channel_data, specialty_data,
                           channel_specialty_matrix, dimension_data
                    FROM mart_general_brand_metric
                    WHERE (brand_name=%s OR brand_key=%s) AND measure='sales'
                    ORDER BY source, atc4_code, brand_key
                    """,
                    (brand_name, brand_name),
                )
                return list(cursor.fetchall())

    def load_general_rows_for_brands(self, brand_names: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not brand_names:
            return {}
        placeholders = ", ".join(["%s"] * len(brand_names))
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT brand_key, brand_name, atc4_code, atc4_desc, source, measure, unit_label,
                           raw_value_history, channel_data, specialty_data,
                           channel_specialty_matrix, dimension_data
                    FROM mart_general_brand_metric
                    WHERE (brand_key IN ({placeholders}) OR brand_name IN ({placeholders})) AND measure='sales'
                    ORDER BY brand_key, brand_name, source, atc4_code
                    """,
                    tuple(brand_names) + tuple(brand_names),
                )
                rows = list(cursor.fetchall())
        return _group_by_requested_brand(rows, brand_names)

    def load_brand_universe(self, source: BrandSource) -> list[str]:
        match source:
            case "strategic_ml":
                sql = """
                    SELECT DISTINCT brand_name
                    FROM mart_strategic_ml_brand_metric
                    WHERE measure='sales' AND brand_name IS NOT NULL AND brand_name <> ''
                    ORDER BY brand_name
                """
            case "general_all":
                sql = """
                    SELECT DISTINCT brand_key AS brand_name
                    FROM mart_general_brand_metric
                    WHERE measure='sales' AND brand_key IS NOT NULL AND brand_key <> ''
                    ORDER BY brand_key
                """
            case _:
                raise ValueError(f"unsupported brand source: {source}")  # noqa: GENERIC_ERR_OK
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql)
                return [str(row["brand_name"]) for row in cursor.fetchall()]

    def load_strategic_rows(self, brand_name: str) -> list[dict[str, Any]]:
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT ml_id, brand_key, brand_name, source, measure, overlay_data, dimension_data, raw_value_history
                    FROM mart_strategic_ml_brand_metric
                    WHERE (brand_name=%s OR brand_key=%s) AND measure='sales'
                    ORDER BY source, ml_id, brand_key
                    """,
                    (brand_name, brand_name),
                )
                return list(cursor.fetchall())

    def load_strategic_rows_for_brands(self, brand_names: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not brand_names:
            return {}
        placeholders = ", ".join(["%s"] * len(brand_names))
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT ml_id, brand_key, brand_name, source, measure, overlay_data, dimension_data, raw_value_history
                    FROM mart_strategic_ml_brand_metric
                    WHERE (brand_key IN ({placeholders}) OR brand_name IN ({placeholders})) AND measure='sales'
                    ORDER BY brand_key, brand_name, source, ml_id
                    """,
                    tuple(brand_names) + tuple(brand_names),
                )
                rows = list(cursor.fetchall())
        return _group_by_requested_brand(rows, brand_names)

    def load_molecule_rows(self, brand_name: str) -> list[MoleculeRow]:
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT brand_name, mart_source, molecule_display, component_count, is_combo_component
                    FROM mart_brand_molecule
                    WHERE brand_name=%s
                    ORDER BY mart_source, molecule_display
                    """,
                    (brand_name,),
                )
                rows = cursor.fetchall()
        return [
            MoleculeRow(
                brand_name=str(row["brand_name"]),
                mart_source=str(row["mart_source"]),
                molecule_display=str(row["molecule_display"]),
                component_count=int(row["component_count"] or 0),
                is_combo_component=bool(row["is_combo_component"]),
            )
            for row in rows
        ]

    def load_molecule_rows_for_brands(self, brand_names: list[str]) -> dict[str, list[MoleculeRow]]:
        if not brand_names:
            return {}
        placeholders = ", ".join(["%s"] * len(brand_names))
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT brand_name, mart_source, molecule_display, component_count, is_combo_component
                    FROM mart_brand_molecule
                    WHERE brand_name IN ({placeholders})
                    ORDER BY brand_name, mart_source, molecule_display
                    """,
                    tuple(brand_names),
                )
                rows = cursor.fetchall()
        grouped: dict[str, list[MoleculeRow]] = {brand: [] for brand in brand_names}
        for row in rows:
            grouped.setdefault(str(row["brand_name"]), []).append(
                MoleculeRow(
                    brand_name=str(row["brand_name"]),
                    mart_source=str(row["mart_source"]),
                    molecule_display=str(row["molecule_display"]),
                    component_count=int(row["component_count"] or 0),
                    is_combo_component=bool(row["is_combo_component"]),
                )
            )
        return grouped


def metric_rows_from_general(rows: list[dict[str, Any]]) -> list[MetricRow]:
    return [
        MetricRow(
            brand_name=str(row["brand_name"]),
            brand_key=str(row["brand_key"]),
            source=str(row["source"]),
            measure=str(row["measure"]),
            raw_value_history=parse_history(row.get("raw_value_history")),
            channel_data=parse_json_object(row.get("channel_data")),
            specialty_data=parse_json_object(row.get("specialty_data")),
            channel_specialty_matrix=parse_json_object(row.get("channel_specialty_matrix")),
            dimension_data=parse_json_object(row.get("dimension_data")),
        )
        for row in rows
    ]


def _group_by_requested_brand(rows: list[dict[str, Any]], requested: list[str]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {brand: [] for brand in requested}
    requested_set = set(requested)
    for row in rows:
        brand_key = str(row.get("brand_key") or "")
        brand_name = str(row.get("brand_name") or "")
        matched = False
        if brand_key in requested_set:
            grouped.setdefault(brand_key, []).append(row)
            matched = True
        if brand_name in requested_set and brand_name != brand_key:
            grouped.setdefault(brand_name, []).append(row)
            matched = True
        if not matched and brand_name:
            grouped.setdefault(brand_name, []).append(row)
    return grouped
