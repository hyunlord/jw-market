from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from enum import Enum
from typing import Any, Protocol

from jw_chat_agent_poc.agent_loop.structured_planner import structured_metric_owner
from jw_chat_agent_poc.common.periods import requested_period
from jw_chat_agent_poc.common.qa_trace import attach_tool_qa_trace, qa_trace_started_at
from jw_chat_agent_poc.orchestrator.markdown_renderers import market_members_md
from jw_chat_agent_poc.resolver.catalog_membership import MariaDbCatalogMembershipReader
from jw_chat_agent_poc.tools.general_view_backend import (
    AtcCandidate,
    GeneralMarket,
    GeneralViewBackend,
    GeneralViewBrandMismatchError,
    GeneralViewBackendError,
    TopBrand,
)
from jw_chat_agent_poc.tools.general_view_membership import (
    GeneralMembershipLoadError,
    MariaDbGeneralMembershipReader,
    TtlGeneralMembershipCache,
)
from jw_chat_agent_poc.tools.general_view_mart import GeneralViewMartBackend, MariaDbGeneralMartReader
from jw_chat_agent_poc.tools.metrics.market_scope_intent import (
    asks_market_members,
    asks_other_market_members,
    detect_market_scope_intent,
    requested_market_member_limit,
)


LOGGER = logging.getLogger(__name__)

# Live catalog and membership snapshots contain UBIST 3-5 character codes,
# while IQVIA and combined catalog rows use the five-character form.
_ATC4_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]\d{1,2}[A-Za-z]\d?)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_CATALOG_ATC4_PATTERNS = {
    "ubist": re.compile(r"[A-Z]\d{1,2}[A-Z]\d?"),
    "iqvia": re.compile(r"[A-Z]\d{2}[A-Z]\d"),
    "both": re.compile(r"[A-Z]\d{2}[A-Z]\d"),
}
_CONNECTION_STRING_RE = re.compile(r"(?i)\b(?:mysql|mariadb)://[^\s]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*=\s*[^\s,;]+"
)
_CATALOG_CONNECTION_ERROR_CODES = frozenset({2002, 2003, 2005, 2006, 2013})
_IQVIA_SOURCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:IQVIA|NSA)(?![A-Za-z0-9])|아이큐비아",
    re.IGNORECASE,
)
_SOURCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:IQVIA|NSA|UBIST)(?![A-Za-z0-9])|아이큐비아|유비스트",
    re.IGNORECASE,
)
_STRATEGIC_MARKET_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9_])ml_\d+(?![A-Za-z0-9_])", re.IGNORECASE)
_STRATEGIC_MARKET_SPLIT_LIMIT = 4


class GeneralRoute(Enum):
    EXISTING = "existing"
    GENERAL_ONLY = "general_only"
    DUAL = "dual"


