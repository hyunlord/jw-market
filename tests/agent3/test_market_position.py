from __future__ import annotations

from types import SimpleNamespace

from pipeline.scripts.agent3 import run_source as run_source_module
from pipeline.scripts.agent3.market_position import (
    FORBIDDEN_MARKET_WORDS,
    _stable_sum,
    build_market_position_fallback,
)
from pipeline.scripts.agent3.source_loader import compute_source_input_hash, make_source_record
from pipeline.scripts.agent3.strength_candidate_extractor import MarketMetricRow


def _row(
    brand_key: str,
    brand_name: str,
    source: str,
    atc4_code: str,
    atc4_desc: str,
    history: dict[str, float],
) -> MarketMetricRow:
    return MarketMetricRow(
        brand_key=brand_key,
        brand_name=brand_name,
        source=source,
        atc4_code=atc4_code,
        atc4_desc=atc4_desc,
        raw_value_history=history,
    )


def test_iqvia_market_position_matches_recovered_positive_template() -> None:
    base_summary = {
        "brand": "대상약",
        "source": "iqvia",
        "profile_display": {"brand": "대상약"},
        "strength_items": [],
        "limitations": ["candidate 0"],
        "candidate_count": 0,
    }
    market_rows = [
        _row(
            "target",
            "대상약",
            "iqvia_nsa",
            "C10A",
            "지질조절제",
            {"2026-Q1": 100_000_000.0, "2026-Q2": 200_000_000.0},
        ),
        _row(
            "competitor",
            "경쟁약",
            "iqvia_nsa",
            "C10A",
            "지질조절제",
            {"2026-Q1": 250_000_000.0, "2026-Q2": 300_000_000.0},
        ),
    ]

    result = build_market_position_fallback(
        brand_key="target",
        brand_name="대상약",
        source="iqvia",
        profile={"atc4_codes": ["C10A"]},
        base_summary=base_summary,
        market_rows=market_rows,
    )

    assert result.candidate == {
        "brand": "대상약",
        "source": "iqvia",
        "measure": "sales",
        "metric": "market_position",
        "slice": "전체 IQVIA / ATC4 C10A",
        "period": "2026-Q2",
        "rank": 2,
        "share_pct": 40.0,
        "market_brand_count": 2,
        "latest_value": 200_000_000.0,
        "observation_count": 2,
        "market_key": "iqvia:C10A",
        "cumulative_value": 300_000_000.0,
        "evidence": "market_scope.iqvia.C10A.2026-Q2",
        "low_base": False,
        "caveats": [],
    }
    assert result.narrative == (
        "IQVIA 지질조절제 시장 2개 브랜드 중 2위이며, 최신 2026-Q2 매출액은 "
        "2.0억원(점유율 40.0%)입니다. 최근 2분기 연속 실적이 확인됩니다."
    )
    assert result.summary == {
        **base_summary,
        "brand": "대상약",
        "candidate_count": 1,
        "source": "iqvia",
        "strength_items": [
            {
                "candidate_index": 0,
                "confidence": "high",
                "metric": "market_position",
                "narrative": result.narrative,
                "numbers": {
                    "rank": 2,
                    "share_pct": 40.0,
                    "market_brand_count": 2,
                    "latest_value": 200_000_000.0,
                    "observation_count": 2,
                    "cumulative_value": 300_000_000.0,
                },
                "period": "2026-Q2",
                "slice": "전체 IQVIA / ATC4 C10A",
            }
        ],
    }


def test_ubist_latest_zero_matches_recovered_template_and_alias() -> None:
    result = build_market_position_fallback(
        brand_key="target",
        brand_name="대상약",
        source="ubist",
        profile={"atc4_codes": ["A2B2"]},
        base_summary={"strength_items": [], "candidate_count": 0},
        market_rows=[
            _row(
                "target",
                "대상약",
                "ubist",
                "A2B2",
                "고지혈증 치료제",
                {"2026-01": 100_000_000.0, "2026-02": 0.0},
            ),
            _row(
                "competitor",
                "경쟁약",
                "ubist",
                "A02B2",
                "고지혈증 치료제",
                {"2026-01": 40_000_000.0, "2026-02": 50_000_000.0},
            ),
        ],
    )

    assert result.candidate["slice"] == "전체 UBIST / ATC4 A02B2"
    assert result.candidate["rank"] == 2
    assert result.candidate["share_pct"] == 0.0
    assert result.candidate["observation_count"] == 0
    assert result.candidate["market_key"] == "ubist:A02B2"
    assert result.candidate["low_base"] is True
    assert result.narrative == (
        "UBIST 고지혈증 치료제 시장에서 최신 2026-02 실적은 확인되지 않습니다. "
        "전체 관측기간 누적 처방액은 1.0억원입니다."
    )


