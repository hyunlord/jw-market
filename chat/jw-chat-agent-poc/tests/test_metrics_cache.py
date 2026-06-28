from __future__ import annotations

from copy import deepcopy

from jw_chat_agent_poc import ChatAgent
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.metrics.cache_live import StaticCausePayloadReader, StaticMetricsCacheReader


BRAND_CARDS = {
    "brand_cards": [
        {
            "rank": 6,
            "total_brands_in_market": 516,
            "brand": "리바로",
            "market_id": "strategy_006",
            "market_name": "리바로/리바로젯",
            "front": {
                "value_recent": 8_493_234_217.11,
                "ms_recent_pct": 3.7634,
                "gr_mom_pct": -2.5026,
                "gr_qoq_pct": 62.2517,
                "gr_yoy_pct": 33.6494,
                "gr_yoy_mat_pct": 12.8795,
                "gr_yoy_ym_pct": 33.6494,
                "default_source": "UBIST",
            },
            "back": {"cagr_5y_pct": 12.0888},
            "back_extended": {
                "market_size_recent": 225_677_368_890.97986,
                "market_cagr_5y_pct": 16.18,
                "brand_cagr_5y_pct": 12.0888,
                "excess_growth_pct": -4.0912,
                "source_label": "UBIST",
                "market_label_kor": "고지혈증",
            },
        },
        {
            "rank": 3,
            "total_brands_in_market": 516,
            "brand": "리바로젯",
            "market_id": "strategy_006",
            "market_name": "리바로/리바로젯",
            "front": {
                "value_recent": 12_009_054_192.93,
                "ms_recent_pct": 5.3213,
                "default_source": "UBIST",
            },
            "back": {"cagr_5y_pct": 50.3084},
            "back_extended": {
                "market_size_recent": 225_677_368_890.97986,
                "market_cagr_5y_pct": 16.18,
                "brand_cagr_5y_pct": 50.3084,
                "excess_growth_pct": 34.1284,
                "source_label": "UBIST",
                "market_label_kor": "고지혈증",
            },
        },
        {
            "rank": 2,
            "total_brands_in_market": 51,
            "brand": "페린젝트",
            "market_id": "strategy_012",
            "market_name": "페린젝트/베노훼럼",
            "front": {
                "value_recent": 2_100_000_000.0,
                "ms_recent_pct": 12.5,
                "default_source": "UBIST",
            },
            "back": {"cagr_5y_pct": 8.1},
            "back_extended": {
                "market_size_recent": 16_800_000_000.0,
                "market_cagr_5y_pct": 3.4,
                "brand_cagr_5y_pct": 8.1,
                "excess_growth_pct": 4.7,
                "source_label": "UBIST",
                "market_label_kor": "철분제",
            },
        },
    ]
}


CACHE_BRANDS = [
    {"brand": "리바로", "market_id": "strategy_006", "sources": ["UBIST"], "rank": 1},
    {"brand": "리바로젯", "market_id": "strategy_006", "sources": ["UBIST"], "rank": 2},
    {"brand": "페린젝트", "market_id": "strategy_012", "market_name": "페린젝트/베노훼럼", "sources": ["UBIST"], "rank": 2},
]

