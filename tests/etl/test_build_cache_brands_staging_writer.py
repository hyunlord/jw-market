from pipeline.scripts.etl import build_cache_brands, cache_build_common


class _Cursor:
    def __init__(self):
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params):
        self.executed.append((sql, params))


class _Connection:
    def __init__(self):
        self.cursor_value = _Cursor()
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def close(self):
        self.closed = True


def test_staging_target_uses_insert(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        build_cache_brands,
        "insert_rows",
        lambda *_args, **_kwargs: calls.append("insert"),
    )
    monkeypatch.setattr(
        build_cache_brands,
        "replace_rows",
        lambda *_args, **_kwargs: calls.append("replace"),
    )

    build_cache_brands._write_rows(
        "jw_mart.cache_brands_staging",
        ["query_key"],
        [{"query_key": "default"}],
    )

    assert calls == ["insert"]


def test_live_target_keeps_replace(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        build_cache_brands,
        "insert_rows",
        lambda *_args, **_kwargs: calls.append("insert"),
    )
    monkeypatch.setattr(
        build_cache_brands,
        "replace_rows",
        lambda *_args, **_kwargs: calls.append("replace"),
    )

    build_cache_brands._write_rows(
        "cache_brands",
        ["query_key"],
        [{"query_key": "default"}],
    )

    assert calls == ["replace"]


def test_insert_rows_uses_insert_without_replace(monkeypatch):
    connection = _Connection()
    monkeypatch.setattr(cache_build_common, "mariadb_connect", lambda: connection)

    cache_build_common.insert_rows(
        "jw_mart.cache_brands_staging",
        ["query_key", "payload_size"],
        [{"query_key": "default", "payload_size": 25}],
    )

    assert connection.cursor_value.executed == [
        (
            "INSERT INTO `jw_mart`.`cache_brands_staging` "
            "(`query_key`, `payload_size`) VALUES (%s, %s)",
            ("default", 25),
        )
    ]
    assert connection.closed is True