def test_forbidden_market_description_is_replaced_by_atc4_scope() -> None:
    result = build_market_position_fallback(
        brand_key="target",
        brand_name="대상약",
        source="iqvia",
        profile={"atc4_codes": ["C10A"]},
        base_summary={},
        market_rows=[
            _row(
                "target",
                "대상약",
                "iqvia_nsa",
                "C10A",
                "성장 시장",
                {"2026-Q2": 100_000_000.0},
            )
        ],
    )

    assert result.narrative.startswith("IQVIA ATC4 C10A 시장")
    assert not any(word in result.narrative for word in FORBIDDEN_MARKET_WORDS)


def test_market_position_result_is_deterministic() -> None:
    kwargs = {
        "brand_key": "target",
        "brand_name": "대상약",
        "source": "iqvia",
        "profile": {"atc4_codes": ["C10A"]},
        "base_summary": {"strength_items": []},
        "market_rows": [
            _row(
                "target",
                "대상약",
                "iqvia_nsa",
                "C10A",
                "지질조절제",
                {"2026-Q2": 100_000_000.0},
            )
        ],
    }

    assert build_market_position_fallback(**kwargs) == build_market_position_fallback(**kwargs)


def test_stable_sum_is_independent_of_python_builtin_sum_version() -> None:
    assert _stable_sum([0.1] * 10) == 1.0


def test_record_can_store_fallback_candidate_without_hashing_it() -> None:
    profile = {"brand": "대상약", "atc4_codes": ["C10A"]}
    fallback = [{"brand": "대상약", "metric": "market_position"}]
    record = make_source_record(
        brand_key="target",
        source="iqvia",
        brand_name="대상약",
        serving_brand_name="대상약",
        profile=profile,
        candidates=fallback,
        hash_candidates=[],
        summary={"strength_items": [{"metric": "market_position"}]},
        workflow_id=316,
        workflow_rev=5692,
    )

    assert record.strength_candidates_json == fallback
    assert record.input_hash == compute_source_input_hash(profile, [], 5692, "iqvia")


def test_run_source_replaces_candidate_zero_with_market_position(monkeypatch, tmp_path) -> None:
    class FakeRepository:
        def __init__(self, _config) -> None:
            pass

        def load_brand_universe(self, _source):
            return ["target"]

        def resolve_brand_identities(self, _refs, _aliases):
            return [SimpleNamespace(brand_key="target", brand_name="대상약")]

        def load_general_rows_for_brands(self, _brand_keys):
            return {
                "target": [
                    {
                        "brand_key": "target",
                        "brand_name": "대상약",
                        "source": "iqvia_nsa",
                        "measure": "sales",
                        "atc4_code": "C10A",
                    }
                ]
            }

        def load_market_metric_rows(self, _general_rows):
            return [
                _row(
                    "target",
                    "대상약",
                    "iqvia_nsa",
                    "C10A",
                    "지질조절제",
                    {"2026-Q2": 100_000_000.0},
                )
            ]

        def load_strategic_rows_for_brands(self, _brand_keys):
            return {"target": []}

        def load_molecule_rows_for_brands(self, _brand_names):
            return {"대상약": []}

    class FakeLoader:
        def __init__(self, _config) -> None:
            pass

    monkeypatch.setattr(run_source_module.DbConfig, "from_env", lambda: object())
    monkeypatch.setattr(run_source_module, "Agent3Repository", FakeRepository)
    monkeypatch.setattr(run_source_module, "Agent3SourceLoader", FakeLoader)
    monkeypatch.setattr(run_source_module, "Agent3WorkflowClient", lambda **_kwargs: object())
    monkeypatch.setattr(
        run_source_module,
        "serving_brand_names_for_identities",
        lambda _identities: {"target": "대상약"},
    )
    monkeypatch.setattr(
        run_source_module,
        "build_source_profile",
        lambda **_kwargs: {"brand": "대상약", "atc4_codes": ["C10A"]},
    )
    monkeypatch.setattr(run_source_module, "extract_source_candidates", lambda **_kwargs: [])

    result = run_source_module.run_source(
        brand_source="general_all",
        mode="dry-run",
        source_selection="iqvia",
        explicit_brands=None,
        output=tmp_path / "result.json",
        top_n=5,
        workflow_rev=5692,
        expected_workflow_rev=5692,
        environment_mode=None,
    )

    assert result["profile_only"] == 0
    assert result["market_position"] == 1
    assert result["records"][0]["candidate_count"] == 1
    assert result["records"][0]["status"] == "market_position"


