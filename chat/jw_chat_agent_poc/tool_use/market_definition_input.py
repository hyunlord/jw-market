from __future__ import annotations

from collections.abc import Mapping

from jw_chat_agent_poc.tool_use.market_scope_contract import (
    GeneralCompositeUnavailableError,
    InvalidMarketLabelError,
)


def definition_axes(arguments: Mapping[str, object]) -> tuple[str, str, str]:
    view = _text(arguments.get("view"))
    atc4 = _text(arguments.get("atc4")).upper()
    market_id = _text(arguments.get("market_id"))
    scope = arguments.get("scope")
    if scope is not None:
        view, atc4, market_id = _scope_axes(
            scope,
            view=view,
            atc4=atc4,
            market_id=market_id,
        )
    if view == "general":
        if not atc4 or market_id:
            raise InvalidMarketLabelError("general definition requires ATC4 and no strategic market")
        return view, atc4, ""
    if view in ("market_landscape", "competitive_dynamics"):
        return _strategic_axes(view, atc4, market_id)
    if view:
        raise InvalidMarketLabelError(f"unknown definition view: {view}")
    if atc4 and market_id:
        raise InvalidMarketLabelError("general and strategic definition axes conflict")
    if atc4:
        return "general", atc4, ""
    if market_id:
        inferred_view = (
            "competitive_dynamics" if market_id.startswith("cd_") else "market_landscape"
        )
        return inferred_view, "", market_id
    return "", "", ""


def _scope_axes(
    scope: object,
    *,
    view: str,
    atc4: str,
    market_id: str,
) -> tuple[str, str, str]:
    if not isinstance(scope, Mapping):
        raise InvalidMarketLabelError("scope must be an object")
    scope_kind = _text(scope.get("kind"))
    scope_market_id = _text(scope.get("market_id"))
    scope_atc4 = _scope_atc4(scope.get("atc4"))
    if scope_kind == "general_composite" or scope.get("filters"):
        raise GeneralCompositeUnavailableError("filtered definition lookup remains fail-closed")
    if scope_kind == "general_atc4":
        if len(scope_atc4) != 1 or scope_market_id:
            raise InvalidMarketLabelError("general_atc4 requires exactly one ATC4")
        if view not in ("", "general") or market_id or (atc4 and atc4 != scope_atc4[0]):
            raise InvalidMarketLabelError("general and strategic definition axes conflict")
        return "general", scope_atc4[0], ""
    if scope_kind != "strategic":
        raise InvalidMarketLabelError(f"unknown definition scope: {scope_kind}")
    if not scope_market_id or scope_atc4:
        raise InvalidMarketLabelError("strategic scope requires only market_id")
    if atc4 or (market_id and market_id != scope_market_id):
        raise InvalidMarketLabelError("general and strategic definition axes conflict")
    return view, "", scope_market_id


def _strategic_axes(view: str, atc4: str, market_id: str) -> tuple[str, str, str]:
    if atc4 or not market_id:
        raise InvalidMarketLabelError("strategic definition requires market_id and no ATC4")
    if view == "market_landscape" and not market_id.startswith("ml_"):
        raise InvalidMarketLabelError("market_landscape requires an ml_ market identifier")
    if view == "competitive_dynamics" and not market_id.startswith(("ml_", "cd_")):
        raise InvalidMarketLabelError("competitive_dynamics requires an ml_ or cd_ identifier")
    return view, "", market_id


def _scope_atc4(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(_text(item).upper() for item in value if _text(item))


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""