class _Backend(Protocol):
    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]: ...
    def market(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMarket: ...


class _StrategicMembership(Protocol):
    def resolve(self, question: str, allow_default: bool = False): ...
    def explicit_market(self, question: str) -> tuple[str, str] | None: ...
    def market_members(self, question: str) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class StrategicMarketDefinition:
    market_id: str
    data_source: str
    atc4_codes: tuple[str, ...]
    excluded_atc4_count: int = 0


class CatalogDefinitionLoadError(RuntimeError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class _StrategicMarketDefinitionReader(Protocol):
    def resolve(self, market_id: str) -> StrategicMarketDefinition | None: ...


@dataclass(frozen=True, slots=True)
class MariaDbStrategicMarketDefinitionReader:
    catalog: MariaDbCatalogMembershipReader = field(default_factory=MariaDbCatalogMembershipReader)

    def resolve(self, market_id: str) -> StrategicMarketDefinition | None:
        import pymysql

        try:
            with pymysql.connect(
                host=self.catalog.host,
                port=self.catalog.port,
                user=self.catalog.user,
                password=self.catalog.password,
                database=self.catalog.database,
                connect_timeout=self.catalog.connect_timeout_s,
                read_timeout=self.catalog.read_timeout_s,
                write_timeout=self.catalog.read_timeout_s,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT ml_id, data_source, atc_codes_json
                        FROM catalog_ml_market
                        WHERE ml_id = %s
                        """,
                        (market_id,),
                    )
                    rows = tuple(cursor.fetchall())
        except pymysql.MySQLError as exc:
            error_code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
            reason_code = (
                "catalog_db_unreachable"
                if error_code in _CATALOG_CONNECTION_ERROR_CODES
                else "catalog_parse_error"
            )
            raise CatalogDefinitionLoadError(
                reason_code,
                "catalog_ml_market query failed",
            ) from exc
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError(f"catalog_ml_market primary-key lookup returned {len(rows)} rows")
        row = rows[0]
        source = str(row.get("data_source") or "").strip().lower()
        raw_codes = row.get("atc_codes_json")
        if isinstance(raw_codes, str):
            try:
                raw_codes = json.loads(raw_codes)
            except json.JSONDecodeError as exc:
                raise CatalogDefinitionLoadError(
                    "catalog_parse_error",
                    "catalog_ml_market.atc_codes_json is not valid JSON",
                ) from exc
        if raw_codes is None:
            raw_codes = ()
        if not isinstance(raw_codes, (list, tuple)):
            raise CatalogDefinitionLoadError(
                "catalog_parse_error",
                "catalog_ml_market.atc_codes_json must be an array",
            )
        pattern = _CATALOG_ATC4_PATTERNS.get(source)
        codes: list[str] = []
        excluded_count = 0
        for raw_code in raw_codes:
            code = str(raw_code or "").strip().upper()
            if not code or pattern is None or pattern.fullmatch(code) is None:
                excluded_count += 1
                continue
            if code not in codes:
                codes.append(code)
        if raw_codes and not codes:
            raise CatalogDefinitionLoadError(
                "catalog_all_codes_invalid",
                f"catalog_ml_market contains no valid ATC4 codes for source={source or 'missing'}",
            )
        return StrategicMarketDefinition(
            market_id=str(row.get("ml_id") or market_id),
            data_source=source,
            atc4_codes=tuple(codes),
            excluded_atc4_count=excluded_count,
        )


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
        market_definition_reader: _StrategicMarketDefinitionReader | None = None,
    ) -> None:
        self._backend = backend
        self._strategic_membership = strategic_membership
        self._general_membership = general_membership
        self._market_definition_reader = market_definition_reader
        self.enabled = enabled

    @classmethod
    def from_env(cls, strategic_membership: _StrategicMembership) -> "GeneralViewService":
        enabled = os.environ.get("GENERAL_VIEW_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        ttl_seconds = float(os.environ.get("GENERAL_VIEW_MEMBERSHIP_TTL_SECONDS", "300"))
        membership = TtlGeneralMembershipCache(MariaDbGeneralMembershipReader(), ttl_seconds=ttl_seconds)
        backend = GeneralViewMartBackend(MariaDbGeneralMartReader(), GeneralViewBackend())
        return cls(
            backend,
            strategic_membership,
            enabled=enabled,
            general_membership=membership,
            market_definition_reader=MariaDbStrategicMarketDefinitionReader(),
        )

    def route(self, question: str) -> GeneralRoute:
        if not self.enabled:
            return GeneralRoute.EXISTING
        normalized = _normalize(question)
        if _has_explicit_strategic_view_signal(normalized):
            return GeneralRoute.EXISTING
        if _has_explicit_general_signal(normalized):
            return GeneralRoute.GENERAL_ONLY
        if "뉴스" in normalized:
            return GeneralRoute.EXISTING
        membership_state = self._strategic_membership_state(question)
        if (
            membership_state is None
            and _asks_general_brand_metric(normalized)
            and self._has_general_membership(question)
        ):
            return GeneralRoute.GENERAL_ONLY
        market_intent = detect_market_scope_intent(question)
        if membership_state is False and (
            market_intent is not None
            or asks_market_members(question)
            or _asks_general_brand_metric(normalized)
        ):
            return GeneralRoute.GENERAL_ONLY
        if market_intent is not None and membership_state is not True:
            return GeneralRoute.GENERAL_ONLY
        if _has_existing_analytic_signal(normalized):
            return GeneralRoute.EXISTING
        if structured_metric_owner(question) == "brand" and self._explicit_strategic_market(question):
            return GeneralRoute.EXISTING
        if market_intent is not None:
            return GeneralRoute.EXISTING
        if not _asks_market_metric(normalized):
            return GeneralRoute.EXISTING
        if membership_state is not True:
            return GeneralRoute.GENERAL_ONLY
        return GeneralRoute.DUAL

    def _strategic_membership_state(self, question: str) -> bool | None:
        try:
            resolution = self._strategic_membership.resolve(question, allow_default=False)
        except LookupError:
            return None
        market_ids = getattr(resolution, "market_ids", None)
        if market_ids is None:
            return True
        return bool(market_ids or getattr(resolution, "market_id", None))

    def _explicit_strategic_market(self, question: str) -> bool:
        return self._strategic_market(question) is not None

    def _strategic_market(self, question: str) -> tuple[str, str] | None:
        resolve_market = getattr(self._strategic_membership, "explicit_market", None)
        if not callable(resolve_market):
            return None
        try:
            return resolve_market(question)
        except (LookupError, OSError, TypeError, ValueError):
            return None

    def _has_general_membership(self, question: str) -> bool:
        if self._general_membership is None:
            return False
        brand = _brand_hint(question)
        if not brand:
            return False
        requested_source = _requested_source(question)
        sources = (requested_source,) if requested_source is not None else ("ubist", "iqvia")
        try:
            resolve = getattr(self._general_membership, "resolve", None)
            if callable(resolve):
                return any(resolve(brand, source) is not None for source in sources)
            candidates = getattr(self._general_membership, "candidates", None)
            return callable(candidates) and any(candidates(brand, source) for source in sources)
        except (GeneralMembershipLoadError, LookupError, OSError, TypeError, ValueError):
            return False

    def observability(self) -> dict[str, int | float | None]:
        metrics = getattr(self._general_membership, "observability", None)
        if callable(metrics):
            return metrics()
        return {
            "row_count": 0,
            "snapshot_age_seconds": None,
            "refresh_successes": 0,
            "refresh_failures": 0,
        }

    def answer(self, question: str, *, compact: bool, dual: bool) -> dict[str, Any]:
        started_at = qa_trace_started_at()
        member_period = requested_period(question) if asks_market_members(question) else None
        if member_period is not None:
            return _unavailable_result(
                question,
                f"{member_period} 기준 구성 브랜드 목록은 지원하지 않으며 최신 값으로 대체하지 않습니다",
                dual=dual,
                started_at=started_at,
            )
        requested_source = _requested_source(question)
        source = requested_source or "ubist"
        measure = "sales"
        brand = _brand_hint(question)
        strategic_market = self._strategic_market(question)
        explicit_strategic_market = strategic_market is not None
        try:
            brand = str(self._strategic_membership.resolve(question, allow_default=False).canonical_brand)
        except (AttributeError, LookupError, OSError, TypeError, ValueError):
            if explicit_strategic_market:
                brand = ""
        resolved_brand = brand
        explicit_atc4 = _atc4_code(question)
        membership_source = "not_applicable"
        selection_trace = self._selection_trace(question, strategic_market)
        try:
            if explicit_atc4:
                candidates = (AtcCandidate(explicit_atc4, f"ATC4 {explicit_atc4}"),)
                membership_source = "explicit_atc4"
                selection_trace.update(
                    atc4_source="fallback",
                    candidate_atc4_codes=[explicit_atc4],
                    reduction_reason="explicit_atc4",
                )
            elif strategic_market is not None:
                (
                    candidates,
                    definition_source,
                    fallback_reason,
                    excluded_count,
                ) = self._catalog_market_candidates(
                    strategic_market[0],
                    requested_source=requested_source,
                )
                if candidates:
                    source = definition_source
                    resolved_brand = ""
                    membership_source = "catalog_definition"
                    selection_trace.update(
                        atc4_source="catalog_definition",
                        candidate_atc4_codes=[candidate.code for candidate in candidates],
                        excluded_atc4_count=excluded_count,
                        reduction_reason=fallback_reason,
                    )
                else:
                    selection_trace.update(
                        excluded_atc4_count=excluded_count,
                        reduction_reason=fallback_reason,
                    )
                    candidates, resolved_brand, membership_source, source = self._brand_or_market_candidates(
                        question,
                        brand,
                        source,
                        requested_source=requested_source,
                    )
                    selection_trace.update(
                        atc4_source=_public_atc4_source(membership_source),
                        candidate_atc4_codes=[candidate.code for candidate in candidates],
                    )
            elif brand:
                candidates, resolved_brand, membership_source, source = self._brand_or_market_candidates(
                    question,
                    brand,
                    source,
                    requested_source=requested_source,
                )
                selection_trace.update(
                    atc4_source=_public_atc4_source(membership_source),
                    candidate_atc4_codes=[candidate.code for candidate in candidates],
                )
            else:
                candidates, membership_source = self._strategic_market_candidates(question, source)
                selection_trace.update(
                    atc4_source=_public_atc4_source(membership_source),
                    candidate_atc4_codes=[candidate.code for candidate in candidates],
                )
            if not candidates:
                raise GeneralViewBackendError("ATC4 후보를 찾지 못했습니다")
            hhi_requested = _asks_hhi(question)
            if hhi_requested and len(candidates) > _STRATEGIC_MARKET_SPLIT_LIMIT:
                codes = ", ".join(f"{candidate.code} ({candidate.description})" for candidate in candidates)
                raise GeneralViewBackendError(
                    f"일반뷰 ATC4가 분리 표시 상한 {_STRATEGIC_MARKET_SPLIT_LIMIT}개를 초과합니다. "
                    f"ATC4를 지정해 조회해 주세요. 전체 후보: {codes}"
                )
            markets = self._fetch_candidates(
                candidates,
                resolved_brand or None,
                source,
                measure,
                require_all=explicit_strategic_market or hhi_requested,
            )
            if hhi_requested and any(market.hhi_recent is None for market in markets):
                missing = ", ".join(market.atc4_code for market in markets if market.hhi_recent is None)
                raise GeneralViewBackendError(f"ATC4 {missing}의 HHI 데이터가 없습니다")
            if hhi_requested and len(markets) > 1:
                ordered_markets = tuple(sorted(markets, key=lambda item: item.atc4_code))
                contract = _multi_contract(
                    f"{resolved_brand or brand} 일반뷰",
                    ordered_markets,
                    compact=compact,
                    dual=dual,
                    question=question,
                )
                contract["membership_source"] = membership_source
                contract.update(selection_trace)
                return _multi_result(question, ordered_markets, contract, started_at=started_at)
            if explicit_strategic_market and len(markets) > 1:
                if strategic_market is None:
                    raise GeneralViewBackendError("전략시장 정보를 다시 확인할 수 없습니다")
                ordered_markets = tuple(sorted(markets, key=lambda item: item.atc4_code))
                contract = _multi_contract(
                    strategic_market[1],
                    ordered_markets,
                    compact=compact,
                    dual=dual,
                    question=question,
                )
                contract["membership_source"] = membership_source
                contract.update(selection_trace)
                return _multi_result(question, ordered_markets, contract, started_at=started_at)
            selected = max(markets, key=lambda item: item.brand_value if item.brand_value is not None else float("-inf"))
            descriptions = {market.atc4_code: market.atc4_description for market in markets}
            others = [
                f"{candidate.code} ({descriptions.get(candidate.code, candidate.description)})"
                for candidate in candidates
                if candidate.code != selected.atc4_code
            ]
            contract = _contract(selected, other_candidates=others, compact=compact, dual=dual, question=question)
            contract["membership_source"] = membership_source
            contract.update(selection_trace)
            return _result(question, selected, contract, started_at=started_at)
        except GeneralViewBackendError as exc:
            reason = str(exc)
            if asks_market_members(question) and "ATC4 후보" in reason:
                reason = "시장 매핑이 확인되지 않습니다"
            return _unavailable_result(
                question,
                reason,
                dual=dual,
                started_at=started_at,
                selection_trace=selection_trace,
            )

    def _selection_trace(self, question: str, market: tuple[str, str] | None) -> dict[str, Any]:
        members: tuple[str, ...] = ()
        if market is not None:
            market_members = getattr(self._strategic_membership, "market_members", None)
            if callable(market_members):
                try:
                    members = tuple(market_members(question))
                except (LookupError, OSError, TypeError, ValueError):
                    members = ()
        return {
            "input_market": market[0] if market is not None else None,
            "atc4_source": "fallback",
            "candidate_atc4_codes": [],
            "member_brand_count": len(members),
            "excluded_atc4_count": 0,
            "reduction_reason": None,
        }

    def _catalog_market_candidates(
        self,
        market_id: str,
        *,
        requested_source: str | None,
    ) -> tuple[tuple[AtcCandidate, ...], str, str | None, int]:
        reader = self._market_definition_reader
        if reader is None:
            return (), requested_source or "ubist", "catalog_definition_reader_unavailable", 0
        try:
            definition = reader.resolve(market_id)
        except (CatalogDefinitionLoadError, LookupError, OSError, TypeError, ValueError) as exc:
            reason_code = _catalog_definition_failure_reason(exc)
            logged_error = exc.__cause__ if exc.__cause__ is not None else exc
            LOGGER.warning(
                "catalog definition load failed reason_code=%s error_type=%s error_message=%s",
                reason_code,
                type(logged_error).__name__,
                _sanitize_catalog_exception(logged_error),
            )
            return (), requested_source or "ubist", reason_code, 0
        if definition is None:
            return (), requested_source or "ubist", "catalog_row_absent", 0
        if not definition.atc4_codes:
            return (), requested_source or definition.data_source or "ubist", "catalog_definition_empty", 0
        source = requested_source or definition.data_source or "ubist"
        return (
            tuple(AtcCandidate(code, f"ATC4 {code}") for code in definition.atc4_codes),
            source,
            "catalog_code_invalid" if definition.excluded_atc4_count else None,
            definition.excluded_atc4_count,
        )

    def _brand_or_market_candidates(
        self,
        question: str,
        brand: str,
        source: str,
        *,
        requested_source: str | None,
    ) -> tuple[tuple[AtcCandidate, ...], str, str, str]:
        if not brand:
            candidates, membership_source = self._strategic_market_candidates(question, source)
            return candidates, "", membership_source, source
        candidates, resolved_brand, membership_source = self._membership_resolution(brand, source)
        if not candidates and requested_source is None:
            alternate_source = "iqvia" if source == "ubist" else "ubist"
            candidates, resolved_brand, membership_source = self._membership_resolution(
                brand,
                alternate_source,
            )
            if candidates:
                source = alternate_source
        return candidates, resolved_brand, membership_source, source

    def _membership_resolution(
        self,
        brand: str,
        source: str,
    ) -> tuple[tuple[AtcCandidate, ...], str, str]:
        if self._general_membership is not None:
            try:
                resolve = getattr(self._general_membership, "resolve", None)
                if callable(resolve):
                    resolution = resolve(brand, source)
                    candidates = ()
                    if resolution is not None:
                        return resolution.candidates, resolution.brand_key, "membership_db"
                else:
                    candidates = self._general_membership.candidates(brand, source)
            except GeneralMembershipLoadError:
                candidates = ()
            if candidates:
                return candidates, brand, "membership_db"
        return self._backend.candidates(brand, source), brand, "backend_fallback"

    def _strategic_market_candidates(
        self,
        question: str,
        source: str,
    ) -> tuple[tuple[AtcCandidate, ...], str]:
        market_members = getattr(self._strategic_membership, "market_members", None)
        if not callable(market_members):
            return (), "unavailable"
        market = self._strategic_market(question)
        if market is None:
            return (), "unavailable"
        if self._general_membership is None:
            raise GeneralViewBackendError(
                f"전략시장 '{market[1]}'의 구성 브랜드와 ATC4 멤버십을 연결할 수 없습니다"
            )
        brands = market_members(question)
        if not brands:
            raise GeneralViewBackendError(f"전략시장 '{market[1]}'의 구성 브랜드를 확인할 수 없습니다")
        candidates_by_code: dict[str, AtcCandidate] = {}
        membership_sources: set[str] = set()
        for member_brand in brands:
            candidates, _, membership_source = self._membership_resolution(member_brand, source)
            membership_sources.add(membership_source)
            for candidate in candidates:
                candidates_by_code.setdefault(candidate.code, candidate)
        candidates = tuple(candidates_by_code[code] for code in sorted(candidates_by_code))
        if len(candidates) > _STRATEGIC_MARKET_SPLIT_LIMIT:
            codes = ", ".join(f"{candidate.code} ({candidate.description})" for candidate in candidates)
            raise GeneralViewBackendError(
                f"전략시장 '{market[1]}'의 ATC4가 분리 표시 상한 "
                f"{_STRATEGIC_MARKET_SPLIT_LIMIT}개를 초과합니다. "
                f"ATC4를 지정해 조회해 주세요. 전체 후보: {codes}"
            )
        if not candidates:
            raise GeneralViewBackendError(
                f"전략시장 '{market[1]}'의 구성 브랜드에 대응하는 ATC4를 확인할 수 없습니다"
            )
        return candidates, _combined_source(membership_sources)

    def _fetch_candidates(
        self,
        candidates: tuple[AtcCandidate, ...],
        brand: str | None,
        source: str,
        measure: str,
        *,
        require_all: bool = False,
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
                except GeneralViewBrandMismatchError as exc:
                    if require_all:
                        candidate = futures[future]
                        raise GeneralViewBackendError(
                            f"ATC4 {candidate.code} 일반뷰 데이터를 독립 조회할 수 없습니다"
                        ) from exc
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
    question: str,
) -> dict[str, Any]:
    window = _requested_market_window(question, market)
    member_fields = _member_contract_fields(market, question) if asks_market_members(question) else {}
    section = _render_section(
        market,
        other_candidates=other_candidates,
        compact=compact,
        window=window,
        question=question,
        member_fields=member_fields,
    )
    contract = {
        "mode": "dual" if dual else "general_only",
        "view_type": "general_view",
        "market_basis": "ATC4",
        "atc4_code": market.atc4_code,
        "atc4_description": market.atc4_description,
        "source": market.source,
        "selected_data_path": market.selected_data_path,
        "measure": market.measure,
        "unit": market.unit,
        "period": window[0] if window else market.period,
        "share_denominator": f"ATC4 {market.atc4_code} 시장 전체 {market.measure}",
        "other_atc4_candidates": other_candidates,
        "section_markdown": section,
    }
    if _asks_hhi(question):
        contract["hhi_recent"] = market.hhi_recent
    if market.fallback_reason is not None:
        contract["fallback_reason"] = market.fallback_reason
    contract.update(member_fields)
    return contract


def _multi_contract(
    strategic_market_name: str,
    markets: tuple[GeneralMarket, ...],
    *,
    compact: bool,
    dual: bool,
    question: str,
) -> dict[str, Any]:
    sections: list[dict[str, Any]] = []
    for market in markets:
        section = _contract(market, other_candidates=[], compact=compact, dual=dual, question=question)
        section["market_size"] = market.market_size
        section["market_size_recent_krw"] = market.market_size
        public_label = _public_atc4_market_label(market)
        section_heading = (
            f"### ATC4 {market.atc4_code}"
            if public_label == f"ATC4 {market.atc4_code}"
            else f"### ATC4 {market.atc4_code} — {public_label}"
        )
        section["section_markdown"] = section["section_markdown"].replace("## 일반뷰 (ATC4)", section_heading, 1)
        sections.append(section)
    codes = [market.atc4_code for market in markets]
    selected_paths = {market.selected_data_path for market in markets}
    explanation = (
        f"{strategic_market_name}은 {'·'.join(codes)} {len(codes)}개 ATC4에 걸쳐 있어 "
        "각각의 일반뷰로 나눠 보여드립니다."
    )
    section_markdown = "\n\n".join(
        ["## 일반뷰 (ATC4별 분리)", explanation, *(str(section["section_markdown"]) for section in sections)]
    )
    return {
        "mode": "dual" if dual else "general_only",
        "view_type": "general_view",
        "market_basis": "ATC4",
        "split_by": "ATC4",
        "strategic_market_name": strategic_market_name,
        "atc4_codes": codes,
        "atc4_sections": sections,
        "selected_data_path": next(iter(selected_paths)) if len(selected_paths) == 1 else "mixed",
        "section_markdown": section_markdown,
    }


def _result(
    question: str,
    market: GeneralMarket,
    contract: dict[str, Any],
    *,
    started_at: datetime,
) -> dict[str, Any]:
    member_query = "member_brands" in contract
    tool = "get_market_members" if member_query else "general_view_dynamic_market"
    summary = (
        f"ATC4 {market.atc4_code} 일반뷰의 구성 브랜드를 조회했습니다. "
        f"총 {int(contract.get('total_brands_in_market') or 0):,}개 중 "
        f"{int(contract.get('displayed_brand_count') or 0):,}개 표시"
        if member_query
        else f"ATC4 {market.atc4_code} 일반뷰를 조회했습니다."
    )
    call = {
        "source": _data_path_source(market),
        "tool": tool,
        "summary_text": summary,
        "render_data": dict(contract),
    }
    attach_tool_qa_trace(
        call,
        started_at=started_at,
        status="ok",
        row_count=max(1, int(contract.get("displayed_brand_count") or len(market.top_brands))),
        data_as_of=str(contract.get("period") or "") or None,
        cache_hit=False,
    )
    _attach_data_path_trace(call, market)
    _attach_selection_trace(call, contract)
    return {
        "question": question,
        "resolution": {"canonical_brand": market.brand, "atc4_code": market.atc4_code},
        "decomposition": [
            {
                "intent": "market_members" if member_query else "general_view_market_metric",
                "view_type": "general_view",
            }
        ],
        "router_diagnostics": {
            "mode": "general_view",
            "reason": "general_view_dynamic_market",
            "deterministic": True,
            "general_view": True,
            "selected_data_path": market.selected_data_path,
            "membership_source": contract.get("membership_source"),
            "input_market": contract.get("input_market"),
            "atc4_source": contract.get("atc4_source"),
            "candidate_atc4_codes": contract.get("candidate_atc4_codes"),
            "member_brand_count": contract.get("member_brand_count"),
            "excluded_atc4_count": contract.get("excluded_atc4_count"),
            "reduction_reason": contract.get("reduction_reason"),
        },
        "tool_calls": [call],
        "answer": contract["section_markdown"],
        "markdown_response": None,
        "sources": [market.source],
        "general_view_contract": contract,
        "general_view_ready": contract["mode"] == "general_only",
    }


def _data_path_source(market: GeneralMarket) -> str:
    return "jw-market-direct-mart" if market.selected_data_path == "direct_mart" else "jw-market-backend-api"


def _attach_data_path_trace(call: dict[str, Any], market: GeneralMarket) -> None:
    trace = call.get("qa_trace")
    if not isinstance(trace, dict):
        return
    trace["selected_data_path"] = market.selected_data_path
    if market.fallback_reason is not None:
        trace["fallback_reason"] = market.fallback_reason


def _combined_source(sources: set[str]) -> str:
    if not sources:
        return "unavailable"
    if len(sources) == 1:
        return next(iter(sources))
    return "mixed"


def _public_atc4_source(source: str) -> str:
    if source == "catalog_definition":
        return "catalog_definition"
    if source == "membership_db":
        return "brand_membership"
    return "fallback"


def _catalog_definition_failure_reason(error: BaseException) -> str:
    if isinstance(error, CatalogDefinitionLoadError):
        return error.reason_code
    if isinstance(error, OSError):
        return "catalog_db_unreachable"
    return "catalog_parse_error"


def _sanitize_catalog_exception(error: BaseException) -> str:
    message = _CONNECTION_STRING_RE.sub("[REDACTED_CONNECTION_STRING]", str(error))
    return _SECRET_ASSIGNMENT_RE.sub(r"\1=[REDACTED]", message)


def _attach_selection_trace(call: dict[str, Any], contract: dict[str, Any]) -> None:
    trace = call.get("qa_trace")
    if not isinstance(trace, dict):
        return
    for field_name in (
        "input_market",
        "atc4_source",
        "candidate_atc4_codes",
        "member_brand_count",
        "excluded_atc4_count",
        "reduction_reason",
    ):
        trace[field_name] = contract.get(field_name)


def _multi_result(
    question: str,
    markets: tuple[GeneralMarket, ...],
    contract: dict[str, Any],
    *,
    started_at: datetime,
) -> dict[str, Any]:
    tool_calls: list[dict[str, Any]] = []
    for market, section in zip(markets, contract["atc4_sections"], strict=True):
        call = {
            "source": _data_path_source(market),
            "tool": "general_view_dynamic_market",
            "summary_text": f"ATC4 {market.atc4_code} 일반뷰를 독립 조회했습니다.",
            "render_data": dict(section),
        }
        attach_tool_qa_trace(
            call,
            started_at=started_at,
            status="ok",
            row_count=max(1, len(market.top_brands)),
            data_as_of=str(section.get("period") or "") or None,
            cache_hit=False,
        )
        _attach_data_path_trace(call, market)
        _attach_selection_trace(call, contract)
        tool_calls.append(call)
    return {
        "question": question,
        "resolution": {"atc4_codes": list(contract["atc4_codes"])},
        "decomposition": [
            {
                "intent": "general_view_market_metric",
                "view_type": "general_view",
                "atc4_code": market.atc4_code,
            }
            for market in markets
        ],
        "router_diagnostics": {
            "mode": "general_view",
            "reason": "general_view_dynamic_market_split",
            "deterministic": True,
            "general_view": True,
            "selected_data_path": contract.get("selected_data_path"),
            "membership_source": contract.get("membership_source"),
            "input_market": contract.get("input_market"),
            "atc4_source": contract.get("atc4_source"),
            "candidate_atc4_codes": contract.get("candidate_atc4_codes"),
            "member_brand_count": contract.get("member_brand_count"),
            "excluded_atc4_count": contract.get("excluded_atc4_count"),
            "reduction_reason": contract.get("reduction_reason"),
        },
        "tool_calls": tool_calls,
        "answer": contract["section_markdown"],
        "markdown_response": None,
        "sources": sorted({market.source for market in markets}),
        "general_view_contract": contract,
        "general_view_ready": contract["mode"] == "general_only",
    }


def _unavailable_result(
    question: str,
    reason: str,
    *,
    dual: bool,
    started_at: datetime,
    selection_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    contract.update(selection_trace or {})
    call = {"source": "jw-market-backend-api", "tool": "general_view_unavailable", "render_data": contract}
    attach_tool_qa_trace(
        call,
        started_at=started_at,
        status="no_data",
        row_count=0,
        cache_hit=False,
    )
    _attach_selection_trace(call, contract)
    return {
        "question": question,
        "decomposition": [{"intent": "general_view_unavailable", "view_type": "general_view"}],
        "router_diagnostics": {
            "mode": "general_view",
            "reason": "general_view_unavailable",
            "deterministic": True,
            "general_view": True,
            "unavailable": True,
            "input_market": contract.get("input_market"),
            "atc4_source": contract.get("atc4_source"),
            "candidate_atc4_codes": contract.get("candidate_atc4_codes"),
            "member_brand_count": contract.get("member_brand_count"),
            "excluded_atc4_count": contract.get("excluded_atc4_count"),
            "reduction_reason": contract.get("reduction_reason"),
        },
        "tool_calls": [call],
        "answer": text,
        "markdown_response": None,
        "sources": [],
        "general_view_contract": contract,
        "general_view_ready": not dual,
    }


def _render_section(
    market: GeneralMarket,
    *,
    other_candidates: list[str],
    compact: bool,
    window: tuple[str, float] | None,
    question: str,
    member_fields: dict[str, Any],
) -> str:
    if member_fields:
        blocks = ["## 일반뷰 (ATC4)", market_members_md(member_fields)]
        if not compact:
            blocks.append(f"점유율 분모: ATC4 {market.atc4_code} 시장 전체 {market.measure}")
        return "\n\n".join(blocks)
    lines = ["## 일반뷰 (ATC4)", "", f"- 시장: {_public_atc4_market_label(market)}"]
    if _asks_hhi(question):
        lines.append(f"- ATC4: {market.atc4_code}")
        if market.hhi_recent is not None:
            lines.append(f"- HHI ({market.period}): {market.hhi_recent:,.4f}")
    if window is not None:
        label, value = window
        lines.append(f"- 시장 규모 ({label}): {_format_value(value, market.unit)}")
    elif market.market_size is not None:
        lines.append(f"- 시장 규모 ({market.period}): {_format_value(market.market_size, market.unit)}")
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


def _requested_market_window(question: str, market: GeneralMarket) -> tuple[str, float] | None:
    if not re.search(r"최근\s*(?:1년|12\s*개월)", question):
        return None
    normalized_source = re.sub(r"[_\s]+", " ", market.source.strip().upper())
    if normalized_source in {"IQVIA", "IQVIA NSA"}:
        points = tuple(
            (period, value)
            for period, value in market.market_size_series
            if re.fullmatch(r"\d{4}-Q[1-4]", period)
        )
        if len(points) < 4:
            raise GeneralViewBackendError("IQVIA 소스는 분기 단위이며 최근 4분기 데이터가 부족합니다")
        selected = points[-4:]
        return f"최근 4분기 합계 {selected[0][0]}~{selected[-1][0]}", sum(value for _, value in selected)
    points = tuple((period, value) for period, value in market.market_size_series if re.fullmatch(r"\d{4}-\d{2}", period))
    if len(points) < 12:
        raise GeneralViewBackendError("UBIST 소스는 월 단위이며 최근 12개월 데이터가 부족합니다")
    selected = points[-12:]
    return f"최근 12개월 합계 {selected[0][0]}~{selected[-1][0]}", sum(value for _, value in selected)


def _requested_member_rows(market: GeneralMarket, question: str) -> tuple[TopBrand, ...]:
    population = market.member_brands or market.top_brands
    selected = population[5:] if asks_other_market_members(question) else population
    applied = requested_market_member_limit(question).applied
    return selected if applied is None else selected[:applied]


def _member_contract_fields(market: GeneralMarket, question: str) -> dict[str, Any]:
    population = market.member_brands or market.top_brands
    other_only = asks_other_market_members(question)
    limit = requested_market_member_limit(question)
    members = _requested_member_rows(market, question)
    other_rows = population[5:]
    other_share: float | None
    if not other_rows:
        other_share = 0.0
    elif all(row.share_pct is not None for row in other_rows):
        other_share = sum(float(row.share_pct) for row in other_rows if row.share_pct is not None)
    else:
        other_share = None
    fields: dict[str, Any] = {
        "status": "ok",
        "market": market.atc4_code,
        "market_id": market.atc4_code,
        "market_name": _public_atc4_market_label(market),
        "scope": "market",
        "scope_label": "시장 구성 브랜드",
        "level": "Brand",
        "view_type": "general_view",
        "period": market.period,
        "anchor_brand": market.brand,
        "member_brands": [row.brand for row in members],
        "displayed_brand_count": len(members),
        "total_brands_in_market": len(population),
        "other_members_only": other_only,
        "other_member_count": len(other_rows),
        "sort": "sales_desc",
        "limit": len(members) if limit.applied is None else limit.applied,
        "display_limit": len(members) if limit.applied is None else limit.applied,
    }
    if limit.requested is not None and limit.requested > 0:
        fields["requested_limit"] = limit.requested
        fields["limit_capped"] = False
    elif limit.all_requested:
        fields["requested_all"] = True
        fields["limit_capped"] = False
    if other_only and other_share is not None:
        fields["other_total_share_pct"] = other_share
    return fields


def _format_value(value: float, unit: str) -> str:
    if unit.upper() == "KRW":
        return f"{value / 100_000_000:,.1f}억원"
    return f"{value:,.2f} {unit}".strip()


def _public_atc4_market_label(market: GeneralMarket) -> str:
    description = market.atc4_description.strip()
    if re.search(r"[가-힣]", description):
        return description
    return f"ATC4 {market.atc4_code}"


def _normalize(question: str) -> str:
    return re.sub(r"\s+", "", question).lower()


def _has_explicit_general_signal(normalized: str) -> bool:
    return bool(_ATC4_PATTERN.search(normalized)) or any(
        token in normalized for token in ("일반뷰", "일반view", "atc4", "atc기준")
    )


def _has_explicit_strategic_view_signal(normalized: str) -> bool:
    return bool(_STRATEGIC_MARKET_ID_PATTERN.search(normalized)) or any(
        token in normalized
        for token in (
            "전략뷰",
            "전략view",
            "시장조망",
            "market_landscape",
            "경쟁군",
            "경쟁시장",
            "competitive_dynamics",
            "cd기준",
            "cr5",
            "집중도",
        )
    )


def _asks_hhi(question: str) -> bool:
    return "hhi" in _normalize(question)


def _asks_market_metric(normalized: str) -> bool:
    return any(token in normalized for token in ("시장점유율", "시장규모", "시장순위", "시장에서", "같은시장"))


def _asks_general_brand_metric(normalized: str) -> bool:
    return any(token in normalized for token in ("매출", "실적", "점유율", "추이", "순위", "시장"))


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
    return _requested_source(question) or "ubist"


def _requested_source(question: str) -> str | None:
    if _IQVIA_SOURCE_PATTERN.search(question):
        return "iqvia"
    if _SOURCE_PATTERN.search(question):
        return "ubist"
    return None


def _atc4_code(question: str) -> str | None:
    match = _ATC4_PATTERN.search(question)
    return match.group(1).upper() if match else None


def _brand_hint(question: str) -> str:
    market_scope = detect_market_scope_intent(question)
    if market_scope is not None and market_scope.brand_hint:
        return market_scope.brand_hint
    text = _SOURCE_PATTERN.sub(" ", question)
    text = _ATC4_PATTERN.sub(" ", text)
    text = re.split(
        r"시장|점유율|매출|실적|최근|추이|순위|규모|top\s*\d*",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    text = re.sub(r"일반뷰|전략뷰|ATC4?|기준|으로|에서|의", " ", text, flags=re.IGNORECASE)
    hint = re.sub(r"\s+", " ", text).strip(" ?")
    return re.sub(r"(?:은|는|이|가|을|를)$", "", hint)
