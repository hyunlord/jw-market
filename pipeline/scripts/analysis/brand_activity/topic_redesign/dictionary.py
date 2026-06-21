"""Market-specific provisional dictionary generation and coverage matching."""

from __future__ import annotations

from collections import defaultdict
from typing import Final

from .models import CoverageRow, LabelCandidate, LabelTemplate, MessageRow
from .text import contains_keyword, matched_keywords, ngram_counts, pmi_collocations, redact_snippet, tokenize


MARKET_TEMPLATES: Final[dict[str, tuple[LabelTemplate, ...]]] = {
    "A02B2": (
        LabelTemplate("P-CAB/PPI 비교", ("P-CAB", "PCAB", "PPI", "케이캡", "테고", "비교", "경쟁"), "market_seed+data_gap", "1차 미분류 피드백의 P-CAB/PPI 경쟁어 보강"),
        LabelTemplate("식사무관/빠른발현", ("식사", "식전", "식후", "무관", "관계없", "빠른", "발현", "즉시", "30분"), "data_gap", "식사무관·빠른발현 누락 보강"),
        LabelTemplate("야간산분비/위산억제", ("야간", "위산", "산분비", "분비", "억제", "heartburn", "가슴쓰림"), "market_seed+collocation", "야간 위산/증상 조절"),
        LabelTemplate("GERD/역류성식도염", ("GERD", "역류성", "식도염", "위식도역류", "미란", "reflux"), "market_seed", "질환/적응증 축"),
        LabelTemplate("위염/궤양 적응증", ("위염", "위궤양", "십이지장", "궤양", "NSAID"), "ngram_discovery", "기존 GERD 밖 적응증 후보"),
        LabelTemplate("안전성/장기복용", ("안전", "부작용", "상호작용", "간장애", "장기", "6개월"), "market_seed", "안전성 및 장기복용"),
    ),
    "C10C0": (
        LabelTemplate("LDL-C 강하", ("LDL-C", "LDL", "콜레스테롤", "강하", "저하", "지질"), "xenon_seed", "제논 C10 seed"),
        LabelTemplate("복합제/병용 장점", ("복합제", "복합", "병용", "에제티미브", "ezetimibe", "combination"), "xenon_seed", "제논 C10 seed"),
        LabelTemplate("당뇨 안전성/NODM", ("NODM", "당뇨", "혈당", "신규 당뇨", "피타바스타틴"), "xenon_seed", "제논 C10 seed"),
        LabelTemplate("부작용/상호작용 감소", ("부작용", "근육", "간수치", "상호작용", "내약성"), "xenon_seed", "제논 C10 seed"),
        LabelTemplate("심혈관 예방/위험", ("심혈관", "ASCVD", "CV", "예방", "위험", "사건"), "xenon_seed", "제논 C10 seed"),
        LabelTemplate("TG/중성지방", ("TG", "중성지방", "triglyceride"), "xenon_seed", "제논 C10 seed"),
        LabelTemplate("약가/용량/제형", ("약가", "보험", "용량", "저용량", "제형", "10/"), "data_gap", "미분류 잔여의 용량·약가 축"),
    ),
    "C10A1": (
        LabelTemplate("LDL-C 강하", ("LDL-C", "LDL", "콜레스테롤", "강하", "저하", "지질"), "xenon_seed", "제논 C10 seed"),
        LabelTemplate("당뇨 안전성/NODM", ("NODM", "당뇨", "혈당", "발생 위험"), "xenon_seed", "제논 C10 seed"),
        LabelTemplate("심혈관 예방", ("심혈관", "ASCVD", "CV", "예방", "사건", "질환"), "xenon_seed", "제논 C10 seed"),
        LabelTemplate("부작용/내약성", ("부작용", "근육", "간수치", "내약성", "신장", "상호작용"), "xenon_seed", "제논 C10 seed"),
        LabelTemplate("오리지널/경쟁제품", ("오리지널", "리피토", "크레스토", "로수바스타틴", "아토르바스타틴", "피타바스타틴"), "data_gap", "제품/경쟁명 후보"),
    ),
    "A10N1": (
        LabelTemplate("혈당/HbA1c 조절", ("혈당", "HbA1c", "HBA1C", "당화혈색소", "조절", "강하"), "market_seed", "DPP-4 핵심 효능"),
        LabelTemplate("신기능/용량 조절", ("신기능", "신장", "신장애", "CKD", "용량", "알부민뇨"), "market_seed", "신장 환자/용량 축"),
        LabelTemplate("저혈당/체중 안전성", ("저혈당", "체중", "안전", "변동성", "미세혈관"), "market_seed", "안전성 축"),
        LabelTemplate("복약/병용 편의", ("복약", "복용", "병용", "순응도", "편의", "1일 1회", "메트포르민"), "market_seed", "복약 편의"),
        LabelTemplate("DPP-4 경쟁/선택성", ("DPP-4", "DPP4", "자누비아", "트라젠타", "선택성", "sitagliptin"), "data_gap", "경쟁 제품/기전 후보"),
    ),
    "G04C2": (
        LabelTemplate("전립선/BPH", ("전립선", "BPH", "비대", "전립선비대증"), "market_seed", "질환 축"),
        LabelTemplate("배뇨증상/IPSS", ("배뇨", "IPSS", "잔뇨", "빈뇨", "야간뇨", "요속", "QMAX"), "market_seed", "증상/스코어"),
        LabelTemplate("기립성저혈압/심혈관 안전성", ("기립성", "저혈압", "혈압", "심혈관", "A1A", "선택성"), "data_gap", "미분류의 안전성 세부축"),
        LabelTemplate("성기능/사정 부작용", ("성기능", "사정", "역사정", "발기", "조루"), "market_seed", "성기능 부작용"),
        LabelTemplate("복용/용량 편의", ("복용", "용량", "하루", "편의", "0.2", "0.4"), "market_seed", "복약 편의"),
    ),
    "L04D0": (
        LabelTemplate("JAK 억제제/기전", ("JAK", "억제제", "기전", "린버크", "젤잔즈"), "ngram_discovery", "JAK 연어 강신호"),
        LabelTemplate("류마티스관절염 치료", ("류마티스관절염", "류마티스", "관절염", "치료"), "ngram_discovery", "질환 축"),
        LabelTemplate("안전성/부작용", ("안전성", "안전", "부작용", "감염", "위험"), "ngram_discovery", "안전성 반복 문구"),
        LabelTemplate("효능/증상 개선", ("효과", "효능", "통증", "개선", "관해"), "generic", "효능 축"),
    ),
    "K01D2": (
        LabelTemplate("TPN/영양 공급", ("TPN", "영양", "공급", "투여", "수액"), "ngram_discovery", "영양수액 핵심"),
        LabelTemplate("아미노산/단백질 함량", ("아미노산", "단백질", "고함량", "함량"), "ngram_discovery", "단백질 함량"),
        LabelTemplate("오메가/Fish oil", ("오메", "오메가", "FISH OIL", "지방산", "OIL"), "collocation", "Fish oil 연어"),
        LabelTemplate("투여속도/편의", ("빠른 투여", "투여", "편의", "말초", "중심정맥"), "generic", "투여 편의"),
    ),
    "C11A1": (
        LabelTemplate("고혈압+고지혈증 복합", ("고혈압", "고지혈증", "이상지질혈증", "복합제", "동시에"), "ngram_discovery", "복합질환 축"),
        LabelTemplate("혈압 조절", ("혈압", "강압", "ARB", "암로디핀", "조절"), "generic", "혈압 효능"),
        LabelTemplate("지질/LDL 개선", ("LDL", "콜레스테롤", "지질", "감소", "로수바스타틴"), "generic", "지질 효능"),
        LabelTemplate("복약 순응도", ("복약", "순응도", "편의", "한알", "복합제로"), "collocation", "복합제 편의"),
    ),
    "L04B0": (
        LabelTemplate("TNF 억제제/기전", ("TNF", "억제제", "기전", "심퍼니"), "ngram_discovery", "TNF 연어 강신호"),
        LabelTemplate("류마티스관절염 치료", ("류마티스관절염", "류마티스", "관절염", "치료"), "ngram_discovery", "질환 축"),
        LabelTemplate("고농도/제형", ("고농도", "제형", "SC", "주사", "약물"), "collocation", "제형 차별점"),
        LabelTemplate("안전성/통증", ("안전성", "안전", "통증", "부작용"), "generic", "안전성/증상"),
    ),
    "B03A1": (
        LabelTemplate("철결핍성 빈혈", ("철결핍성", "빈혈", "철분", "혈색소", "HB"), "ngram_discovery", "질환/지표"),
        LabelTemplate("IV/고용량 빠른 투여", ("IV", "고용량", "빠른", "신속", "투여", "주사제"), "collocation", "투여 차별점"),
        LabelTemplate("수술 전후 빈혈", ("수술", "수술전", "수술 후", "수술환자"), "data_gap", "수술 맥락"),
        LabelTemplate("보험/용법", ("보험", "급여", "용량", "용법"), "generic", "보험/용법"),
        LabelTemplate("부작용/내약성", ("부작용", "내약성", "반응", "위장관"), "generic", "안전성"),
    ),
    "A06B1": (
        LabelTemplate("대장내시경/장정결", ("대장내시경", "장정결", "정결도", "검사", "OSS"), "ngram_discovery", "검사/정결도"),
        LabelTemplate("알약/복약 편의", ("오라팡", "알약", "복용", "복약", "편의"), "ngram_discovery", "정제 복약성"),
        LabelTemplate("거품제거/복합 작용", ("거품제거제", "거품", "복합 작용", "시메티콘"), "collocation", "복합 작용 후보"),
        LabelTemplate("효능/비교", ("효능", "비해", "비교", "차별"), "generic", "비교/효능"),
    ),
    "M01C0": (
        LabelTemplate("IL-6 억제제/기전", ("IL-6", "억제제", "기전", "악템라"), "ngram_discovery", "IL-6 연어 강신호"),
        LabelTemplate("류마티스관절염 치료", ("류마티스관절염", "류마티스", "관절염", "치료"), "ngram_discovery", "질환 축"),
        LabelTemplate("안전성/부작용", ("안전성", "안전", "부작용", "감염"), "generic", "안전성"),
        LabelTemplate("적응증/임상논의", ("적응증", "임상", "논의", "심포지엄"), "generic", "근거/확장"),
    ),
    "L03A1": (
        LabelTemplate("호중구감소증", ("호중구", "감소증", "호중구감소증", "중증"), "ngram_discovery", "질환/상태"),
        LabelTemplate("예방/기간 감소", ("예방", "기간", "감소", "발현"), "generic", "예방 효과"),
        LabelTemplate("항암/화학요법", ("항암", "화학요법", "세포독성", "암"), "data_gap", "치료 맥락"),
        LabelTemplate("작용기전/효능", ("작용기전", "효능", "롤론티스", "약제"), "generic", "기전/효능"),
    ),
    "B01C5": (
        LabelTemplate("아스피린+PPI 복합", ("아스피린", "PPI", "복합제", "라베프라졸"), "ngram_discovery", "복합제 정체성"),
        LabelTemplate("위장관 출혈 예방", ("위장관", "출혈", "예방", "궤양", "GI"), "collocation", "예방 가치"),
        LabelTemplate("저용량/용량", ("저용량", "100", "MG", "용량"), "generic", "용량 축"),
        LabelTemplate("부작용/안전성", ("부작용", "안전", "위장장애"), "generic", "안전성"),
    ),
    "A03F0": (
        LabelTemplate("기능성 소화불량", ("기능성", "소화불량", "소화불량증", "FD"), "ngram_discovery", "신규 시장 질환 축"),
        LabelTemplate("위장관 운동 조절", ("위장관", "운동", "조절", "모티리톤"), "collocation", "기전/작용"),
        LabelTemplate("증상 개선/효능", ("증상", "개선", "효능", "치료제"), "generic", "효능 축"),
        LabelTemplate("병용/안전성", ("병용", "부작용", "안전", "장기"), "generic", "병용/안전성"),
    ),
    "A10N3": (
        LabelTemplate("식후혈당/TIR", ("식후", "혈당", "TIR", "개선"), "small_market_seed", "소규모 시장 혈당 후보"),
        LabelTemplate("메트포르민 복합", ("메트포르민", "복합", "가드메트", "성분"), "small_market_seed", "복합제 정체성"),
        LabelTemplate("HbA1c/혈당 조절", ("HbA1c", "hba1c", "혈당", "조절", "당뇨병"), "small_market_seed", "핵심 효능"),
    ),
    "A07E9": (
        LabelTemplate("JAK/젤잔즈 치료", ("JAK", "젤잔즈", "류마티스관절염", "치료"), "small_market_seed", "9행 시장의 제한적 후보"),
        LabelTemplate("복약 편의/안전성", ("복약", "편의성", "PMS", "안전성"), "small_market_seed", "소량 데이터에서만 관찰"),
    ),
}