CAUSE_PAYLOAD = {
    "data": {
        "kpi": {
            "target_brand": "리바로",
            "hhi_recent": 226.2664,
            "target_ei": 74.7168,
            "ei": 74.7168,
            "ei_basis": "endpoint_5y",
            "ei_period_years": 5,
            "target_momentum": -0.07062409584374052,
        },
        "sources_data": {
            "hhi_recent": 226.2664,
            "hhi_series_5y": [
                {"year": 2021, "period": "2021", "hhi": 288.0519},
                {"year": 2022, "period": "2022", "hhi": 252.4153},
                {"year": 2023, "period": "2023", "hhi": 240.2576},
                {"year": 2024, "period": "2024", "hhi": 233.0596},
                {"year": 2025, "period": "2025", "hhi": 226.2664},
            ],
            "market_size_series": {
                "2025-01": {"value": 100_000_000_000.0, "yoy_growth_pct": 8.1},
                "2025-12": {"value": 150_000_000_000.0, "yoy_growth_pct": 9.2},
                "2026-03": {"value": 228_838_670_570.0, "yoy_growth_pct": 25.59},
                "2026-04": {"value": 225_677_368_890.97986, "yoy_growth_pct": 36.88},
            },
        },
        "analysis_levels": {
            "data": {
                "Brand": {
                    "by_channel": {
                        "상급종병": [
                            {
                                "brand": "리바로",
                                "name": "리바로",
                                "value": 1_500_000_000.0,
                                "value_recent": 1_500_000_000.0,
                            }
                        ]
                    },
                    "ms_by_channel": {
                        "상급종병": [
                            {
                                "brand": "리바로",
                                "name": "리바로",
                                "ms_recent_pct": 10.5,
                            }
                        ]
                    },
                },
                "제형": {
                    "ms_segments": [
                        {"name": "정제", "value": 60.0, "ms_recent_pct": 60.0},
                        {"name": "복합제", "value": 40.0, "ms_recent_pct": 40.0},
                    ]
                },
            }
        },
        "level_top5_trend": {
            "by_level": {
                "Brand": {
                    "periods_10pt": ["2025-01", "2025-12", "2026-03", "2026-04"],
                    "values": [
                        {
                            "brands_in_value": [
                                {
                                    "brand": "리바로",
                                    "value_series_10pt": [7_000_000_000.0, 9_000_000_000.0, 8_711_248_139.54, 8_493_234_217.11],
                                    "ms_series_10pt": [7.0, 6.0, 3.8067, 3.7634],
                                    "rank_series_10pt": [5, 5, 6, 6],
                                    "value_recent": 8_493_234_217.11,
                                    "ms_recent_pct": 3.7634,
                                    "rank": 6,
                                }
                            ]
                        }
                    ],
                }
            }
        },
    }
}

IQVIA_CAUSE_PAYLOAD = {
    "data": {
        **CAUSE_PAYLOAD["data"],
        "sources_data": {
            **CAUSE_PAYLOAD["data"]["sources_data"],
            "market_size_series": {
                "2026-04": {"value": 333_000_000_000.0, "yoy_growth_pct": 11.1},
            },
        },
        "level_top5_trend": {
            "by_level": {
                "Brand": {
                    "periods_10pt": ["2026-04"],
                    "values": [
                        {
                            "brands_in_value": [
                                {
                                    "brand": "리바로",
                                    "value_series_10pt": [11_000_000_000.0],
                                    "ms_series_10pt": [3.3033],
                                    "rank_series_10pt": [4],
                                    "value_recent": 11_000_000_000.0,
                                    "ms_recent_pct": 3.3033,
                                    "rank": 4,
                                }
                            ]
                        }
                    ],
                }
            }
        },
    }
}


CAUSE_READER = StaticCausePayloadReader(
    {
        ("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): CAUSE_PAYLOAD,
        ("리바로", "market_landscape", "IQVIA", "sales", "strategy_006"): IQVIA_CAUSE_PAYLOAD,
    }
)


def cause_payload_with_top_brand_trends() -> dict:
    payload = deepcopy(CAUSE_PAYLOAD)
    rows = payload["data"]["level_top5_trend"]["by_level"]["Brand"]["values"][0]["brands_in_value"]
    rows.extend(
        [
            {
                "brand": "로수젯",
                "value_series_10pt": [18_000_000_000.0, 19_500_000_000.0, 20_100_000_000.0, 20_685_385_934.33],
                "ms_series_10pt": [8.2, 8.7, 9.0, 9.1659],
                "rank_series_10pt": [1, 1, 1, 1],
                "value_recent": 20_685_385_934.33,
                "ms_recent_pct": 9.1659,
                "rank": 1,
            },
            {
                "brand": "리피토",
                "value_series_10pt": [15_100_000_000.0, 14_900_000_000.0, 14_500_000_000.0, 14_421_756_866.72],
                "ms_series_10pt": [6.9, 6.6, 6.4, 6.3904],
                "rank_series_10pt": [2, 2, 2, 2],
                "value_recent": 14_421_756_866.72,
                "ms_recent_pct": 6.3904,
                "rank": 2,
            },
            {
                "brand": "아토젯",
                "value_series_10pt": [10_700_000_000.0, 11_000_000_000.0, 11_300_000_000.0, 11_648_132_500.0],
                "ms_series_10pt": [4.8, 4.95, 5.05, 5.162],
                "rank_series_10pt": [4, 4, 4, 4],
                "value_recent": 11_648_132_500.0,
                "ms_recent_pct": 5.162,
                "rank": 4,
            },
        ]
    )
    return payload


def test_metrics_tool_reads_latest_brand_card_values() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader)

    result = tool.get_brand_metric("리바로", metric="sales")

    assert result["render_data"]["market_id"] == "strategy_006"
    assert result["render_data"]["period"] == "2026-04"
    assert result["render_data"]["sales_krw"] == 8_493_234_217.11
    assert result["render_data"]["ms_recent_pct"] == 3.7634
    assert result["render_data"]["rank"] == 6
    assert result["render_data"]["total_brands_in_market"] == 516
    assert result["render_data"]["market_size_recent_krw"] == 225_677_368_890.97986
    assert result["render_data"]["brand_cagr_5y_pct"] == 12.0888
    assert result["render_data"]["market_cagr_5y_pct"] == 16.18


