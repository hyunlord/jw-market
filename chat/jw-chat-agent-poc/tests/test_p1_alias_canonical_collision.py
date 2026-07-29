"""P-1 — runtime alias 병합이 다른 브랜드의 키를 가리지 않는다.

적재 가드는 alias_name 을 저장된 brand_KEY 와 NFKC+strip 기준으로 비교한다. 이 인덱스는
공백을 전부 지우고 casefold 한 뒤 ★표시명을 키로 쓴다. 그래서 가드를 통과한 alias 가
여기서는 다른 브랜드의 키에 얹힐 수 있고, 오늘 답변되는 이름이 모호해진다.
라이브 brand_alias 1,688행 중 264행이 그 상태였다(측정값).

자기 자신의 키로 접히는 행(1,424)은 표기만 늘리므로 그대로 둔다.
"""

from jw_chat_agent_poc.resolver.brand_resolver import BrandResolver

CACHE = (
    {"brand": "가스터", "market_id": "strategy_x", "market_name": "위궤양"},
    {"brand": "가스터 주사", "market_id": "strategy_x", "market_name": "위궤양"},
    {"brand": "리바로", "market_id": "strategy_y", "market_name": "고지혈증"},
)


def _resolver():
    resolver = BrandResolver()
    resolver._mode = "cache"
    return resolver


def _index(items):
    normalize = BrandResolver._normalize
    grouped: dict[str, list[str]] = {}
    for item in items:
        for raw in (item["canonical_brand"], *item.get("aliases", [])):
            key = normalize(str(raw).strip())
            if not key:
                continue
            owners = grouped.setdefault(key, [])
            if item["canonical_brand"] not in owners:
                owners.append(item["canonical_brand"])
    return grouped


def test_alias_that_is_another_brands_key_is_not_merged() -> None:
    # Given: '가스터 주사' 는 그 자체로 canonical 브랜드인데 '가스터' 의 alias 로도 적재돼 있다.
    aliases = ({"alias_name": "가스터 주사", "brand_key": "가스터"},)

    items = _resolver()._assemble_items(CACHE, (), (), aliases)

    # Then: '가스터주사' 키의 주인은 하나뿐이다(모호해지지 않는다).
    assert _index(items)["가스터주사"] == ["가스터 주사"]
    gaster = next(item for item in items if item["canonical_brand"] == "가스터")
    assert "가스터 주사" not in gaster["aliases"]
    assert "가스터주사" not in gaster.get("_runtime_alias_keys", [])


def test_alias_that_folds_onto_its_own_key_is_kept() -> None:
    # Given: 내부 공백만 다른 alias. 접으면 자기 키와 같아지므로 새 주인을 만들지 않는다.
    # (선행 .strip() 이 후행 공백은 이미 지우므로, 내부 공백형으로 구분한다.)
    aliases = ({"alias_name": "가 스 터", "brand_key": "가스터"},)

    items = _resolver()._assemble_items(CACHE, (), (), aliases)

    gaster = next(item for item in items if item["canonical_brand"] == "가스터")
    assert "가 스 터" in gaster["aliases"]
    assert _index(items)["가스터"] == ["가스터"]


def test_alias_pointing_at_a_sibling_product_cannot_borrow_its_identity() -> None:
    # IDCK3 커밋 메시지가 스스로 경계한 위험: 리바로 <-> 리바로젯 같은 형제 제품 혼동.
    # '리바로젯' 이 canonical 로 존재하지 않는 구성에서는 alias 로 붙는다(대조군).
    cache_without_sibling = tuple(CACHE)
    aliases = ({"alias_name": "리바로젯", "brand_key": "리바로"},)
    items = _resolver()._assemble_items(cache_without_sibling, (), (), aliases)
    livalo = next(item for item in items if item["canonical_brand"] == "리바로")
    assert "리바로젯" in livalo["aliases"]

    # 그러나 '리바로젯' 이 canonical 브랜드로 존재하면 그 정체성을 빌려올 수 없다.
    cache_with_sibling = cache_without_sibling + (
        {"brand": "리바로젯", "market_id": "strategy_y", "market_name": "고지혈증"},
    )
    items = _resolver()._assemble_items(cache_with_sibling, (), (), aliases)
    livalo = next(item for item in items if item["canonical_brand"] == "리바로")
    assert "리바로젯" not in livalo["aliases"]
    assert _index(items)["리바로젯"] == ["리바로젯"]


def test_unrelated_alias_still_merges() -> None:
    # 어떤 canonical 과도 충돌하지 않는 alias 는 그대로 붙는다(P-1 이 과차단하지 않는다).
    aliases = ({"alias_name": "가스터정20밀리그램", "brand_key": "가스터"},)

    items = _resolver()._assemble_items(CACHE, (), (), aliases)

    gaster = next(item for item in items if item["canonical_brand"] == "가스터")
    assert "가스터정20밀리그램" in gaster["aliases"]
    assert "가스터정20밀리그램" in gaster["_runtime_alias_keys"]