def test_run_source_replaces_validation_isolation_with_market_position(
    monkeypatch,
    tmp_path,
) -> None:
    captured_records = []

    class FakeRepository:
        def __init__(self, _config) -> None:
            pass

        def load_brand_universe(self, _source):
            return ["target"]

        def resolve_brand_identities(self, _refs, _aliases):
            return [SimpleNamespace(brand_key="target", brand_name="대상약")]

        def load_general_rows_for_brands(self, _brand_keys):
            return {
                "target": [
                    {
                        "brand_key": "target",
                        "brand_name": "대상약",
                        "source": "iqvia_nsa",
                        "measure": "sales",
                        "atc4_code": "C10A",
                    }
                ]
            }

        def load_market_metric_rows(self, _general_rows):
            return [
                _row(
                    "target",
                    "대상약",
                    "iqvia_nsa",
                    "C10A",
                    "지질조절제",
                    {"2026-Q2": 100_000_000.0},
                )
            ]

        def load_strategic_rows_for_brands(self, _brand_keys):
            return {"target": []}

        def load_molecule_rows_for_brands(self, _brand_names):
            return {"대상약": []}

    class FakeLoader:
        def __init__(self, _config) -> None:
            pass

        def ensure_table(self) -> None:
            pass

        def load_coverage(self):
            return 35_521, 24_789, 0

        def load_existing_hashes(self, _brand_keys):
            return {}

        def upsert_many(self, records, *, batch_size):
            assert batch_size == 200
            captured_records.extend(records)
            return len(records)

    primary_candidate = {"slice": "전체 IQVIA", "metric": "recent_growth"}
    monkeypatch.setattr(run_source_module.DbConfig, "from_env", lambda: object())
    monkeypatch.setattr(run_source_module, "Agent3Repository", FakeRepository)
    monkeypatch.setattr(run_source_module, "Agent3SourceLoader", FakeLoader)
    monkeypatch.setattr(run_source_module, "Agent3WorkflowClient", lambda **_kwargs: object())
    monkeypatch.setattr(
        run_source_module,
        "serving_brand_names_for_identities",
        lambda _identities: {"target": "대상약"},
    )
    monkeypatch.setattr(
        run_source_module,
        "build_source_profile",
        lambda **_kwargs: {"brand": "대상약", "atc4_codes": ["C10A"]},
    )
    monkeypatch.setattr(
        run_source_module,
        "extract_source_candidates",
        lambda **_kwargs: [primary_candidate],
    )
    monkeypatch.setattr(
        run_source_module,
        "_run_workflow_with_validation",
        lambda **_kwargs: SimpleNamespace(
            summary={
                "brand": "대상약",
                "strength_items": [],
                "unavailable_reason": "validation_failed",
            },
            status="validation_isolated",
            workflow_calls=2,
        ),
    )

    result = run_source_module.run_source(
        brand_source="general_all",
        mode="full",
        source_selection="iqvia",
        explicit_brands=None,
        output=tmp_path / "result.json",
        top_n=5,
        workflow_rev=5692,
        expected_workflow_rev=5692,
        environment_mode=None,
    )

    assert result["affected"] == 1
    assert result["market_position"] == 1
    assert result["records"][0]["status"] == "validation_isolated_market_position"
    assert captured_records[0].strength_candidates_json[0]["metric"] == "market_position"
    assert captured_records[0].strength_summary_json["strength_items"][0]["metric"] == "market_position"
    assert captured_records[0].input_hash == compute_source_input_hash(
        {"brand": "대상약", "atc4_codes": ["C10A"]},
        [primary_candidate],
        5692,
        "iqvia",
    )
