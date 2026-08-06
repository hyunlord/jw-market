from __future__ import annotations

import re
import unicodedata
from typing import Any

from pipeline.etl.mi_master_registry import (
    MiMasterRegistry,
    default_mi_master_registry,
)

CD_FILTER_RAW_JSON_BY_ID: dict[str, str] = {
    "cd_001": "[{\"column_id\":\"C\",\"label\":null,\"product_name_kor\":\"라베칸/라베칸듀오\",\"row_id\":48,\"value\":\"라베칸: [A2B2] 프로톤 펌프 억제제 - Rabeprazole 단일제 \"},{\"column_id\":\"C\",\"label\":null,\"product_name_kor\":\"라베칸/라베칸듀오\",\"row_id\":49,\"value\":\"라베칸 듀오: [A2B2] 프로톤 펌프 억제제 - Rabeprazole/제산제 FDC \"},{\"column_id\":\"C\",\"label\":null,\"product_name_kor\":\"라베칸/라베칸듀오\",\"row_id\":50,\"value\":null}]",
    "cd_002": "[{\"column_id\":\"D\",\"label\":null,\"product_name_kor\":\"제이클\",\"row_id\":48,\"value\":\"A06B1 & A06B2 - 비급여(NON_NHI) \"},{\"column_id\":\"D\",\"label\":null,\"product_name_kor\":\"제이클\",\"row_id\":49,\"value\":null},{\"column_id\":\"D\",\"label\":null,\"product_name_kor\":\"제이클\",\"row_id\":50,\"value\":null}]",
    "cd_003": "[{\"column_id\":\"E\",\"label\":null,\"product_name_kor\":\"가드렛/가드메트\",\"row_id\":48,\"value\":\"A10N3\"},{\"column_id\":\"E\",\"label\":null,\"product_name_kor\":\"가드렛/가드메트\",\"row_id\":49,\"value\":\"A10N1\"},{\"column_id\":\"E\",\"label\":null,\"product_name_kor\":\"가드렛/가드메트\",\"row_id\":50,\"value\":null}]",
    "cd_004": "[{\"column_id\":\"F\",\"label\":null,\"product_name_kor\":\"타발리스\",\"row_id\":48,\"value\":\"B02E1\"},{\"column_id\":\"F\",\"label\":null,\"product_name_kor\":\"타발리스\",\"row_id\":49,\"value\":\"B02E9\"},{\"column_id\":\"F\",\"label\":null,\"product_name_kor\":\"타발리스\",\"row_id\":50,\"value\":null}]",
    "cd_005": "[{\"column_id\":\"G\",\"label\":null,\"product_name_kor\":\"시그마트\",\"row_id\":48,\"value\":\"[C1D] 관상동맥 치료제\"},{\"column_id\":\"G\",\"label\":null,\"product_name_kor\":\"시그마트\",\"row_id\":49,\"value\":null},{\"column_id\":\"G\",\"label\":null,\"product_name_kor\":\"시그마트\",\"row_id\":50,\"value\":null}]",
    "cd_006": "[{\"column_id\":\"H\",\"label\":null,\"product_name_kor\":\"리바로/리바로젯\",\"row_id\":48,\"value\":\"[C10C] 지질조절제 복합제제\"},{\"column_id\":\"H\",\"label\":null,\"product_name_kor\":\"리바로/리바로젯\",\"row_id\":49,\"value\":\"[C10A1] 스타틴류 (HMG-CoA 환원효소 억제제)\"},{\"column_id\":\"H\",\"label\":null,\"product_name_kor\":\"리바로/리바로젯\",\"row_id\":50,\"value\":null}]",
    "cd_007": "[{\"column_id\":\"I\",\"label\":null,\"product_name_kor\":\"리바로페노\",\"row_id\":48,\"value\":\"[C10C] 지질조절제 복합제제\"},{\"column_id\":\"I\",\"label\":null,\"product_name_kor\":\"리바로페노\",\"row_id\":49,\"value\":\"[C10A2] Fibrate 유도체\"},{\"column_id\":\"I\",\"label\":null,\"product_name_kor\":\"리바로페노\",\"row_id\":50,\"value\":\"[C10B] 천연물질 유래 동맥경화치료제\"}]",
    "cd_008": "[{\"column_id\":\"J\",\"label\":null,\"product_name_kor\":\"리바로하이\",\"row_id\":48,\"value\":\"[C11A1] 심혈관 질환 다중요법 목적의 복합제제 (단일 투약 형태) - Statin/ARB/CCB\"},{\"column_id\":\"J\",\"label\":null,\"product_name_kor\":\"리바로하이\",\"row_id\":49,\"value\":null},{\"column_id\":\"J\",\"label\":null,\"product_name_kor\":\"리바로하이\",\"row_id\":50,\"value\":null}]",
    "cd_009": "[{\"column_id\":\"K\",\"label\":null,\"product_name_kor\":\"리바로브이\",\"row_id\":48,\"value\":\"[C11A1] 심혈관 질환 다중요법 목적의 복합제제 (단일 투약 형태) - Statin/ARB\"},{\"column_id\":\"K\",\"label\":null,\"product_name_kor\":\"리바로브이\",\"row_id\":49,\"value\":null},{\"column_id\":\"K\",\"label\":null,\"product_name_kor\":\"리바로브이\",\"row_id\":50,\"value\":null}]",
    "cd_010": "[{\"column_id\":\"L\",\"label\":null,\"product_name_kor\":\"트루패스\",\"row_id\":48,\"value\":\"[G4C2] BPH 알파 아드레날린 길항제\"},{\"column_id\":\"L\",\"label\":null,\"product_name_kor\":\"트루패스\",\"row_id\":49,\"value\":null},{\"column_id\":\"L\",\"label\":null,\"product_name_kor\":\"트루패스\",\"row_id\":50,\"value\":null}]",
    "cd_011": "[{\"column_id\":\"M\",\"label\":null,\"product_name_kor\":\"피나스타/제이다트\",\"row_id\":48,\"value\":\"[G4C3] BPH 5- 알파 환원 효소 저해 테스토스테론 단일제\"},{\"column_id\":\"M\",\"label\":null,\"product_name_kor\":\"피나스타/제이다트\",\"row_id\":49,\"value\":null},{\"column_id\":\"M\",\"label\":null,\"product_name_kor\":\"피나스타/제이다트\",\"row_id\":50,\"value\":null}]",
    "cd_012": "[{\"column_id\":\"N\",\"label\":null,\"product_name_kor\":\"뉴트로진\",\"row_id\":48,\"value\":\"L03A1 COLONY-STIMULATING FACT.\"},{\"column_id\":\"N\",\"label\":null,\"product_name_kor\":\"뉴트로진\",\"row_id\":49,\"value\":null},{\"column_id\":\"N\",\"label\":null,\"product_name_kor\":\"뉴트로진\",\"row_id\":50,\"value\":null}]",
    "cd_013": "[{\"column_id\":\"O\",\"label\":null,\"product_name_kor\":\"모빌리아\",\"row_id\":48,\"value\":\"L03A9 OTH.IMMUNOSTIM.EX.INTFRN\"},{\"column_id\":\"O\",\"label\":null,\"product_name_kor\":\"모빌리아\",\"row_id\":49,\"value\":null},{\"column_id\":\"O\",\"label\":null,\"product_name_kor\":\"모빌리아\",\"row_id\":50,\"value\":null}]",
    "cd_014": "[{\"column_id\":\"P\",\"label\":null,\"product_name_kor\":\"악템라\",\"row_id\":48,\"value\":null},{\"column_id\":\"P\",\"label\":null,\"product_name_kor\":\"악템라\",\"row_id\":49,\"value\":null},{\"column_id\":\"P\",\"label\":null,\"product_name_kor\":\"악템라\",\"row_id\":50,\"value\":null}]",
    "cd_015": "[{\"column_id\":\"Q\",\"label\":null,\"product_name_kor\":\"페린젝트\",\"row_id\":48,\"value\":\"B03A1IRON PLAIN - IV Iron \"},{\"column_id\":\"Q\",\"label\":null,\"product_name_kor\":\"페린젝트\",\"row_id\":49,\"value\":null},{\"column_id\":\"Q\",\"label\":null,\"product_name_kor\":\"페린젝트\",\"row_id\":50,\"value\":null},{\"column_id\":\"R\",\"label\":null,\"product_name_kor\":\"베노훼럼\",\"row_id\":48,\"value\":\"B03A1IRON PLAIN - IV Iron \"},{\"column_id\":\"R\",\"label\":null,\"product_name_kor\":\"베노훼럼\",\"row_id\":49,\"value\":null},{\"column_id\":\"R\",\"label\":null,\"product_name_kor\":\"베노훼럼\",\"row_id\":50,\"value\":null}]",
    "cd_016": "[{\"column_id\":\"S\",\"label\":null,\"product_name_kor\":\"헴리브라\",\"row_id\":48,\"value\":\"B02D1FACTOR VIII INCL SUBSTIT\"},{\"column_id\":\"S\",\"label\":null,\"product_name_kor\":\"헴리브라\",\"row_id\":49,\"value\":\"B02D3HAEMOPHILIA ANTI-INH PRD\"},{\"column_id\":\"S\",\"label\":null,\"product_name_kor\":\"헴리브라\",\"row_id\":50,\"value\":null}]",
    "cd_017": "[{\"column_id\":\"T\",\"label\":null,\"product_name_kor\":\"엔커버\",\"row_id\":48,\"value\":\"V06D0OTHER GENERAL NUTRIENTS\"},{\"column_id\":\"T\",\"label\":null,\"product_name_kor\":\"엔커버\",\"row_id\":49,\"value\":null},{\"column_id\":\"T\",\"label\":null,\"product_name_kor\":\"엔커버\",\"row_id\":50,\"value\":null}]",
    "cd_018": "[{\"column_id\":\"U\",\"label\":null,\"product_name_kor\":\"위너프/A+\",\"row_id\":48,\"value\":\"K01DFAT EMULSIONS,INCL TPN - 3CB & NHI \"},{\"column_id\":\"U\",\"label\":null,\"product_name_kor\":\"위너프/A+\",\"row_id\":49,\"value\":\"K01DFAT EMULSIONS,INCL TPN - 3CB & NHI \"},{\"column_id\":\"U\",\"label\":null,\"product_name_kor\":\"위너프/A+\",\"row_id\":50,\"value\":null}]",
    "cd_019": "[{\"column_id\":\"V\",\"label\":null,\"product_name_kor\":\"플라주오피\",\"row_id\":48,\"value\":\"K01A31/2-ELECTROLYTE SOLUTIONS - Acetated Balanced Crystalloid \"},{\"column_id\":\"V\",\"label\":null,\"product_name_kor\":\"플라주오피\",\"row_id\":49,\"value\":\"K01A11/1-ELECTROLYTE SOLUTIONS - Acetated Balanced Crystalloid \"},{\"column_id\":\"V\",\"label\":null,\"product_name_kor\":\"플라주오피\",\"row_id\":50,\"value\":null}]",
}