def test_chat_agent_routes_sales_question_to_cache_metrics() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader)

    result = ChatAgent(metrics=tool).answer("리바로 매출 알려줘")

    assert "cache" in result["sources"]
    assert any(call["tool"] == "get_brand_metric" for call in result["tool_calls"])
    assert "84.93억원" in result["answer"]


def test_chat_agent_filters_sales_to_previous_year_from_cause_payload() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=CAUSE_READER)

    result = ChatAgent(metrics=tool).answer("리바로 작년 매출")
    brand_call = next(call for call in result["tool_calls"] if call["tool"] == "get_brand_metric")
    data = brand_call["render_data"]

    assert data["period"] == "2025"
    assert data["sales_krw"] == 16_000_000_000.0
    assert data["market_size_filtered_krw"] == 250_000_000_000.0
    assert data["ms_recent_pct"] == 6.4
    assert data["applied_filters"]["period_year"] == 2025
    assert "2025" in result["answer"]


def test_chat_agent_filters_metrics_to_channel_market_share() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=CAUSE_READER)

    result = ChatAgent(metrics=tool).answer("리바로 상급종병 M/S")
    brand_call = next(call for call in result["tool_calls"] if call["tool"] == "get_brand_metric")
    data = brand_call["render_data"]

    assert data["channel"] == "상급종병"
    assert data["sales_krw"] == 1_500_000_000.0
    assert data["ms_recent_pct"] == 10.5
    assert data["applied_filters"]["channel"] == "상급종병"


def test_chat_agent_filters_metrics_to_analysis_level_segments() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=CAUSE_READER)

    result = ChatAgent(metrics=tool).answer("리바로 제형별 점유율")
    brand_call = next(call for call in result["tool_calls"] if call["tool"] == "get_brand_metric")
    data = brand_call["render_data"]

    assert data["level"] == "제형"
    assert [item["name"] for item in data["level_segments"]] == ["정제", "복합제"]
    assert data["applied_filters"]["level"] == "제형"
    assert "제형" in result["answer"]
    assert "분석 기준" in result["answer"]
    assert "level" not in result["answer"]


def test_brand_level_segments_read_live_share_and_sales_keys() -> None:
    payload = deepcopy(CAUSE_PAYLOAD)
    payload["data"]["analysis_levels"]["data"]["Brand"]["ms_segments"] = [
        {"name": "전체", "rank": 0, "recent_share_pct": 100.0, "value_recent": 225_677_368_890.98, "is_overall": True},
        {"name": "로수젯", "rank": 1, "recent_share_pct": 9.1659, "value_recent": 20_685_385_934.33},
        {"name": "리피토", "rank": 2, "recent_share_pct": 6.3904, "value_recent": 14_421_756_866.72},
        {"name": "리바로젯", "rank": 3, "recent_share_pct": 5.3213, "value_recent": 12_009_054_192.93},
    ]
    cause_reader = StaticCausePayloadReader(
        {("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): payload}
    )
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=cause_reader)

    result = tool.get_brand_metric("리바로", metric="market_share", filter_entries=(("level", "Brand"),))
    segments = result["render_data"]["level_segments"]

    assert [item["name"] for item in segments] == ["로수젯", "리피토", "리바로젯"]
    assert segments[0]["rank"] == 1
    assert isinstance(segments[0]["rank"], int)
    assert segments[0]["ms_recent_pct"] == 9.1659
    assert segments[0]["value"] == 20_685_385_934.33


def test_chat_agent_filters_metrics_source_to_iqvia_payload() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=CAUSE_READER)

    result = ChatAgent(metrics=tool).answer("리바로 매출 IQVIA 기준")
    brand_call = next(call for call in result["tool_calls"] if call["tool"] == "get_brand_metric")
    data = brand_call["render_data"]

    assert data["source_label"] == "IQVIA"
    assert data["sales_krw"] == 11_000_000_000.0
    assert data["market_size_filtered_krw"] == 333_000_000_000.0
    assert data["applied_filters"]["source"] == "IQVIA"


