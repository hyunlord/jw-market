from __future__ import annotations

import shutil
from pathlib import Path


HELPER_PATH = "pipeline/scripts/api/market_definition_display.py"

MARKET_STATUS_IMPORT_ANCHOR = '''from pipeline.etl.io.cache.archive_services_shim import MARKET_STATUS_COMPANY_BY_BRAND
'''
MARKET_STATUS_IMPORT_BLOCK = '''from pipeline.etl.io.cache.archive_services_shim import MARKET_STATUS_COMPANY_BY_BRAND
from pipeline.scripts.api.market_definition_display import cd_display_for_catalog_row
'''

MARKET_STATUS_ATC_OLD = '''    atc_codes = _catalog_atc_codes(market)
'''
MARKET_STATUS_ATC_NEW = '''    cd_display = cd_display_for_catalog_row(brand_row.get("catalog_row"))
    atc_codes = cd_display.atc_codes if cd_display else _catalog_atc_codes(market)
'''

MARKET_STATUS_LABEL_OLD = '''            "market_definition_label": _market_definition_label(atc_codes),
            "market_definition_full": ", ".join(atc_codes),
            "atc_count": len(atc_codes),
'''
MARKET_STATUS_LABEL_NEW = '''            "market_definition_label": cd_display.label if cd_display else _market_definition_label(atc_codes),
            "market_definition_full": cd_display.full if cd_display else ", ".join(atc_codes),
            "atc_count": len(atc_codes),
'''

CAUSE_IMPORT_ANCHOR = '''from pipeline.scripts.etl.ubist_channel_resolver import resolve_market_channels, strategic_channel_totals_context
'''
CAUSE_IMPORT_BLOCK = '''from pipeline.scripts.etl.ubist_channel_resolver import resolve_market_channels, strategic_channel_totals_context
from pipeline.scripts.api.market_definition_display import cd_display_for_catalog_row
'''

CAUSE_DISPLAY_ANCHOR = '''    catalog_members = _catalog_members_for_market(strategic_brand, view_source_id)
'''
CAUSE_DISPLAY_BLOCK = '''    catalog_members = _catalog_members_for_market(strategic_brand, view_source_id)
    cd_display = cd_display_for_catalog_row(market_catalog_row) if view_type == "competitive_dynamics" else None
    display_atc_codes = cd_display.atc_codes if cd_display else atc_codes_from_market_catalog(market_catalog_row)
'''

CAUSE_META_OLD = '''            "market_definition_label": (BRAND_METADATA_BY_NAME.get(brand_row["brand_name"]).market_label_kor if BRAND_METADATA_BY_NAME.get(brand_row["brand_name"]) else market_name),
            "market_definition_full": f"{market_name} 시장 정의" if market_name else None,
'''
CAUSE_META_NEW = '''            "market_definition_label": cd_display.label if cd_display else (BRAND_METADATA_BY_NAME.get(brand_row["brand_name"]).market_label_kor if BRAND_METADATA_BY_NAME.get(brand_row["brand_name"]) else market_name),
            "market_definition_full": cd_display.full if cd_display else (f"{market_name} 시장 정의" if market_name else None),
'''

CAUSE_ATC_OLD = '''            "atc_codes": atc_codes_from_market_catalog(market_catalog_row),
'''
CAUSE_ATC_NEW = '''            "atc_codes": display_atc_codes,
'''

CAUSE_CD_MARKET_ROW_OLD = '''                market_catalog_row=ml,
'''
CAUSE_CD_MARKET_ROW_NEW = '''                market_catalog_row=cd,
'''


class ArchiveCdDisplayPatchError(RuntimeError):
    pass


def apply_cd_display_patch(temp_root: Path, project_root: Path) -> None:
    _copy_helper(temp_root, project_root)
    _patch_market_status_builder(temp_root)
    _patch_cause_builder(temp_root)


def _copy_helper(temp_root: Path, project_root: Path) -> None:
    source = project_root / HELPER_PATH
    destination = temp_root / HELPER_PATH
    if not source.exists():
        raise FileNotFoundError(f"CD display helper not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _patch_market_status_builder(temp_root: Path) -> None:
    builder = temp_root / "pipeline" / "scripts" / "etl" / "build_cache_market_status.py"
    text = builder.read_text(encoding="utf-8")
    text = _replace_once(text, MARKET_STATUS_IMPORT_ANCHOR, MARKET_STATUS_IMPORT_BLOCK, builder)
    text = _replace_once(text, MARKET_STATUS_ATC_OLD, MARKET_STATUS_ATC_NEW, builder)
    text = _replace_once(text, MARKET_STATUS_LABEL_OLD, MARKET_STATUS_LABEL_NEW, builder)
    builder.write_text(text, encoding="utf-8")


def _patch_cause_builder(temp_root: Path) -> None:
    builder = temp_root / "pipeline" / "scripts" / "etl" / "build_cache_cause.py"
    text = builder.read_text(encoding="utf-8")
    text = _replace_once(text, CAUSE_IMPORT_ANCHOR, CAUSE_IMPORT_BLOCK, builder)
    text = _replace_once(text, CAUSE_DISPLAY_ANCHOR, CAUSE_DISPLAY_BLOCK, builder)
    text = _replace_once(text, CAUSE_META_OLD, CAUSE_META_NEW, builder)
    text = _replace_once(text, CAUSE_ATC_OLD, CAUSE_ATC_NEW, builder)
    text = _replace_once(text, CAUSE_CD_MARKET_ROW_OLD, CAUSE_CD_MARKET_ROW_NEW, builder)
    builder.write_text(text, encoding="utf-8")


def _replace_once(text: str, old: str, new: str, path: Path) -> str:
    if old not in text:
        raise ArchiveCdDisplayPatchError(f"archive builder no longer matches CD display patch point: {path}")
    return text.replace(old, new, 1)
