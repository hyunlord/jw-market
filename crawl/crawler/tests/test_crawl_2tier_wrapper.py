from __future__ import annotations

import argparse
import sys
from pathlib import Path

CRAWLER_DIR = Path(__file__).resolve().parents[1]
if str(CRAWLER_DIR) not in sys.path:
    sys.path.insert(0, str(CRAWLER_DIR))

from crawl_2tier import build_tier1_command


def test_build_tier1_command_when_delegating_to_orchestrator() -> None:
    # Given: Tier1 wrapper arguments.
    args = argparse.Namespace(
        sites="의학신문,데일리팜",
        drug_profile_dir="/tmp/drug_profiles",
        output_dir="/tmp/out",
        months=60,
        delay_sec=5.0,
        concurrent_sites=4,
        max_pages_per_site=10,
        unique_json_per_url=False,
    )

    # When: the wrapper builds the orchestrator command.
    command = build_tier1_command(args)

    # Then: the command uses the orchestrator's actual argparse names.
    assert "--drug-profile-dir" in command
    assert "--output-base" in command
    assert "--delay" in command
    assert "--max-pages" in command
    assert "--output-base-dir" not in command
    assert "--delay-sec" not in command
    assert "--max-articles" not in command