def test_chat_agent_routes_hospital_level_sales_to_channel_query() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=CAUSE_READER)

    result = ChatAgent(metrics=tool).answer("리바로 병원별 매출")
    brand_call = next(call for call in result["tool_calls"] if call["tool"] == "query_failed")
    data = brand_call["render_data"]

    assert data["status"] == "query_failed"
    assert "조회 실행이 실패" in data["message"]
    assert data["error_type"] == "LookupError"
    assert '"dimensions": ["channel"]' in data["arguments"]["spec"]
    assert '"metrics": ["sales"]' in data["arguments"]["spec"]
    assert "granularity" not in result["answer"]


def test_chat_agent_reports_same_market_scope_as_unsupported() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=CAUSE_READER)

    result = ChatAgent(metrics=tool).answer("리바로랑 같은 시장 작년 매출")
    brand_call = next(call for call in result["tool_calls"] if call["tool"] == "unsupported_metric")
    data = brand_call["render_data"]

    assert data["status"] == "unsupported"
    assert data["unsupported_filters"][0]["field"] == "market_scope"
    assert data["unsupported_filters"][0]["value"] == "같은 시장"
    assert "160.00억원" not in result["answer"]
    assert "시장 범위" in result["answer"]
    assert "market_scope" not in result["answer"]


def test_chat_agent_recognizes_cache_brand_outside_fixture() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    resolver = BrandResolver(mode="cache", brand_reader=reader)
    tool = MetricsTool(mode="cache", cache_reader=reader)

    result = ChatAgent(metrics=tool, resolver=resolver).answer("페린젝트 매출 알려줘")

    assert result["resolution"]["canonical_brand"] == "페린젝트"
    assert result["resolution"]["market_id"] == "strategy_012"
    assert result["resolution"]["molecule_en"] == ("ferric carboxymaltose",)
    brand_call = next(call for call in result["tool_calls"] if call["tool"] == "get_brand_metric")
    assert brand_call["render_data"]["sales_krw"] == 2_100_000_000.0


def test_chat_agent_keeps_sidecar_combo_decomposition_with_cache_brand_source() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    resolver = BrandResolver(mode="cache", brand_reader=reader)

    result = ChatAgent(metrics=MetricsTool(mode="cache", cache_reader=reader), resolver=resolver).answer("리바로젯 FDA 라벨·특허?")

    assert result["resolution"]["is_combo"] is True
    assert result["resolution"]["molecule_en"] == ("ezetimibe", "pitavastatin")
    tools = [call.get("tool") for call in result["tool_calls"]]
    assert tools.count("openfda_label_search") == 2
    assert tools.count("mfds_patent") == 2


def test_chat_agent_returns_graceful_unsupported_for_non_canonical_brand() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    resolver = BrandResolver(mode="cache", brand_reader=reader)
    tool = MetricsTool(mode="cache", cache_reader=reader)

    result = ChatAgent(metrics=tool, resolver=resolver).answer("타이레놀 매출 알려줘")

    assert result["sources"] == ["unsupported_brand"]
    assert result["tool_calls"] == []
    assert "지원하지 않는 브랜드" in result["answer"]


def test_chat_agent_uses_sidecar_molecule_for_new_cache_brand_external_api() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    resolver = BrandResolver(mode="cache", brand_reader=reader)

    result = ChatAgent(metrics=MetricsTool(mode="cache", cache_reader=reader), resolver=resolver).answer("페린젝트 임상시험 찾아줘")

    assert result["resolution"]["canonical_brand"] == "페린젝트"
    assert result["resolution"]["molecule_en"] == ("ferric carboxymaltose",)
    assert any(call["tool"] == "clinicaltrials_v2_search" for call in result["tool_calls"])


