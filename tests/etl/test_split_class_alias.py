from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.scripts.api.dynamic_market.cause_payload import _ensure_class_alias
from pipeline.scripts.etl.build_cache_cause import _ensure_split_class_alias


@pytest.mark.parametrize("ensure_alias", [_ensure_class_alias, _ensure_split_class_alias])
def test_split_class_alias_prefers_class_2(ensure_alias) -> None:
    class_1 = {"segments": ["전체", "JAKi", "Biologics"]}
    class_2 = {"segments": ["IL-6", "TNF-alpha", "JAK", "IL-17", "IL-12/23", "기타"]}
    payload = {
        "levels": ["Class 1", "Class 2"],
        "data": {"Class 1": class_1, "Class 2": class_2},
    }

    result = ensure_alias(deepcopy(payload))

    assert result["data"]["Class"] == class_2
    assert result["data"]["Class"] != class_1
    assert result["data"]["Class 1"] == class_1
    assert result["data"]["Class 2"] == class_2
    assert result["data"]["Class"] is not result["data"]["Class 2"]


@pytest.mark.parametrize("ensure_alias", [_ensure_class_alias, _ensure_split_class_alias])
def test_split_class_alias_falls_back_to_class_1_when_class_2_is_absent(ensure_alias) -> None:
    class_1 = {"segments": ["전체"]}
    payload = {"levels": ["Class 1"], "data": {"Class 1": class_1}}

    result = ensure_alias(deepcopy(payload))

    assert result["data"]["Class"] == class_1


@pytest.mark.parametrize("ensure_alias", [_ensure_class_alias, _ensure_split_class_alias])
def test_split_class_alias_uses_class_2_without_requiring_class_1(ensure_alias) -> None:
    class_2 = {"segments": ["detail"]}
    payload = {"levels": ["Class 2"], "data": {"Class 2": class_2}}

    result = ensure_alias(deepcopy(payload))

    assert result["data"]["Class"] == class_2


@pytest.mark.parametrize("ensure_alias", [_ensure_class_alias, _ensure_split_class_alias])
def test_split_class_alias_preserves_existing_generic_class(ensure_alias) -> None:
    existing = {"segments": ["existing"]}
    payload = {
        "levels": ["Class", "Class 1", "Class 2"],
        "data": {
            "Class": existing,
            "Class 1": {"segments": ["class-1"]},
            "Class 2": {"segments": ["class-2"]},
        },
    }

    result = ensure_alias(deepcopy(payload))

    assert result["data"]["Class"] == existing
