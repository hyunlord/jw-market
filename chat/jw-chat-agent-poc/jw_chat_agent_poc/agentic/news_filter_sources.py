from __future__ import annotations

from typing import Final


SOURCE_ALIASES: Final[dict[str, str]] = {
    "약업신문": "약업신문",
    "데일리팜": "데일리팜",
    "약사공론": "약사공론",
    "메디칼타임즈": "메디칼타임즈",
    "메디컬타임즈": "메디칼타임즈",
    "청년의사": "청년의사",
    "의학신문": "의학신문",
    "메디파나뉴스": "메디파나뉴스",
    "히트뉴스": "히트뉴스",
    "바이오스펙테이터": "바이오스펙테이터",
    "라포르시안": "라포르시안",
    "헬스코리아뉴스": "헬스코리아뉴스",
    "메디소비자뉴스": "메디소비자뉴스",
}


def normalise_news_source(value: str) -> str | None:
    return SOURCE_ALIASES.get(value.strip())