def test_sidecar_has_molecule_for_all_canonical_25_brands() -> None:
    cache_brands = [
        {"brand": "라베칸", "market_id": "strategy_001"},
        {"brand": "라베칸듀오", "market_id": "strategy_001"},
        {"brand": "제이클", "market_id": "strategy_002"},
        {"brand": "가드렛", "market_id": "strategy_003"},
        {"brand": "가드메트", "market_id": "strategy_003"},
        {"brand": "타발리스", "market_id": "strategy_004"},
        {"brand": "시그마트", "market_id": "strategy_005"},
        {"brand": "리바로", "market_id": "strategy_006"},
        {"brand": "리바로젯", "market_id": "strategy_006"},
        {"brand": "리바로페노", "market_id": "strategy_007"},
        {"brand": "리바로하이", "market_id": "strategy_008"},
        {"brand": "리바로브이", "market_id": "strategy_008"},
        {"brand": "트루패스", "market_id": "strategy_009"},
        {"brand": "피나스타", "market_id": "strategy_009"},
        {"brand": "제이다트", "market_id": "strategy_009"},
        {"brand": "뉴트로진", "market_id": "strategy_010"},
        {"brand": "모빌리아", "market_id": "strategy_010"},
        {"brand": "악템라", "market_id": "strategy_011"},
        {"brand": "페린젝트", "market_id": "strategy_012"},
        {"brand": "베노훼럼", "market_id": "strategy_012"},
        {"brand": "헴리브라", "market_id": "strategy_013"},
        {"brand": "위너프", "market_id": "strategy_014"},
        {"brand": "위너프A+", "market_id": "strategy_014"},
        {"brand": "엔커버", "market_id": "strategy_015"},
        {"brand": "플라주오피", "market_id": "strategy_016"},
    ]
    reader = StaticMetricsCacheReader(cache_brands=cache_brands, market_status=BRAND_CARDS)
    resolver = BrandResolver(mode="cache", brand_reader=reader)

    missing = [brand["brand"] for brand in cache_brands if not resolver.resolve(brand["brand"], allow_default=False).molecule_en]

    assert resolver.supported_brand_count() == 25
    assert missing == []


def test_unsupported_hhi_series_is_graceful() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=CAUSE_READER)

    result = ChatAgent(metrics=tool).answer("리바로 HHI 추이")

    assert "cache" in result["sources"]
    assert result["tool_calls"][0]["tool"] == "get_brand_metric"
    assert result["tool_calls"][0]["render_data"]["hhi_recent"] == 226.2664
    assert result["tool_calls"][0]["render_data"]["hhi_series_5y"][-1]["hhi"] == 226.2664
    assert "226.27" in result["answer"]


def test_p2_3_metric_terms_route_to_graceful_cache_unsupported() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=CAUSE_READER)

    expectations = {
        "리바로 hhi": ("hhi", "hhi_recent", 226.2664),
        "리바로 월별 매출": ("series", "brand_value_series_10pt", 8_493_234_217.11),
        "리바로 Momentum": ("momentum", "momentum_score", -0.07062409584374052),
        "리바로 모멘텀": ("momentum", "momentum_score", -0.07062409584374052),
        "리바로 EI": ("ei", "ei", 74.7168),
    }
    for question, (metric, field, expected) in expectations.items():
        result = ChatAgent(metrics=tool).answer(question)

        assert "cache" in result["sources"]
        assert result["tool_calls"][0]["tool"] == "get_brand_metric"
        assert result["tool_calls"][0]["render_data"]["metric"] == metric
        if field == "brand_value_series_10pt":
            assert result["tool_calls"][0]["render_data"][field][-1]["value_krw"] == expected
        else:
            assert result["tool_calls"][0]["render_data"][field] == expected


def test_series_metric_exposes_top_brand_trends_from_level_top5_trend() -> None:
    cause_reader = StaticCausePayloadReader(
        {("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): cause_payload_with_top_brand_trends()}
    )
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=cause_reader)

    result = tool.get_brand_metric("리바로", metric="series")
    trend_rows = result["render_data"]["level_top5_trend_series"]

    assert [item["brand"] for item in trend_rows[:4]] == ["로수젯", "리피토", "아토젯", "리바로"]
    atozet = next(item for item in trend_rows if item["brand"] == "아토젯")
    assert atozet["series"][-1]["period"] == "2026-04"
    assert atozet["series"][-1]["ms_pct"] == 5.162
    assert atozet["series"][-1]["value_krw"] == 11_648_132_500.0
    assert atozet["from_period"] == atozet["series"][0]["period"]
    assert atozet["from_ms_pct"] == atozet["series"][0]["ms_pct"]
    assert atozet["to_period"] == atozet["series"][-1]["period"]
    assert atozet["to_ms_pct"] == atozet["series"][-1]["ms_pct"]
    assert atozet["share_delta_pctp"] == 0.362
