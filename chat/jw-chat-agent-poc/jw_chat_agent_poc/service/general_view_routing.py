from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from typing import Any, Protocol

from jw_chat_agent_poc.tools.general_view_backend import (
    AtcCandidate,
    GeneralMarket,
    GeneralViewBackend,
    GeneralViewBrandMismatchError,
    GeneralViewBackendError,
)
from jw_chat_agent_poc.tools.general_view_membership import (
    GeneralMembershipLoadError,
    MariaDbGeneralMembershipReader,
    TtlGeneralMembershipCache,
)
from jw_chat_agent_poc.tools.general_view_mart import GeneralViewMartBackend, MariaDbGeneralMartReader


_ATC4_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]\d{2}[A-Za-z]\d)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_IQVIA_SOURCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:IQVIA|NSA)(?![A-Za-z0-9])|아이큐비아",
    re.IGNORECASE,
)
_SOURCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:IQVIA|NSA|UBIST)(?![A-Za-z0-9])|아이큐비아|유비스트",
    re.IGNORECASE,
)


class GeneralRoute(Enum):
    EXISTING = "existing"
    GENERAL_ONLY = "general_only"
    DUAL = "dual"


class _Backend(Protocol):
    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]: ...
    def market(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMarket: ...


class _StrategicMembership(Protocol):
    def resolve(self, question: str, allow_default: bool = False): ...


class _GeneralMembership(Protocol):
    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]: ...


class GeneralViewService:
    def __init__(
        self,
        backend: _Backend,
        strategic_membership: _StrategicMembership,
        *,
        enabled: bool,
        general_membership: _GeneralMembership | None = None,
    ) -> None:
        self._backend = backend
        self._strategic_membership = strategic_membership
        self._general_membership = general_membership
        self.enabled = enabled

    @classmethod
    def from_env(cls, strategic_membership: _StrategicMembership) -> "GeneralViewService":
        enabled = os.environ.get("GENERAL_VIEW_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        ttl_seconds = float(os.environ.get("GENERAL_VIEW_MEMBERSHIP_TTL_SECONDS", "300"))
        membership = TtlGeneralMembershipCache(MariaDbGeneralMembershipReader(), ttl_seconds=ttl_seconds)
        backend = GeneralViewMartBackend(MariaDbGeneralMartReader(), GeneralViewBackend())
        return cls(backend, strategic_membership, enabled=enabled, general_membership=membership)

    def route(self, question: str) -> GeneralRoute:
        if not self.enabled:
            return GeneralRoute.EXISTING
        normalized = _normalize(question)
        if _has_explicit_strategic_signal(normalized):
            return GeneralRoute.EXISTING
        if _has_explicit_general_signal(normalized):
            return GeneralRoute.GENERAL_ONLY
        if _has_existing_analytic_signal(normalized):
            return GeneralRoute.EXISTING
        if not _asks_market_metric(normalized):
            return GeneralRoute.EXISTING
        try:
            self._strategic_membership.resolve(question, allow_default=False)
        except LookupError:
            return GeneralRoute.GENERAL_ONLY
        return GeneralRoute.DUAL

    def answer(self, question: str, *, compact: bool, dual: bool) -> dict[str, Any]:
        source = _source(question)
        measure = "sales"
        brand = _brand_hint(question)
        explicit_atc4 = _atc4_code(question)
        try:
            if explicit_atc4:
                candidates = (AtcCandidate(explicit_atc4, f"ATC4 {explicit_atc4}"),)
            elif brand:
                candidates = self._membership_candidates(brand, source)
            else:
                candidates = ()
            if not candidates:
                raise GeneralViewBackendError("ATC4 후보를 찾지 못했습니다")
            markets = self._fetch_candidates(candidates, brand or None, source, measure)
            selected = max(markets, key=lambda item: item.brand_value if item.brand_value is not None else float("-inf"))
            descriptions = {market.atc4_code: market.atc4_description for market in markets}
            others = [
                f"{candidate.code} ({descriptions.get(candidate.code, candidate.description)})"
                for candidate in candidates
                if candidate.code != selected.atc4_code
            ]
            contract = _contract(selected, other_candidates=others, compact=compact, dual=dual)
            return _result(question, selected, contract)
        except GeneralViewBackendError as exc:
            return _unavailable_result(question, str(exc), dual=dual)

    def _membership_candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]:
        if self._general_membership is not None:
            try:
                candidates = self._general_membership.candidates(brand, source)
            except GeneralMembershipLoadError:
                candidates = ()
            if candidates:
                return candidates
        return self._backend.candidates(brand, source)

    def _fetch_candidates(
        self,
        candidates: tuple[AtcCandidate, ...],
        brand: str | None,
        source: str,
        measure: str,
    ) -> tuple[GeneralMarket, ...]:
        if len(candidates) == 1:
            return (self._backend.market(candidates[0].code, brand, source, measure),)
        markets: list[GeneralMarket] = []
        with ThreadPoolExecutor(max_workers=min(5, len(candidates)), thread_name_prefix="general-atc") as executor:
            futures = {
                executor.submit(self._backend.market, candidate.code, brand, source, measure): candidate
                for candidate in candidates
            }
            for future in as_completed(futures):
                try:
                    markets.append(future.result())
                except GeneralViewBrandMismatchError:
                    continue
        if not markets:
            raise GeneralViewBrandMismatchError(
                "general-view brand mismatch: requested brand is absent from every ATC4 candidate"
            )
        return tuple(markets)


