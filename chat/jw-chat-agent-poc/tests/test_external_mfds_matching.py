from __future__ import annotations

from jw_chat_agent_poc.agent_loop.external_tools import _first_matching_mfds_item
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tool_use.registry import ExternalToolRegistry
from jw_chat_agent_poc.tools.external import ExternalApiClient
from jw_chat_agent_poc.tools.external import ExternalCall


def _permission_call(items: list[dict[str, str]]) -> ExternalCall:
    return ExternalCall(
        tool="mfds_permission_search",
        source="external_api",
        status="fixture",
        summary_text="MFDS fixture",
        render_data={"resultCode": "00", "items": items},
    )


def test_mfds_brand_match_rejects_substring_false_positive() -> None:
    # Given: a substring collision appears before the real product family.
    call = _permission_call(
        [
            {"ITEM_SEQ": "bad", "ITEM_NAME": "리바로트정"},
            {"ITEM_SEQ": "good", "ITEM_NAME": "리바로정1밀리그램"},
        ]
    )

    # When: the canonical brand is matched against the MFDS rows.
    item = _first_matching_mfds_item(call, "리바로")

    # Then: the unrelated rivaroxaban product is not selected.
    assert item is not None
    assert item["ITEM_SEQ"] == "good"


def test_mfds_brand_match_allows_exact_product_family_prefix() -> None:
    # Given: an exact product name and a dosage-form product name.
    exact = _permission_call([{"ITEM_SEQ": "exact", "ITEM_NAME": "리바로"}])
    dosage = _permission_call([{"ITEM_SEQ": "dosage", "ITEM_NAME": "리바로정2밀리그램"}])

    # When: each row is matched against the canonical brand.
    exact_item = _first_matching_mfds_item(exact, "리바로")
    dosage_item = _first_matching_mfds_item(dosage, "리바로")

    # Then: exact and known dosage-form boundaries remain valid.
    assert exact_item is not None and exact_item["ITEM_SEQ"] == "exact"
    assert dosage_item is not None and dosage_item["ITEM_SEQ"] == "dosage"


def test_permission_search_spec_projects_only_canonical_product_family(monkeypatch) -> None:
    # Given: the upstream substring search returns an unrelated collision first.
    call = _permission_call(
        [
            {"ITEM_SEQ": "bad", "ITEM_NAME": "리바로트정"},
            {"ITEM_SEQ": "good", "ITEM_NAME": "리바로정1밀리그램"},
        ]
    )
    external = ExternalApiClient(mode="fixture")
    monkeypatch.setattr(external, "mfds_permission_search", lambda _brand: call)
    registry = ExternalToolRegistry(resolver=BrandResolver(), external=external)
    spec = next(
        item
        for item in registry.list_for_query("허가 품목")
        if item.name == "mfds_permission_search"
    )

    # When: the public ToolSpec normalizes the upstream response.
    envelope = spec.execute(spec.input_model.model_validate({"brand": "리바로"}))

    # Then: only the canonical product family becomes evidence.
    locators = tuple(fact.source_locator for fact in envelope.evidence)
    assert any(locator and "리바로정" in locator for locator in locators)
    assert all(not locator or "리바로트" not in locator for locator in locators)


def test_main_ingredient_spec_fails_closed_when_ingredient_field_is_absent(monkeypatch) -> None:
    # Given: MFDS finds the product family but returns no ingredient field.
    external = ExternalApiClient(mode="fixture")
    monkeypatch.setattr(
        external,
        "mfds_permission_search",
        lambda _brand: _permission_call(
            [{"ITEM_SEQ": "missing-ingredient", "ITEM_NAME": "리바로정1밀리그램"}]
        ),
    )
    registry = ExternalToolRegistry(resolver=BrandResolver(), external=external)
    spec = next(
        item
        for item in registry.list_for_query("주성분")
        if item.name == "get_drug_main_ingredient"
    )

    # When: the public ToolSpec normalizes the incomplete row.
    envelope = spec.execute(spec.input_model.model_validate({"brand": "리바로"}))

    # Then: a product name is never promoted into ingredient evidence.
    assert envelope.ok is False
    assert envelope.evidence == ()
    assert envelope.error_code == "NO_EVIDENCE"
