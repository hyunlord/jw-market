from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.scripts.analysis.brand_activity.alias.builder import (  # noqa: E402
    KorEvidence,
    SourceObservation,
    build_alias_records,
)
from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en  # noqa: E402


def test_only_pl_approved_spelling_variants_are_merged() -> None:
    assert normalize_iqvia_en("A-PITO") == "APITO"
    assert normalize_iqvia_en("APITO") == "APITO"
    assert normalize_iqvia_en("LOWOSMOPERI") == "LOW OSMO PERI"
    assert normalize_iqvia_en("LOW OSMO PERI") == "LOW OSMO PERI"

    assert normalize_iqvia_en("TENELA") != normalize_iqvia_en("TENELIA")
    assert normalize_iqvia_en("NEUSTATIN") != normalize_iqvia_en("NEUSTATIN-A")
    assert normalize_iqvia_en("NEUSTATIN") != normalize_iqvia_en("NEUSTATIN-R")
    assert normalize_iqvia_en("DRUG M") != normalize_iqvia_en("DRUG XR")


def test_alias_builder_marks_sources_variants_and_uncovered_markets() -> None:
    observations = [
        SourceObservation("csd", "APITO", "", "LIVALO Market", "동아ST", None),
        SourceObservation("keyword", "A-PITO", "C10A1", "", "동아ST", None),
        SourceObservation("meeting", "LIVALOZET", "C10C0", "", "", None),
        SourceObservation("keyword", "CIBINQO", "L04B0", "", "한국화이자", None),
        SourceObservation("keyword", "TENELA", "A10N3", "", "한독", None),
        SourceObservation("csd", "TENELIA", "", "GUARDLET Market", "한독", None),
    ]
    kor_evidence = {
        "APITO": KorEvidence("아피토", "nsa_product_name_kor", "NSA fixture"),
        "LIVALOZET": KorEvidence("리바로젯", "nsa_product_name_kor", "NSA fixture"),
    }

    result = build_alias_records(observations, kor_evidence, {"리바로젯"})

    apito = result.by_anchor["APITO"]
    assert apito.variants == ("A-PITO", "APITO")
    assert apito.sources == {"csd": True, "keyword": True, "meeting": False}
    assert apito.mapping_status == "confirmed"

    livalozet = result.by_anchor["LIVALOZET"]
    assert livalozet.kr_canonical == "리바로젯"
    assert livalozet.is_jw is True

    cibinqo = result.by_anchor["CIBINQO"]
    assert cibinqo.csd_uncovered is True
    assert cibinqo.mapping_status == "pending"

    assert "TENELA" in result.by_anchor
    assert "TENELIA" in result.by_anchor
    assert result.by_anchor["TENELA"].iqvia_en != result.by_anchor["TENELIA"].iqvia_en
    assert result.stats.configured_variant_rule_count == 2
    assert result.stats.observed_multi_variant_rule_count == 1