_CD_BUSINESS_SPECS: tuple[dict[str, Any], ...] = (
    {
        "competitive_dynamics_id": "cd_001",
        "strategic_market_id": "strategy_001",
        "product_name_kor": "라베칸/라베칸듀오",
        "col_in_master_excel": "C",
        "column_ids": (3,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "Rabeprazole 단일제 + Rabeprazole/제산제 FDC",
        "cd_filter_expression": "clean(molecule) == 'Rabeprazole'",
        "filter_kind": "molecule_rabeprazole",
    },
    {
        "competitive_dynamics_id": "cd_002",
        "strategic_market_id": "strategy_002",
        "product_name_kor": "제이클",
        "col_in_master_excel": "D",
        "column_ids": (4,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "NON_NHI",
        "cd_filter_expression": "clean(nhi_type) == 'NON-NHI'",
        "filter_kind": "nhi_non_nhi",
    },
    {
        "competitive_dynamics_id": "cd_003",
        "strategic_market_id": "strategy_003",
        "product_name_kor": "가드렛/가드메트",
        "col_in_master_excel": "E",
        "column_ids": (5,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "A10N3 + A10N1",
        "cd_filter_expression": "atc4_code contains A10N3 or A10N1",
        "filter_kind": "atc_a10n3_a10n1",
    },
    {
        "competitive_dynamics_id": "cd_004",
        "strategic_market_id": "strategy_004",
        "product_name_kor": "타발리스",
        "col_in_master_excel": "F",
        "column_ids": (6,),
        "cd_definition_type": "ml_equals_cd_exact",
        "cd_definition_brand_class": "default",
        "cd_filter_expression": "sheet 전체",
        "filter_kind": "sheet_all",
    },
    {
        "competitive_dynamics_id": "cd_005",
        "strategic_market_id": "strategy_005",
        "product_name_kor": "시그마트",
        "col_in_master_excel": "G",
        "column_ids": (7,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "C01D0 -> [C1D] only",
        "cd_filter_expression": "Q-29 option B: atc4_code contains [C1D] only",
        "filter_kind": "sigmart_c1d_only",
        "cd_filter_status": "confirmed_q29_b",
    },
    {
        "competitive_dynamics_id": "cd_006",
        "strategic_market_id": "strategy_006",
        "product_name_kor": "리바로/리바로젯",
        "col_in_master_excel": "H",
        "column_ids": (8,),
        "cd_definition_type": "ml_equals_cd_exact",
        "cd_definition_brand_class": "default",
        "cd_filter_expression": "sheet 전체",
        "filter_kind": "sheet_all",
    },
    {
        "competitive_dynamics_id": "cd_007",
        "strategic_market_id": "strategy_007",
        "product_name_kor": "리바로페노",
        "col_in_master_excel": "I",
        "column_ids": (9,),
        "cd_definition_type": "ml_equals_cd_exact",
        "cd_definition_brand_class": "default",
        "cd_filter_expression": "sheet 전체",
        "filter_kind": "sheet_all",
    },
    {
        "competitive_dynamics_id": "cd_008",
        "strategic_market_id": "strategy_008",
        "product_name_kor": "리바로하이",
        "col_in_master_excel": "J",
        "column_ids": (10,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "Statin/ARB/CCB",
        "cd_filter_expression": "corrected explicit lookup clean(class_2) == 'Statin/ARB/CCB'",
        "filter_kind": "class2_statin_arb_ccb",
    },
    {
        "competitive_dynamics_id": "cd_009",
        "strategic_market_id": "strategy_008",
        "product_name_kor": "리바로브이",
        "col_in_master_excel": "K",
        "column_ids": (11,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "Statin/ARB",
        "cd_filter_expression": "corrected explicit lookup clean(class_2) == 'Statin/ARB'",
        "filter_kind": "class2_statin_arb",
    },
    {
        "competitive_dynamics_id": "cd_010",
        "strategic_market_id": "strategy_009",
        "product_name_kor": "트루패스",
        "col_in_master_excel": "L",
        "column_ids": (12,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "G4C2",
        "cd_filter_expression": "atc4_code contains G4C2",
        "filter_kind": "atc_g4c2",
    },
    {
        "competitive_dynamics_id": "cd_011",
        "strategic_market_id": "strategy_009",
        "product_name_kor": "피나스타/제이다트",
        "col_in_master_excel": "M",
        "column_ids": (13,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "G4C3",
        "cd_filter_expression": "atc4_code contains G4C3",
        "filter_kind": "atc_g4c3",
    },
    {
        "competitive_dynamics_id": "cd_012",
        "strategic_market_id": "strategy_010",
        "product_name_kor": "뉴트로진",
        "col_in_master_excel": "N",
        "column_ids": (14,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "L03A1",
        "cd_filter_expression": "clean(atc4_code) == 'L03A1'",
        "filter_kind": "atc_l03a1",
    },
    {
        "competitive_dynamics_id": "cd_013",
        "strategic_market_id": "strategy_010",
        "product_name_kor": "모빌리아",
        "col_in_master_excel": "O",
        "column_ids": (15,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "L03A9",
        "cd_filter_expression": "clean(atc4_code) == 'L03A9'",
        "filter_kind": "atc_l03a9",
    },
    {
        "competitive_dynamics_id": "cd_014",
        "strategic_market_id": "strategy_011",
        "product_name_kor": "악템라",
        "col_in_master_excel": "P",
        "column_ids": (16,),
        "cd_definition_type": "ml_equals_cd_by_empty",
        "cd_definition_brand_class": "default_sheet_all",
        "cd_filter_expression": "R48-R50 empty -> sheet 전체",
        "filter_kind": "sheet_all",
    },
    {
        "competitive_dynamics_id": "cd_015",
        "strategic_market_id": "strategy_012",
        "product_name_kor": "페린젝트 + 베노훼럼",
        "col_in_master_excel": "Q+R",
        "column_ids": (17, 18),
        "cd_definition_type": "collapse_pair",
        "cd_definition_brand_class": "IV Iron",
        "cd_filter_expression": "clean(atc4_code) == 'B03A1' and clean(dosage_form) == 'IV Iron'",
        "filter_kind": "b03a1_iv_iron",
    },
    {
        "competitive_dynamics_id": "cd_016",
        "strategic_market_id": "strategy_013",
        "product_name_kor": "헴리브라",
        "col_in_master_excel": "S",
        "column_ids": (19,),
        "cd_definition_type": "ml_equals_cd_exact",
        "cd_definition_brand_class": "default",
        "cd_filter_expression": "sheet 전체",
        "filter_kind": "sheet_all",
    },
    {
        "competitive_dynamics_id": "cd_017",
        "strategic_market_id": "strategy_015",
        "product_name_kor": "엔커버",
        "col_in_master_excel": "T",
        "column_ids": (20,),
        "cd_definition_type": "ml_equals_cd_exact",
        "cd_definition_brand_class": "default",
        "cd_filter_expression": "sheet 전체",
        "filter_kind": "sheet_all",
    },
    {
        "competitive_dynamics_id": "cd_018",
        "strategic_market_id": "strategy_014",
        "product_name_kor": "위너프/위너프A+",
        "col_in_master_excel": "U",
        "column_ids": (21,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "3CB & NHI & strength exists",
        "cd_filter_expression": "clean(class) == '3CB' and clean(nhi_type) == 'NHI' and clean(strength) is not null",
        "filter_kind": "winnerf_3cb_nhi_strength",
    },
    {
        "competitive_dynamics_id": "cd_019",
        "strategic_market_id": "strategy_016",
        "product_name_kor": "플라주오피",
        "col_in_master_excel": "V",
        "column_ids": (22,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "Acetated Balanced Crystalloid",
        "cd_filter_expression": "clean(atc4_code) in (K01A1,K01A3) and clean(class) == 'Acetated Balanced Crystalloid'",
        "filter_kind": "plajuopi_acetated",
    },
)


def _excel_column_name(column_id: int) -> str:
    name = ""
    index = column_id
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _product_identity_parts(value: Any) -> tuple[str, ...]:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = text.replace("에이플러스", "a+")
    text = re.sub(r"\s+\+\s+", "/", text)
    return tuple(
        compact
        for token in re.split(r"[\s/,·&]+", text)
        if (compact := re.sub(r"[^0-9a-z가-힣+]+", "", token))
    )


def _identity_differences(
    topology: dict[str, Any],
    business: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    differences: dict[str, dict[str, Any]] = {}
    expected_parent = str(business["strategic_market_id"])
    actual_parent = str(topology["strategic_market_id"])
    if expected_parent != actual_parent:
        differences["strategic_market_id"] = {
            "expected": expected_parent,
            "actual": actual_parent,
        }

    expected_columns = tuple(int(value) for value in business["column_ids"])
    actual_columns = tuple(int(value) for value in topology["column_ids"])
    if expected_columns != actual_columns:
        differences["column_ids"] = {
            "expected": expected_columns,
            "actual": actual_columns,
        }

    expected_product = str(business["product_name_kor"])
    actual_product = str(topology["name"])
    if _product_identity_parts(expected_product) != _product_identity_parts(
        actual_product
    ):
        differences["product_name_kor"] = {
            "expected": expected_product,
            "actual": actual_product,
        }
    return differences


def build_cd_specs(
    registry: MiMasterRegistry | None = None,
    *,
    business_specs: tuple[dict[str, Any], ...] | None = None,
) -> tuple[dict[str, Any], ...]:
    active_registry = registry or default_mi_master_registry()
    active_business_specs = (
        _CD_BUSINESS_SPECS if business_specs is None else business_specs
    )
    business_by_id = {
        str(spec["competitive_dynamics_id"]): spec
        for spec in active_business_specs
    }
    specs: list[dict[str, Any]] = []
    for topology in active_registry.cd_specs:
        cd_id = str(topology["cd_id"])
        column_ids = tuple(int(value) for value in topology["column_ids"])
        business = business_by_id.get(cd_id)
        if business is None:
            raise ValueError(
                "CD business spec rejected: "
                f"cd_id={cd_id!r}, reason=missing_explicit_spec, actual_identity={{"
                f"'strategic_market_id': {topology['strategic_market_id']!r}, "
                f"'column_ids': {column_ids!r}, "
                f"'product_name_kor': {topology['name']!r}}}"
            )
        differences = _identity_differences(topology, business)
        if differences:
            raise ValueError(
                "CD business spec identity_mismatch: "
                f"cd_id={cd_id!r}, differences={differences!r}"
            )
        specs.append(
            {
                **business,
                "competitive_dynamics_id": cd_id,
                "strategic_market_id": str(topology["strategic_market_id"]),
                "product_name_kor": business["product_name_kor"],
                "col_in_master_excel": "+".join(
                    _excel_column_name(column_id)
                    for column_id in column_ids
                ),
                "column_ids": column_ids,
            }
        )
    return tuple(specs)


CD_SPECS: tuple[dict[str, Any], ...] = build_cd_specs()
