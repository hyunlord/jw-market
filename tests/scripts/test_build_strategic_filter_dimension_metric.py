from __future__ import annotations

import json

from pipeline.scripts import build_strategic_filter_dimension_metric as cli


def test_strategic_sidecar_cli_drops_target_after_success_when_requested(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    manifest_path = tmp_path / "manifest.json"

    class Conn:
        def close(self) -> None:
            calls.append(("close", "conn"))

    def fake_connect() -> Conn:
        calls.append(("connect", "db"))
        return Conn()

    def fake_build_strategic_sidecar(*, source_db: str, target_db: str, connection: object, replace_table: bool) -> dict[str, object]:
        calls.append(("build", source_db, target_db, connection, replace_table))
        return {"target_db": target_db, "rows_inserted": 10}

    monkeypatch.setattr(cli, "mariadb_connect", fake_connect)
    monkeypatch.setattr(cli, "build_strategic_sidecar", fake_build_strategic_sidecar)
    monkeypatch.setattr(cli, "drop_stage_schema", lambda conn, target_db: calls.append(("drop", target_db)))

    cli.main(
        [
            "--source-db",
            "jw_mart",
            "--target-db",
            "jw_mart_d2_strategic_dim_stage_20260630_010203",
            "--manifest",
            str(manifest_path),
            "--drop-target-after-success",
        ]
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["cleanup"] == {"target_db_dropped": True}
    assert ("drop", "jw_mart_d2_strategic_dim_stage_20260630_010203") in calls