def _contract(
    market: GeneralMarket,
    *,
    other_candidates: list[str],
    compact: bool,
    dual: bool,
) -> dict[str, Any]:
    section = _render_section(market, other_candidates=other_candidates, compact=compact)
    return {
        "mode": "dual" if dual else "general_only",
        "view_type": "general_view",
        "market_basis": "ATC4",
        "atc4_code": market.atc4_code,
        "atc4_description": market.atc4_description,
        "source": market.source,
        "measure": market.measure,
        "unit": market.unit,
        "period": market.period,
        "share_denominator": f"ATC4 {market.atc4_code} 시장 전체 {market.measure}",
        "other_atc4_candidates": other_candidates,
        "section_markdown": section,
    }


def _result(question: str, market: GeneralMarket, contract: dict[str, Any]) -> dict[str, Any]:
    call = {
        "source": "jw-market-backend-api",
        "tool": "general_view_dynamic_market",
        "summary_text": f"ATC4 {market.atc4_code} 일반뷰를 조회했습니다.",
        "render_data": dict(contract),
    }
    return {
        "question": question,
        "resolution": {"canonical_brand": market.brand, "atc4_code": market.atc4_code},
        "decomposition": [{"intent": "general_view_market_metric", "view_type": "general_view"}],
        "router_diagnostics": {"deterministic": True, "general_view": True},
        "tool_calls": [call],
        "answer": contract["section_markdown"],
        "markdown_response": None,
        "sources": [market.source],
        "general_view_contract": contract,
        "general_view_ready": contract["mode"] == "general_only",
    }


def _unavailable_result(question: str, reason: str, *, dual: bool) -> dict[str, Any]:
    text = f"## 일반뷰 (ATC4)\n\n일반뷰 데이터를 현재 조회할 수 없습니다. ({reason})"
    contract = {
        "mode": "dual" if dual else "general_only",
        "view_type": "general_view",
        "market_basis": "ATC4",
        "atc4_code": "확인 불가",
        "atc4_description": "확인 불가",
        "source": "확인 불가",
        "measure": "sales",
        "unit": "",
        "period": "확인 불가",
        "share_denominator": "확인 불가",
        "other_atc4_candidates": [],
        "section_markdown": text,
        "unavailable": True,
    }
    return {
        "question": question,
        "decomposition": [{"intent": "general_view_unavailable", "view_type": "general_view"}],
        "router_diagnostics": {"deterministic": True, "general_view": True, "unavailable": True},
        "tool_calls": [{"source": "jw-market-backend-api", "tool": "general_view_unavailable", "render_data": contract}],
        "answer": text,
        "markdown_response": None,
        "sources": [],
        "general_view_contract": contract,
        "general_view_ready": not dual,
    }


