from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator.hira_disease import explicit_hira_disease_code
from jw_chat_agent_poc.tools.external.client import _mcp_tool_spec


HIRA_STAT_TOOLS = (
    "hira_disease_hospitalization_outpatient_stats",
    "hira_disease_gender_age_stats",
    "hira_disease_institution_class_stats",
    "hira_disease_area_stats",
)


@pytest.mark.parametrize(("display_code", "request_code"), (("D69.3", "D693"), ("H36.0", "H360")))
@pytest.mark.parametrize("tool", HIRA_STAT_TOOLS)
def test_four_character_kcd_uses_compact_hira_code_and_sick_type_two(
    tool: str,
    display_code: str,
    request_code: str,
) -> None:
    spec = _mcp_tool_spec(tool, {"sickCd": display_code, "year": "2024"})

    assert spec["arguments"]["sick_cd"] == request_code
    assert spec["arguments"]["sick_type"] == "2"
    assert spec["arguments"]["year"] == "2024"


@pytest.mark.parametrize("display_code", ("J00", "E11", "I10", "D69"))
@pytest.mark.parametrize("tool", HIRA_STAT_TOOLS)
def test_three_character_kcd_keeps_sick_type_one(tool: str, display_code: str) -> None:
    spec = _mcp_tool_spec(tool, {"sickCd": display_code, "year": "2024"})

    assert spec["arguments"]["sick_cd"] == display_code
    assert spec["arguments"]["sick_type"] == "1"


@pytest.mark.parametrize(("display_code", "request_code", "sick_type"), (("D69.3", "D693", "2"), ("I10", "I10", "1")))
def test_disease_code_search_uses_the_same_hira_wire_contract(
    display_code: str,
    request_code: str,
    sick_type: str,
) -> None:
    spec = _mcp_tool_spec(
        "hira_disease_name_code",
        {"sickCd": display_code, "searchText": display_code, "diseaseType": "SICK_CD"},
    )

    assert spec["arguments"]["search_text"] == request_code
    assert spec["arguments"]["sick_type"] == sick_type


@pytest.mark.parametrize(("question", "display_code"), (("상병코드 D693", "D69.3"), ("질병코드 H360", "H36.0")))
def test_user_facing_kcd_binding_remains_dotted(question: str, display_code: str) -> None:
    assert explicit_hira_disease_code(question) == display_code