def build_label_candidates(market: str, keyword_rows: list[MessageRow], auxiliary_rows: list[MessageRow]) -> list[LabelCandidate]:
    """Create measured provisional candidates for a market from templates and discovered phrases."""
    texts = [row.text for row in keyword_rows]
    aux_texts = [row.text for row in auxiliary_rows]
    total = len(keyword_rows) or 1
    phrases = [term for term, _ in ngram_counts(texts + aux_texts, 2).most_common(40)]
    phrases.extend(term for term, _, _ in pmi_collocations(texts + aux_texts, max(2, min(10, total // 100)))[:30])
    candidates = [_candidate_from_template(market, template, keyword_rows, phrases, total) for template in MARKET_TEMPLATES.get(market, ())]
    present = [candidate for candidate in candidates if candidate.hit_count > 0 or total < 80]
    present.extend(_auto_phrase_candidates(market, keyword_rows, phrases, present, total))
    return sorted(present, key=lambda item: (-item.hit_count, item.label))


def _candidate_from_template(
    market: str,
    template: LabelTemplate,
    rows: list[MessageRow],
    phrases: list[str],
    total: int,
) -> LabelCandidate:
    """Measure one template and attach data-derived evidence terms."""
    matched_rows = [row for row in rows if matched_keywords(row.text, template.keywords)]
    phrase_hits = [phrase for phrase in phrases if any(contains_keyword(phrase, keyword) or contains_keyword(keyword, phrase) for keyword in template.keywords)]
    evidence = tuple(dict.fromkeys([*phrase_hits[:4], *template.keywords[:6]]))
    snippets = tuple(redact_snippet(row.text) for row in _representatives(matched_rows))
    return LabelCandidate(
        market,
        template.label,
        template.keywords,
        evidence,
        template.source,
        len(matched_rows),
        len(matched_rows) / total,
        snippets,
        template.note,
    )


def _auto_phrase_candidates(
    market: str,
    rows: list[MessageRow],
    phrases: list[str],
    existing: list[LabelCandidate],
    total: int,
) -> list[LabelCandidate]:
    """Add a few explicit discovery candidates not covered by templates."""
    existing_terms = " ".join(keyword for candidate in existing for keyword in candidate.keywords)
    auto: list[LabelCandidate] = []
    limit = 1 if total < 80 else 2
    for phrase in phrases:
        if len(auto) >= limit or any(contains_keyword(existing_terms, part) for part in tokenize(phrase)):
            continue
        matched_rows = [row for row in rows if contains_keyword(row.text, phrase)]
        if len(matched_rows) < max(2, total // 100):
            continue
        label = f"신규 후보: {phrase}"
        snippets = tuple(redact_snippet(row.text) for row in _representatives(matched_rows))
        auto.append(LabelCandidate(market, label, (phrase,), (phrase,), "auto_ngram_pmi", len(matched_rows), len(matched_rows) / total, snippets, "사람 검토용 자동 후보"))
    return auto


def _representatives(rows: list[MessageRow], limit: int = 3) -> list[MessageRow]:
    """Choose concise representative rows for PL review snippets."""
    distinct: dict[str, MessageRow] = {}
    for row in sorted(rows, key=lambda item: (abs(len(item.text) - 90), item.stage_hash)):
        distinct.setdefault(row.text, row)
    return list(distinct.values())[:limit]


def assign_labels(rows: list[MessageRow], candidates: list[LabelCandidate]) -> dict[str, tuple[str, ...]]:
    """Apply a market-local multi-label dictionary to rows."""
    by_market: defaultdict[str, list[LabelCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_market[candidate.market].append(candidate)
    assigned: dict[str, tuple[str, ...]] = {}
    for row in rows:
        labels = [candidate.label for candidate in by_market[row.market] if matched_keywords(row.text, candidate.keywords)]
        assigned[row.row_id] = tuple(dict.fromkeys(labels))
    return assigned


def coverage_by_market(rows: list[MessageRow], assignments: dict[str, tuple[str, ...]]) -> list[CoverageRow]:
    """Summarize unmatched and multilabel rates by market."""
    totals: defaultdict[str, int] = defaultdict(int)
    matched: defaultdict[str, int] = defaultdict(int)
    multilabel: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        labels = assignments.get(row.row_id, ())
        totals[row.market] += 1
        if labels:
            matched[row.market] += 1
        if len(labels) > 1:
            multilabel[row.market] += 1
    coverage: list[CoverageRow] = []
    for market, total in sorted(totals.items(), key=lambda item: (-item[1], item[0])):
        unmatched = total - matched[market]
        coverage.append(CoverageRow(market, total, matched[market], unmatched, multilabel[market], unmatched / total, multilabel[market] / total))
    return coverage

