"""R24 STAGE 2 — one lane must not spend the whole prompt, and the display cap must
govern both surfaces it claims to govern.

The live turn these tests are built from: "당뇨병 환자수 알려줘" returned 1,004 clinical
trials, the synthesis prompt reached 8,569,677 chars, the global bound replaced every
record with an identifier list, and the answer said the trials "were not confirmed in
the provided evidence" while the source block underneath it listed all 1,004 links.
"""

from __future__ import annotations

import json

import pytest

from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    EvidenceRecord,
    EvidenceSet,
    SourceReference,
)
from jw_chat_agent_poc.service.v4.synthesis_policy import (
    bound_sources_fairly,
    bound_synthesis_messages,
    limit_evidence_sets_for_render,
)


def _packet(source: str, records: int, blob: int) -> dict:
    return {
        "source": source,
        "query": f"{source} query",
        "evidence": {
            "records": [
                {"id": f"{source}-{i}", "text": "x" * 40} for i in range(records)
            ]
        },
        "detail": {
            "items": [{"ref": f"{source}-{i}", "blob": "y" * blob} for i in range(records)]
        },
    }


def _prompt(ct_records: int = 1004) -> dict:
    return {
        "external_evidence": [
            _packet("clinicaltrials", ct_records, 8000),
            _packet("hira", 10, 50),
            _packet("patent", 9, 100),
            _packet("web", 6, 200),
        ],
        "internal_datamart": [_packet("mart", 4, 80)],
        "user_question": "당뇨병 환자수 알려줘",
    }


def _messages(prompt: dict) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "contract"},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ]


def _lanes(content: str) -> dict[str, dict]:
    body = json.loads(content)
    return {
        packet["source"]: packet
        for key in ("external_evidence", "internal_datamart")
        for packet in body.get(key, [])
    }


# --------------------------------------------------------------------------- ⑸


def test_oversized_lane_does_not_strip_the_other_lanes():
    messages = _messages(_prompt())
    bounded, trace = bound_synthesis_messages(messages, char_limit=120_000)

    assert trace["strategy"] == "per_source_fair_share"
    assert trace["after_chars"] <= 120_000
    lanes = _lanes(bounded[1]["content"])
    # every lane still present, and the small ones untouched
    assert set(lanes) == {"clinicaltrials", "hira", "patent", "web", "mart"}
    for source in ("hira", "patent", "web", "mart"):
        assert "omitted" not in lanes[source]["detail"], source
        assert lanes[source]["evidence"]["records"], source
    assert trace["fair_share"]["sources_trimmed"] == ["clinicaltrials"]


def test_the_identifier_manifest_no_longer_fires_on_the_live_shape():
    """Before this change the same input produced bounded_identifier_manifest,
    which is the state in which synthesis sees IDs and no content at all."""
    _bounded, trace = bound_synthesis_messages(_messages(_prompt()), char_limit=120_000)
    assert trace["strategy"] not in {"identifier_manifest", "bounded_identifier_manifest"}


def test_small_prompts_are_left_exactly_as_they_were():
    messages = _messages(
        {"external_evidence": [_packet("hira", 5, 20)], "user_question": "q"}
    )
    bounded, trace = bound_synthesis_messages(messages, char_limit=120_000)
    assert trace["applied"] is False
    assert trace["strategy"] == "none"
    assert bounded == messages


def test_trimming_reports_what_it_withheld():
    _value, trace = bound_sources_fairly(_prompt(), char_limit=120_000)
    assert trace["applied"] is True
    detail = trace["detail"]["clinicaltrials"][0]
    assert detail["before_chars"] > detail["after_chars"]
    assert detail["detail_chars_omitted"] > 0


def test_a_lane_under_its_share_keeps_everything_when_another_is_huge():
    value, _trace = bound_sources_fairly(_prompt(), char_limit=120_000)
    lanes = {
        packet["source"]: packet
        for key in ("external_evidence", "internal_datamart")
        for packet in value[key]
    }
    assert len(lanes["hira"]["evidence"]["records"]) == 10
    assert len(lanes["hira"]["detail"]["items"]) == 10


def test_bounding_is_stable_for_the_same_input():
    first, _ = bound_synthesis_messages(_messages(_prompt()), char_limit=120_000)
    second, _ = bound_synthesis_messages(_messages(_prompt()), char_limit=120_000)
    assert first == second


# --------------------------------------------------------------------------- ⑷


