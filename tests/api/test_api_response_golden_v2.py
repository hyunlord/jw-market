from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pipeline.scripts.api.dynamic_market.cause_ranking import brand_ranking
from pipeline.scripts.api.dynamic_market.types import (
    AggregatedMetrics,
    BrandMetric,
    MarketDefinition,
)
from pipeline.scripts.api.models.dynamic_market import DynamicMarketRequest
from pipeline.scripts.api.routes import dynamic_market as dynamic_market_route


GOLDEN_PATH = Path(__file__).with_name("api_response_golden_v2.json")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _hemlibra_ranking_fixture() -> dict[str, object]:
    names_and_values = (
        ("헴리브라", 700.0),
        ("애드베이트", 600.0),
        ("애디노베이트", 500.0),
        ("그린모노", 400.0),
        ("진타솔로퓨즈", 300.0),
        ("노보세븐알티", 200.0),
        ("잔여브랜드", 100.0),
    )
    brands = tuple(
        BrandMetric(
            brand_key=name,
            brand_name=name,
            atc4_code="B02D1",
            total_value=value,
            market_share_pct=value / 28.0,
            rank=rank,
            latest_period="2026-Q1",
            latest_value=value,
            history_by_period={"2026-Q1": value},
        )
        for rank, (name, value) in enumerate(names_and_values, start=1)
    )
    ranking = brand_ranking(brands, focus=brands[0])
    return {
        "top_brands": ranking["top_brands"],
        "rankings_by_year": ranking["rankings_by_year"],
    }


def capture_api_response_samples(
    monkeypatch,
    samples: list[dict[str, object]],
) -> dict[str, object]:
    class PassthroughCache:
        def get_or_build(self, _request: object, builder: object) -> object:
            return builder()

    class FakeResolver:
        def __init__(self, *, mart_db: str, bridge_db: str) -> None:
            assert mart_db
            assert bridge_db

        def resolve(
            self,
            *,
            atc4: tuple[str, ...],
            molecule: tuple[str, ...],
            source: str,
            measure: str,
            **_kwargs: object,
        ) -> MarketDefinition:
            return MarketDefinition(
                view="general",
                filter_echo={
                    "view": "general",
                    "atc4": list(atc4),
                    "molecule": list(molecule),
                    "source": source,
                    "measure": measure,
                },
                source=source,
                measure=measure,
                brands=(),
            )

    class FakeAggregator:
        def __init__(
            self,
            *,
            mart_db: str,
            strategic_dimension_db: str | None = None,
        ) -> None:
            assert mart_db
            assert strategic_dimension_db

        def aggregate(
            self,
            *,
            source: str,
            measure: str,
            **_kwargs: object,
        ) -> AggregatedMetrics:
            market_size = 2.0 if source == "iqvia_nsa" else 1.0
            return AggregatedMetrics(
                source=source,
                measure=measure,
                unit_label="KRW" if measure == "sales" else "Unit",
                market_size=market_size,
                hhi=None,
                cagr=None,
                monthly_series=(
                    {"period": "2026-Q1" if source == "iqvia_nsa" else "2026-05", "market_size": market_size},
                ),
                brands=(),
            )

    def fake_strategic_payload(**kwargs: object) -> dict[str, object]:
        brand = str(kwargs["focus_brand_key"])
        data: dict[str, object] = {
            "kpi": {"target_brand_sales": 1.0},
            "selected_brand": brand,
        }
        if brand == "헴리브라":
            data["brand_ranking_stacked"] = _hemlibra_ranking_fixture()
        return {"data": data}

    monkeypatch.setattr(
        dynamic_market_route,
        "_dynamic_response_cache",
        PassthroughCache(),
    )
    monkeypatch.setattr(dynamic_market_route, "GeneralViewResolver", FakeResolver)
    monkeypatch.setattr(dynamic_market_route, "MetricAggregator", FakeAggregator)
    monkeypatch.setattr(
        dynamic_market_route,
        "get_strategic_payload",
        fake_strategic_payload,
    )
    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.resolvers.db.fetch_all",
        lambda sql, *_args, **_kwargs: [
            {
                "market_id": (
                    "cd_001" if "catalog_cd_market" in str(sql) else "ml_001"
                )
            }
        ],
    )

    observed: dict[str, object] = {}
    for sample in samples:
        request = DynamicMarketRequest.model_validate(sample["request"])
        response = dynamic_market_route.dynamic_market(request)
        observed[str(sample["name"])] = response
    return observed


def test_api_response_golden_v2_is_separate_and_covers_required_markets(
    monkeypatch,
) -> None:
    contract = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    samples = contract["samples"]

    assert contract["contract"] == "api_response_golden_v2"
    assert contract["fixture_scope"] == "dynamic-market route with deterministic resolver, aggregator, and strategic payload fixtures"
    assert len(samples) >= 12
    assert {"제이클", "가드렛"}.issubset(
        {sample["request"]["filters"].get("focus_brand_key") for sample in samples}
    )
    assert len(
        {
            sample["request"]["filters"].get("focus_brand_key")
            or tuple(sample["request"]["filters"].get("atc4", []))
            for sample in samples
        }
    ) >= 10
    assert {"general", "market_landscape", "competitive_dynamics"}.issubset(
        {
            sample["request"]["filters"].get("view_kind", "general")
            for sample in samples
        }
    )
    assert all("market_id" not in _canonical_bytes(sample["request"]).decode("utf-8") for sample in samples)

    observed = capture_api_response_samples(monkeypatch, samples)
    for sample in samples:
        name = sample["name"]
        expected_response = sample["canonical_response"]
        assert observed[name] == expected_response
        assert hashlib.sha256(_canonical_bytes(expected_response)).hexdigest() == sample["canonical_sha256"]


def test_api_response_golden_v2_locks_hemlibra_selected_plus_five_contract() -> None:
    contract = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    hemlibra = next(
        sample
        for sample in contract["samples"]
        if sample["name"] == "cd_hemlibra"
    )
    ranking = hemlibra["canonical_response"]["result"]["data"]["brand_ranking_stacked"]

    assert ranking["top_brands"] == [
        "헴리브라",
        "애드베이트",
        "애디노베이트",
        "그린모노",
        "진타솔로퓨즈",
        "노보세븐알티",
        "기타",
    ]
    rows = ranking["rankings_by_year"]["2026"]
    visible = [row for row in rows if not row["is_others"]]
    assert len(visible) == 6
    assert visible[0]["brand"] == "헴리브라"
    assert visible[0]["rank"] == 1
    assert len({row["brand"] for row in visible}) == 6
    assert abs(sum(row["ms_pct"] for row in rows) - 100.0) < 1e-9
