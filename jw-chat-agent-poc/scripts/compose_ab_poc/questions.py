from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EvalQuestion:
    """Evaluation question with the deterministic intent used for scoring."""

    qid: str
    category: str
    question: str
    intent_id: str


QUESTIONS: tuple[EvalQuestion, ...] = (
    EvalQuestion("Q01", "advanced", "리바로와 리바로젯의 최근 6개월 매출 추이를 비교해줘", "brand_pair_sales_trend"),
    EvalQuestion("Q02", "advanced", "리바로 시장 상위 3개 브랜드의 점유율 변화를 비교해줘", "top_share_trend"),
    EvalQuestion("Q03", "advanced", "리바로 점유율이 최근 하락하는 이유가 뭐야?", "share_decline_context"),
    EvalQuestion("Q04", "advanced", "리바로 2월 매출이 떨어진 게 시장 전체 영향이야, 리바로만의 문제야?", "market_vs_brand_feb"),
    EvalQuestion("Q05", "advanced", "리바로 시장의 경쟁 구도가 최근 어떻게 변하고 있어?", "competition_change"),
    EvalQuestion("Q06", "advanced", "아토젯이 리바로를 위협하고 있어?", "atozet_threat"),
    EvalQuestion("Q07", "advanced", "아토젯 점유율이 오르는 동안 리바로 점유율은 어떻게 됐어?", "atozet_livaro_cross_trend"),
    EvalQuestion("Q08", "advanced", "최근 뉴스 이슈가 리바로 매출에 영향을 줬는지 봐줘", "news_sales_effect"),
    EvalQuestion("Q09", "advanced", "리바로 매출의 작년 동기 대비 성장률은?", "livaro_yoy_growth"),
    EvalQuestion("Q10", "advanced", "리바로의 지난 6개월 평균 점유율은?", "livaro_avg_share_6m"),
    EvalQuestion("Q11", "advanced", "리바로 시장은 집중된 시장이야, 분산된 시장이야? (상위 비중·집중도)", "market_concentration"),
    EvalQuestion("Q12", "advanced", "리바로 시장에서 상위 5개 브랜드가 차지하는 비중은?", "top5_share_sum"),
    EvalQuestion("Q13", "advanced", "리바로가 점유율 4%를 회복하려면 매출이 얼마나 늘어야 해?", "target_share_gap"),
    EvalQuestion("M01", "multistep", "리바로 의원 채널에서 성분별 점유율", "clinic_channel_molecule_share"),
    EvalQuestion("M02", "multistep", "리바로와 아토젯의 채널별 점유율 차이", "livaro_atozet_channel_diff"),
    EvalQuestion("M03", "multistep", "리바로 시장 오리지널 vs 제네릭 비중", "ox_gx_mix"),
    EvalQuestion("M04", "multistep", "리바로 상위 경쟁사 3개의 진료과별 매출", "top_competitor_specialty_sales"),
    EvalQuestion("M05", "multistep", "리바로 제형별 매출 추이(최근 1년)", "class_sales_trend_12m"),
    EvalQuestion("M06", "multistep", "리바로 시장에서 급매출 회사 top3와 그 성분", "top_company_molecule"),
    EvalQuestion("M07", "multistep", "리바로 급여/비급여 매출 구성과 추이", "nhi_mix_trend"),
)


INTENT_DESCRIPTIONS: dict[str, str] = {
    "brand_pair_sales_trend": "리바로와 리바로젯 최근 6개월 매출/MS 시계열 비교",
    "top_share_trend": "최근 6개월 상위 N개 브랜드 MS 시계열 비교",
    "share_decline_context": "리바로 MS 추이와 경쟁/시장 맥락, 인과 단정 금지",
    "market_vs_brand_feb": "2026-02 전후 리바로 매출 변화와 시장규모 변화 대조",
    "competition_change": "최근 상위 브랜드 rank/MS 변화로 경쟁 구도 설명",
    "atozet_threat": "아토젯과 리바로의 최근 MS/rank/value 비교",
    "atozet_livaro_cross_trend": "아토젯 MS 변화 중 리바로 MS 동행/역행 비교",
    "news_sales_effect": "뉴스-매출 인과 효과는 mart 단독으로 불가함을 정직 표시",
    "livaro_yoy_growth": "리바로 최신월 매출의 전년 동월 대비 성장률",
    "livaro_avg_share_6m": "리바로 최근 6개월 평균 MS",
    "market_concentration": "최신월 HHI, top3/top5 share, 브랜드 수",
    "top5_share_sum": "최신월 상위 5개 브랜드 share 합계",
    "target_share_gap": "최신 시장규모 기준 4% MS 달성 필요 매출과 증가분",
    "clinic_channel_molecule_share": "의원 채널 최신월 성분별 매출 점유율",
    "livaro_atozet_channel_diff": "리바로/아토젯 채널별 시장 내 점유율 차이",
    "ox_gx_mix": "최신월 오리지널/Ox vs 제네릭/Gx 매출 구성",
    "top_competitor_specialty_sales": "상위 경쟁 브랜드 3개 진료과별 최신월 매출",
    "class_sales_trend_12m": "최근 12개월 class(제형 proxy)별 매출 추이",
    "top_company_molecule": "최신월 회사 top3와 회사별 주요 성분",
    "nhi_mix_trend": "급여/비급여 차원 존재 여부와 미지원 처리",
}