def _evidence_set(source: str, records: int) -> EvidenceSet:
    built = [
        EvidenceRecord(
            evidence_id=f"{source}:{i}",
            source=source,
            result_kind="study",
            payload={"i": i},
            source_refs=(
                SourceReference(url=f"https://example.test/{source}/{i}", title=f"{source} {i}"),
            ),
        )
        for i in range(records)
    ]
    call_ref = SourceReference(url=f"https://example.test/{source}/call", title="call")
    # The live shape: result_refs() emits one ref per citation, so every record's
    # url is already in source_refs before the per-record refs are merged in. A
    # fixture that only carried per-record refs would let a broken rule pass.
    citation_refs = tuple(
        SourceReference(url=ref.url) for record in built for ref in record.source_refs
    )
    return EvidenceSet(
        source=source,
        query_spec=(f"{source} q",),
        retrieved_at="2026-08-17T00:00:00+00:00",
        coverage=CoverageLedger(records_received=records, records_unique=records),
        records=tuple(built),
        source_refs=(
            call_ref,
            *citation_refs,
            *(ref for record in built for ref in record.source_refs),
        ),
    )


def test_the_display_cap_governs_links_as_well_as_records():
    limited, trace = limit_evidence_sets_for_render(
        [_evidence_set("clinicaltrials", 1004)], per_source_limit=40
    )
    evidence = limited[0]
    assert len(evidence.records) == 40
    # The call-level ref survives; every url a withheld record claims goes with it,
    # including the duplicate the citation list contributed. That duplicate is why
    # the first attempt at this rule left all 1,004 links in place.
    assert {ref.url for ref in evidence.source_refs} == {
        "https://example.test/clinicaltrials/call",
        *(f"https://example.test/clinicaltrials/{i}" for i in range(40)),
    }
    assert trace["sources"]["clinicaltrials"]["shown"] == 40
    assert trace["sources"]["clinicaltrials"]["total"] == 1004
    assert trace["sources"]["clinicaltrials"]["refs_total"] == 2009
    assert trace["sources"]["clinicaltrials"]["refs_shown"] == 81  # call + 40 x2


def test_the_selection_rule_is_named_and_not_claimed_to_be_ranked():
    _limited, trace = limit_evidence_sets_for_render(
        [_evidence_set("clinicaltrials", 1004)], per_source_limit=40
    )
    assert trace["selection_rule"] == "leading_records_in_upstream_order"
    assert trace["selection_is_ranked"] is False


def test_a_set_within_the_cap_keeps_every_ref():
    limited, trace = limit_evidence_sets_for_render(
        [_evidence_set("hira", 10)], per_source_limit=40
    )
    assert trace["applied"] is False
    # untouched: call ref + 10 citation refs + 10 record refs
    assert len(limited[0].source_refs) == 21
    assert len(limited[0].records) == 10


@pytest.mark.parametrize("limit", [0, -1])
def test_a_nonsense_cap_is_refused_rather_than_silently_ignored(limit):
    with pytest.raises(ValueError):
        limit_evidence_sets_for_render([_evidence_set("hira", 3)], per_source_limit=limit)


# ------------------------------------------------------- ⑷ the third surface


def _result_with_citations(source: str, n: int):
    from datetime import datetime, timezone

    from jw_chat_agent_poc.service.v4.contracts import Citation, SourceResult

    return SourceResult(
        source=source,
        query=f"{source} q",
        status="ok",
        payload={},
        citations=tuple(
            Citation(
                source=source,
                query=f"{source} q",
                url=f"https://clinicaltrials.gov/study/NCT{i:08d}",
                retrieved_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
            )
            for i in range(n)
        ),
    )


def test_source_mapping_uses_the_same_cap_as_the_surface():
    """The prompt's url list is the third surface that used to have its own rule.

    Uncapped, it handed the model all 1,004 urls and the model wrote them out;
    one live answer was 93% link list under a notice that said "40 표시".
    """
    from jw_chat_agent_poc.service.v4.synthesizer import _bounded_source_mapping

    mapping, omitted = _bounded_source_mapping([_result_with_citations("clinicaltrials", 1004)])
    assert len(mapping) == 40
    assert omitted == {"ClinicalTrials.gov": 964}


def test_source_mapping_leaves_small_lanes_whole():
    from jw_chat_agent_poc.service.v4.synthesizer import _bounded_source_mapping

    mapping, omitted = _bounded_source_mapping([_result_with_citations("clinicaltrials", 9)])
    assert len(mapping) == 9
    assert omitted == {}
