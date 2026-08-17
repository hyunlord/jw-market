from __future__ import annotations

from collections.abc import Iterable


AUTOMATIC_FACT_NOTICES = {
    "hira": "HIRA 환자수는 주상병 기준 청구 실인원이며 유병률과 다릅니다.",
    "openfda": "FAERS/OpenFDA는 자발적 보고 자료로 인과관계나 발생률 산출에 쓸 수 없습니다.",
    "clinicaltrials": "ClinicalTrials.gov 모집상태는 갱신이 지연될 수 있습니다.",
    "patent": "특허 존속기간 만료가 곧 제네릭 진입 시점을 뜻하지 않습니다.",
}


def append_automatic_fact_notices(answer: str, sources: Iterable[str]) -> str:
    source_names = tuple(sources)
    notes = tuple(
        dict.fromkeys(
            AUTOMATIC_FACT_NOTICES[source]
            for source in source_names
            if source in AUTOMATIC_FACT_NOTICES
        )
    )
    missing = tuple(note for note in notes if note not in answer)
    if "hira" in source_names:
        hira_rate_limit = (
            "[확인 한계] 이 자료는 주상병 기준 청구 실인원이며, 인구 분모가 없어 "
            "성별·연령별 발생 위험이나 유병률을 판단하지 않습니다."
        )
        if hira_rate_limit not in answer:
            missing = (*missing, hira_rate_limit)
    if not missing:
        return answer
    return f"{answer.rstrip()}\n\n" + "\n".join(f"- {note}" for note in missing)