def _render_section(market: GeneralMarket, *, other_candidates: list[str], compact: bool) -> str:
    lines = ["## 일반뷰 (ATC4)", "", f"- 시장: {market.atc4_description}"]
    if market.market_size is not None:
        lines.append(f"- 시장 규모: {_format_value(market.market_size, market.unit)}")
    if market.brand:
        metrics = []
        if market.brand_share_pct is not None:
            metrics.append(f"점유율 {market.brand_share_pct:.2f}%")
        if market.brand_rank is not None:
            metrics.append(f"순위 {market.brand_rank}위")
        if market.brand_value is not None:
            metrics.append(f"매출 {_format_value(market.brand_value, market.unit)}")
        lines.append(f"- {market.brand}: " + ", ".join(metrics or ["지표 없음"]))
    if market.top_brands:
        top_rows = market.top_brands[:5]
        summary = ", ".join(
            f"{row.rank or index}위 {row.brand}" + (f" ({row.share_pct:.2f}%)" if row.share_pct is not None else "")
            for index, row in enumerate(top_rows, 1)
        )
        lines.append(f"- Top 5: {summary}")
    if other_candidates:
        lines.append("- 다른 ATC4 후보: " + ", ".join(other_candidates))
    if not compact:
        lines.extend(("", f"점유율 분모: ATC4 {market.atc4_code} 시장 전체 {market.measure}"))
    return "\n".join(lines)


def _format_value(value: float, unit: str) -> str:
    if unit.upper() == "KRW":
        return f"{value / 100_000_000:,.1f}억원"
    return f"{value:,.2f} {unit}".strip()


def _normalize(question: str) -> str:
    return re.sub(r"\s+", "", question).lower()


def _has_explicit_general_signal(normalized: str) -> bool:
    return bool(_ATC4_PATTERN.search(normalized)) or any(
        token in normalized for token in ("일반뷰", "일반view", "atc4", "atc기준")
    )


def _has_explicit_strategic_signal(normalized: str) -> bool:
    return any(
        token in normalized
        for token in ("전략뷰", "전략view", "시장조망", "market_landscape", "경쟁군", "경쟁시장", "competitive_dynamics", "cd기준")
    )


def _asks_market_metric(normalized: str) -> bool:
    return any(token in normalized for token in ("시장점유율", "시장규모", "시장순위", "시장에서", "같은시장"))


def _has_existing_analytic_signal(normalized: str) -> bool:
    return any(
        token in normalized
        for token in (
            "추이",
            "변화",
            "비교",
            "하락",
            "영향",
            "경쟁",
            "위협",
            "성장률",
            "평균",
            "집중",
            "분산",
            "상위",
            "회복",
            "얼마나",
            "이유",
            "채널",
            "뉴스",
        )
    )


def _source(question: str) -> str:
    return "iqvia" if _IQVIA_SOURCE_PATTERN.search(question) else "ubist"


def _atc4_code(question: str) -> str | None:
    match = _ATC4_PATTERN.search(question)
    return match.group(1).upper() if match else None


def _brand_hint(question: str) -> str:
    text = _SOURCE_PATTERN.sub(" ", question)
    text = _ATC4_PATTERN.sub(" ", text)
    text = re.split(r"시장|점유율|매출|순위|규모|top\s*\d*", text, maxsplit=1, flags=re.IGNORECASE)[0]
    text = re.sub(r"일반뷰|전략뷰|ATC4?|기준|으로|에서|의", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" ?은는이가을를")
