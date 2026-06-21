from __future__ import annotations

from .models import CsdPresence, FilterOption, GroupMember, JsonValue, MarketGroup, MarketGroupModel, SourceMarket


def build_market_group_model() -> MarketGroupModel:
    """Build the five PL-approved market groups without mutating source markets."""
    groups = {
        group.group_id: group
        for group in (
            _livalo_family(),
            _livalo_high_family(),
            _thrupas_family(),
            _ferinject_family(),
            _winuf_family(),
        )
    }
    return MarketGroupModel(groups=groups)


def filter_options_for_brand(model: MarketGroupModel, iqvia_en: str) -> list[FilterOption]:
    """Expand selected brand into source-market and group-union filter options."""
    group = _group_for_brand(model, iqvia_en)
    if group is None:
        return []
    member = _member_for_brand(group, iqvia_en)
    if member is None or member.status is CsdPresence.ABSENT_IN_CSD:
        return []
    source_market = member.source_markets[0]
    return [
        FilterOption(
            option_id=f"source:{source_market}",
            label=member.kr_brand,
            option_type="source_market",
            source_markets=member.source_markets,
            atc4_set=member.atc4,
        ),
        FilterOption(
            option_id=f"group:{group.group_id}",
            label=group.filter_label,
            option_type="group_union",
            source_markets=tuple(market.source_market for market in group.source_markets),
            atc4_set=group.atc4_set,
        ),
    ]


def model_to_json(model: MarketGroupModel) -> dict[str, JsonValue]:
    """Serialize the group model into a human-reviewable JSON shape."""
    return {
        "rule": "source CSD market is preserved; group is additive display/aggregation metadata",
        "group_count": len(model.groups),
        "groups": [_group_to_json(group) for group in model.groups.values()],
    }


def _group_for_brand(model: MarketGroupModel, iqvia_en: str) -> MarketGroup | None:
    """Return the group containing an IQVIA English anchor."""
    for group in model.groups.values():
        if _member_for_brand(group, iqvia_en) is not None:
            return group
    return None


def _member_for_brand(group: MarketGroup, iqvia_en: str) -> GroupMember | None:
    """Return the member matching an IQVIA English anchor."""
    for member in group.members:
        if member.iqvia_en == iqvia_en:
            return member
    return None


def _group_to_json(group: MarketGroup) -> dict[str, JsonValue]:
    """Serialize one group while keeping source markets explicit."""
    return {
        "group_id": group.group_id,
        "label": group.label,
        "filter_label": group.filter_label,
        "atc4_set": list(group.atc4_set),
        "source_markets": [
            {"source_market": item.source_market, "atc4": list(item.atc4), "members": list(item.members)}
            for item in group.source_markets
        ],
        "members": [
            {
                "kr_brand": member.kr_brand,
                "iqvia_en": member.iqvia_en,
                "member_status": member.status.value,
                "source_markets": list(member.source_markets),
                "atc4": list(member.atc4),
            }
            for member in group.members
        ],
    }


def _present(kr_brand: str, iqvia_en: str, source_market: str, atc4: str) -> GroupMember:
    """Create a present CSD member."""
    return GroupMember(kr_brand, iqvia_en, CsdPresence.PRESENT, (source_market,), (atc4,))


def _absent(kr_brand: str) -> GroupMember:
    """Create an absent CSD member without an invented source market."""
    return GroupMember(kr_brand, None, CsdPresence.ABSENT_IN_CSD, (), ())


def _livalo_family() -> MarketGroup:
    """Define the multi-ATC4 리바로 market group."""
    members = (
        _present("리바로", "LIVALO", "LIVALO Market", "C10A1"),
        _present("리바로젯", "LIVALOZET", "LIVALOZET Market", "C10C0"),
    )
    return MarketGroup(
        "livalo_family",
        "리바로 시장군",
        "리바로+리바로젯",
        members,
        (
            SourceMarket("LIVALO Market", ("C10A1",), ("LIVALO",)),
            SourceMarket("LIVALOZET Market", ("C10C0",), ("LIVALOZET",)),
        ),
        ("C10A1", "C10C0"),
    )


def _livalo_high_family() -> MarketGroup:
    """Define the 리바로하이 market group with absent member preserved."""
    members = (_absent("리바로하이"), _present("리바로브이", "LIVALO V", "LIVALO V Market", "C11A1"))
    return MarketGroup(
        "livalo_high_family",
        "리바로하이 시장군",
        "리바로하이+리바로브이",
        members,
        (SourceMarket("LIVALO V Market", ("C11A1",), ("LIVALO V",)),),
        ("C11A1",),
    )


def _thrupas_family() -> MarketGroup:
    """Define the 트루패스 market group with absent members preserved."""
    members = (_present("트루패스", "THRUPAS", "TURUPAS Market", "G04C2"), _absent("피나스타"), _absent("제이다트"))
    return MarketGroup(
        "thrupas_family",
        "트루패스 시장군",
        "트루패스+피나스타+제이다트",
        members,
        (SourceMarket("TURUPAS Market", ("G04C2",), ("THRUPAS",)),),
        ("G04C2",),
    )


def _ferinject_family() -> MarketGroup:
    """Define the 페린젝트 market group."""
    members = (
        _present("페린젝트", "FERINJECT", "FERINJECT Market", "B03A1"),
        _present("베노훼럼", "VENOFERRUM", "FERINJECT Market", "B03A1"),
    )
    return MarketGroup(
        "ferinject_family",
        "페린젝트 시장군",
        "페린젝트+베노훼럼",
        members,
        (SourceMarket("FERINJECT Market", ("B03A1",), ("FERINJECT", "VENOFERRUM")),),
        ("B03A1",),
    )


def _winuf_family() -> MarketGroup:
    """Define the 위너프 market group."""
    members = (
        _present("위너프", "WINUF", "WINUF Market", "K01D2"),
        _present("위너프", "WINUF PERI", "WINUF Market", "K01D2"),
        _present("위너프에이플러스", "WINUF A PLUS", "WINUF Market", "K01D2"),
    )
    return MarketGroup(
        "winuf_family",
        "위너프 시장군",
        "위너프+위너프에이플러스",
        members,
        (SourceMarket("WINUF Market", ("K01D2",), ("WINUF", "WINUF PERI", "WINUF A PLUS")),),
        ("K01D2",),
    )
