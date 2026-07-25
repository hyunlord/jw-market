from __future__ import annotations

import json

from pipeline.scripts.crawler.hira_benefit.scope import (
    brands_from_cache_payload,
    match_brand_names,
)


def test_jw_scope_is_loaded_from_cache_payload_without_hardcoding() -> None:
    payload = json.dumps(
        [
            {"brand": "리바로", "is_jw": True},
            {"brand": "리바로젯", "is_jw": True},
            {"brand": "경쟁약", "is_jw": False},
        ],
        ensure_ascii=False,
    )

    assert brands_from_cache_payload(payload) == ("리바로", "리바로젯")


def test_longer_brand_name_wins_without_losing_other_explicit_matches() -> None:
    text = "품명: 리바로젯정. 기존 리바로정 기준도 함께 개정한다."

    assert match_brand_names(text, ("리바로", "리바로젯")) == (
        "리바로젯",
        "리바로",
    )


def test_nfd_and_case_are_normalized_for_matching() -> None:
    assert match_brand_names("ACTEMRA 리바로", ("actemra", "리바로")) == (
        "actemra",
        "리바로",
    )
