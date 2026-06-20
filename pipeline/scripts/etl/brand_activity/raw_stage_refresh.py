"""Derived stage refresh from brand-activity raw tables."""

from __future__ import annotations

from pipeline.scripts.etl.brand_activity.csd_core import CsdRow, source_month_key


def refresh_stage(cursor: object, raw_schema: str, stage_schema: str, window: tuple[str, str]) -> dict[str, int]:
    """Rebuild legacy stage tables from raw rows inside the analysis window."""
    start, end = window
    cursor.execute(f"TRUNCATE TABLE `{stage_schema}`.`csd_channel_dynamics_stage`")
    cursor.execute(f"TRUNCATE TABLE `{stage_schema}`.`km_keyword_event_stage`")
    csd_rows = _canonical_csd_stage_rows(cursor, raw_schema, start, end)
    _insert_csd_stage(cursor, stage_schema, csd_rows)
    keyword_count = _copy_keyword_stage(cursor, raw_schema, stage_schema, start, end)
    return {
        "csd_channel_dynamics_stage": len(csd_rows),
        "km_keyword_event_stage": keyword_count,
    }


def _canonical_csd_stage_rows(cursor: object, schema: str, start: str, end: str) -> list[CsdRow]:
    """Pick the latest annual-source CSD row for each stage grain."""
    cursor.execute(
        f"""
        SELECT source_file, source_sheet, source_row_no, period_ym, market, jw_channel,
               master_product, representing_company, product_details
        FROM `{schema}`.`raw_csd_channel_dynamics`
        WHERE selected_for_stage = 1 AND period_ym BETWEEN %s AND %s
        """,
        (start, end),
    )
    grouped: dict[tuple[str, str, str, str, str], CsdRow] = {}
    for raw in cursor.fetchall():
        row = CsdRow(
            source_file=str(raw[0]),
            source_sheet=str(raw[1]),
            source_row_no=int(raw[2]),
            period_ym=str(raw[3]),
            market=str(raw[4]),
            jw_channel=str(raw[5]),
            master_product=str(raw[6]),
            representing_company=str(raw[7]),
            product_details=int(raw[8]),
        )
        current = grouped.get(row.grain_key())
        if current is None or source_month_key(row.source_file) > source_month_key(current.source_file):
            grouped[row.grain_key()] = row
    return sorted(grouped.values(), key=lambda row: row.grain_key())


def _insert_csd_stage(cursor: object, schema: str, rows: list[CsdRow]) -> None:
    """Insert canonical CSD rows into the legacy stage schema."""
    if not rows:
        return
    cursor.executemany(
        f"""
        INSERT INTO `{schema}`.`csd_channel_dynamics_stage`
        (period_ym, market, jw_channel, master_product, representing_company, product_details,
         source_file, source_sheet, source_row_no)
        VALUES ({", ".join(["%s"] * 9)})
        """,
        [
            (
                row.period_ym,
                row.market,
                row.jw_channel,
                row.master_product,
                row.representing_company,
                row.product_details,
                row.source_file,
                row.source_sheet,
                row.source_row_no,
            )
            for row in rows
        ],
    )


def _copy_keyword_stage(cursor: object, raw_schema: str, stage_schema: str, start: str, end: str) -> int:
    """Copy raw Keyword events into the existing stage table."""
    cursor.execute(
        f"""
        INSERT INTO `{stage_schema}`.`km_keyword_event_stage`
        (period_ym, visit_location, specialty, representing_company, product_name, therapeutic_class,
         keyword_text, interest, prescription_frequency, prescription_evolution, abstract_lit, patient_lit,
         promotional_lit, samples_left, other_materials_left, what_other_materials, other_comments,
         source_file, source_sheet, source_row_no, source_file_sha256, stage_row_sha256)
        SELECT period_ym, visit_location, specialty, representing_company, product_name, therapeutic_class,
               keyword_text, interest, prescription_frequency, prescription_evolution, abstract_lit, patient_lit,
               promotional_lit, samples_left, other_materials_left, what_other_materials, other_comments,
               source_file, source_sheet, source_row_no, source_file_sha256, row_hash
        FROM `{raw_schema}`.`raw_keyword_events`
        WHERE period_ym BETWEEN %s AND %s
        ORDER BY period_ym, source_file, source_row_no
        """,
        (start, end),
    )
    return int(cursor.rowcount)
