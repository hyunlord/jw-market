from __future__ import annotations

import shutil
from pathlib import Path

TARGET_OVERRIDE_PATHS = (
    "pipeline/scripts/utils/ubist_target_channel_mapping.py",
    "pipeline/scripts/etl/ubist_channel_resolver.py",
)

# The archive ref stays immutable; Phase 1 applies target-only overrides only
# inside the temporary builder tree created by archive_runner.materialize_archive.
OLD_ANALYSIS_LEVEL_MARKET_STATUS_BLOCK = '''    target_customer_channels = analysis_levels.get("channels")
    if source_api == "UBIST":
        specialty_channels = ubist_channel_context.get("specialty_channels")
        if isinstance(specialty_channels, list) and specialty_channels:
            target_customer_channels = [str(channel) for channel in specialty_channels]
    analysis_level_market_channels = target_customer_channels or _channels_for_source(source_api)
'''

NEW_ANALYSIS_LEVEL_MARKET_STATUS_BLOCK = '''    analysis_level_market_channels = analysis_levels.get("channels") or _channels_for_source(source_api)
    target_customer_channels = analysis_levels.get("channels")
    if source_api == "UBIST":
        specialty_channels = ubist_channel_context.get("specialty_channels")
        if isinstance(specialty_channels, list) and specialty_channels:
            target_customer_channels = [str(channel) for channel in specialty_channels]
'''

TARGET_LABEL_HELPER_ANCHOR = '''def _analysis_level_market_status_by_channel(
'''

TARGET_LABEL_HELPER_BLOCK = '''def _target_label_replaced(value: Any, label_map: dict[str, str]) -> Any:
    if isinstance(value, str):
        return label_map.get(value, value)
    if isinstance(value, list):
        return [_target_label_replaced(item, label_map) for item in value]
    if isinstance(value, dict):
        return {
            label_map.get(key, key) if isinstance(key, str) else key: _target_label_replaced(item, label_map)
            for key, item in value.items()
        }
    return value


def _analysis_level_market_status_by_channel(
'''

TARGET_LABEL_REWRITE_ANCHOR = '''    target_customer_competition_by_channel = _target_customer_competition(
        rows=sibling_rows,
        source=source_api,
        target_name=brand_row.get("brand_name"),
        periods=periods,
        channels=target_customer_channels,
    )
'''

TARGET_LABEL_REWRITE_BLOCK = '''    target_customer_competition_by_channel = _target_customer_competition(
        rows=sibling_rows,
        source=source_api,
        target_name=brand_row.get("brand_name"),
        periods=periods,
        channels=target_customer_channels,
    )
    if source_api == "UBIST" and isinstance(ubist_channel_context, dict):
        target_label_map = ubist_channel_context.get("target_channel_label_map")
        if isinstance(target_label_map, dict) and target_label_map:
            target_customer_competition_by_channel = _target_label_replaced(
                target_customer_competition_by_channel,
                {str(key): str(value) for key, value in target_label_map.items()},
            )
'''

SPECIALTY_CHANNEL_OUTPUT_OLD = '''                ubist_channel_context.get("specialty_channels")
                if isinstance(ubist_channel_context, dict)
                else None
'''

SPECIALTY_CHANNEL_OUTPUT_NEW = '''                (
                    ubist_channel_context.get("specialty_display_channels")
                    or ubist_channel_context.get("specialty_channels")
                )
                if isinstance(ubist_channel_context, dict)
                else None
'''


class ArchiveTargetPatchError(RuntimeError):
    pass


def _copy_target_overrides(temp_root: Path, project_root: Path) -> None:
    for relative_path in TARGET_OVERRIDE_PATHS:
        source = project_root / relative_path
        destination = temp_root / relative_path
        if not source.exists():
            raise FileNotFoundError(f"target 4-bucket override not found: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _patch_analysis_level_market_status(temp_root: Path) -> None:
    cause_builder = temp_root / "pipeline" / "scripts" / "etl" / "build_cache_cause.py"
    text = cause_builder.read_text(encoding="utf-8")
    if OLD_ANALYSIS_LEVEL_MARKET_STATUS_BLOCK not in text:
        raise ArchiveTargetPatchError("cache cause archive no longer matches target-channel patch point")
    text = text.replace(
        OLD_ANALYSIS_LEVEL_MARKET_STATUS_BLOCK,
        NEW_ANALYSIS_LEVEL_MARKET_STATUS_BLOCK,
        1,
    )
    cause_builder.write_text(text, encoding="utf-8")


def _patch_target_display_labels(temp_root: Path) -> None:
    cause_builder = temp_root / "pipeline" / "scripts" / "etl" / "build_cache_cause.py"
    text = cause_builder.read_text(encoding="utf-8")
    if TARGET_LABEL_HELPER_ANCHOR not in text:
        raise ArchiveTargetPatchError("cache cause archive no longer matches target label helper point")
    if TARGET_LABEL_REWRITE_ANCHOR not in text:
        raise ArchiveTargetPatchError("cache cause archive no longer matches target label rewrite point")
    if SPECIALTY_CHANNEL_OUTPUT_OLD not in text:
        raise ArchiveTargetPatchError("cache cause archive no longer matches specialty channel output point")
    text = text.replace(TARGET_LABEL_HELPER_ANCHOR, TARGET_LABEL_HELPER_BLOCK, 1)
    text = text.replace(TARGET_LABEL_REWRITE_ANCHOR, TARGET_LABEL_REWRITE_BLOCK, 1)
    text = text.replace(SPECIALTY_CHANNEL_OUTPUT_OLD, SPECIALTY_CHANNEL_OUTPUT_NEW, 1)
    cause_builder.write_text(text, encoding="utf-8")


def apply_target_4bucket_patch(temp_root: Path, project_root: Path) -> None:
    _copy_target_overrides(temp_root, project_root)
    _patch_analysis_level_market_status(temp_root)
    _patch_target_display_labels(temp_root)
