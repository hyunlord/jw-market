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
        raise RuntimeError("cache cause archive no longer matches target-channel patch point")
    text = text.replace(
        OLD_ANALYSIS_LEVEL_MARKET_STATUS_BLOCK,
        NEW_ANALYSIS_LEVEL_MARKET_STATUS_BLOCK,
        1,
    )
    cause_builder.write_text(text, encoding="utf-8")


def apply_target_4bucket_patch(temp_root: Path, project_root: Path) -> None:
    _copy_target_overrides(temp_root, project_root)
    _patch_analysis_level_market_status(temp_root)
