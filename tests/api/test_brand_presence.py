from __future__ import annotations

from collections.abc import Iterator

from pipeline.scripts.api import brand_presence


def test_brand_exists_prefers_indexed_brand_key(monkeypatch) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_fetch_one(sql: str, params: tuple[str, ...]) -> dict[str, int] | None:
        calls.append((sql, params))
        return {"found": 1}

    monkeypatch.setattr(brand_presence.db, "fetch_one", fake_fetch_one)

    assert brand_presence.brand_exists(" 리바로 ") is True
    assert len(calls) == 1
    assert "brand_key = %s" in calls[0][0]
    assert "brand_name" not in calls[0][0]
    assert calls[0][1] == ("리바로",)


def test_brand_exists_falls_back_to_exact_brand_name(monkeypatch) -> None:
    results: Iterator[dict[str, int] | None] = iter([None, {"found": 1}])
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_fetch_one(sql: str, params: tuple[str, ...]) -> dict[str, int] | None:
        calls.append((sql, params))
        return next(results)

    monkeypatch.setattr(brand_presence.db, "fetch_one", fake_fetch_one)

    assert brand_presence.brand_exists("브랜드명") is True
    assert len(calls) == 2
    assert "brand_key = %s" in calls[0][0]
    assert "brand_name = %s" in calls[1][0]
    assert calls[0][1] == calls[1][1] == ("브랜드명",)


def test_brand_exists_returns_false_for_blank_or_missing_brand(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch_one(sql: str, _params: tuple[str, ...]) -> None:
        calls.append(sql)
        return None

    monkeypatch.setattr(brand_presence.db, "fetch_one", fake_fetch_one)

    assert brand_presence.brand_exists(" ") is False
    assert brand_presence.brand_exists("없는브랜드") is False
    assert len(calls) == 2


def test_negative_brand_cache_expires_and_discards_entries() -> None:
    now = [100.0]
    cache = brand_presence.NegativeBrandCache(
        ttl_seconds=60.0,
        max_entries=2,
        clock=lambda: now[0],
    )

    cache.remember("없는브랜드")
    assert cache.contains("없는브랜드") is True

    now[0] = 160.0
    assert cache.contains("없는브랜드") is False

    cache.remember("돌아온브랜드")
    cache.discard("돌아온브랜드")
    assert cache.contains("돌아온브랜드") is False


def test_negative_brand_cache_is_bounded_by_oldest_entry() -> None:
    now = [100.0]
    cache = brand_presence.NegativeBrandCache(
        ttl_seconds=60.0,
        max_entries=2,
        clock=lambda: now[0],
    )

    cache.remember("첫째")
    now[0] += 1.0
    cache.remember("둘째")
    now[0] += 1.0
    cache.remember("셋째")

    assert cache.contains("첫째") is False
    assert cache.contains("둘째") is True
    assert cache.contains("셋째") is True
