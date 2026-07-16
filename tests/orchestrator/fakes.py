"""Shared fakes for orchestrator tests."""

EPOCH = "a" * 64


class FakeProbe:
    def __init__(self, epoch: str = EPOCH, new_brands: dict | None = None, available: bool = True) -> None:
        self.epoch = epoch
        self.new_brands = new_brands or {}
        self.available = available

    def current_epoch(self) -> str:
        if not self.available:
            raise RuntimeError("db unreachable")
        return self.epoch

    def new_brand_keys(self, universe_sql: str, covered_sql: str) -> list[str]:
        return list(self.new_brands.get(universe_sql, []))
