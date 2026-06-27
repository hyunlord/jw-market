from __future__ import annotations

import json

import pytest

from pipeline.scripts.deploy import dynamic_serving_import as importer


def test_source_guard_allows_only_local_jw_mart() -> None:
    endpoint = importer.DbEndpoint(host="127.0.0.1", port="3308", user="root", password="placeholder")

    importer._guard_source_endpoint("jw_mart", endpoint)

    with pytest.raises(RuntimeError, match="non-local source"):
        importer._guard_source_endpoint(
            "jw_mart",
            importer.DbEndpoint(host="llmops-mariadb-service.llmops.svc.cluster.local", port="3306", user="root", password="placeholder"),
        )
    with pytest.raises(RuntimeError, match="dynamic serving source"):
        importer._guard_source_endpoint("jw_mart_d1_stage_20260625_173115", endpoint)


def test_target_guard_requires_explicit_test2_schema_and_host() -> None:
    endpoint = importer.DbEndpoint(
        host="llmops-mariadb-service.llmops.svc.cluster.local",
        port="3306",
        user="llmops",
        password="placeholder",
    )

    importer._guard_target_endpoint(
        "jw_mart_d1_stage_20260625_173115",
        endpoint,
        allow_test2_serving_target=True,
    )

    with pytest.raises(RuntimeError, match="protected target"):
        importer._guard_target_endpoint("jw_mart", endpoint, allow_test2_serving_target=True)
    with pytest.raises(RuntimeError, match="required"):
        importer._guard_target_endpoint(
            "jw_mart_d1_stage_20260625_173115",
            endpoint,
            allow_test2_serving_target=False,
        )
    with pytest.raises(RuntimeError, match="only permits"):
        importer._guard_target_endpoint("jw_mart_test_stage2", endpoint, allow_test2_serving_target=True)
    with pytest.raises(RuntimeError, match="refusing target host"):
        importer._guard_target_endpoint(
            "jw_mart_d1_stage_20260625_173115",
            importer.DbEndpoint(host="127.0.0.1", port="3306", user="root", password="placeholder"),
            allow_test2_serving_target=True,
        )
    importer._guard_target_endpoint(
        "jw_mart_d1_stage_20260625_173115",
        importer.DbEndpoint(host="127.0.0.1", port="13306", user="root", password="placeholder"),
        allow_test2_serving_target=True,
        target_via_port_forward=True,
    )


def test_import_verifies_manifest_counts(tmp_path, monkeypatch) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"run_id": "run", "tables": [{"table": "mart_general_brand_metric", "rows": 2}]}),
        encoding="utf-8",
    )
    output_path = tmp_path / "imported.json"
    calls: list[str] = []

    monkeypatch.setattr(importer, "_endpoint_from_env", lambda env_file: importer.DbEndpoint("llmops-mariadb-service", "3306", "u", "p"))
    monkeypatch.setattr(importer, "_existing_tables", lambda endpoint, db_name, tables: ())
    monkeypatch.setattr(importer, "_restore_dump", lambda endpoint, target_db, dump_path: calls.append(target_db))
    monkeypatch.setattr(
        importer,
        "_fetch_counts",
        lambda endpoint, target_db, tables: (importer.TableCount("mart_general_brand_metric", 2),),
    )

    summary = importer.import_test2_serving(
        target_db="jw_mart_d1_stage_20260625_173115",
        dump_path=tmp_path / "dump.sql.gz",
        manifest_path=manifest_path,
        output_manifest_path=output_path,
        env_file=None,
        backup_target_dump=None,
        allow_test2_serving_target=True,
        target_via_port_forward=False,
        include_cache=False,
    )

    assert calls == ["jw_mart_d1_stage_20260625_173115"]
    assert summary["verification"] == [{"table": "mart_general_brand_metric", "rows": 2}]
    assert json.loads(output_path.read_text(encoding="utf-8"))["target_db"] == "jw_mart_d1_stage_20260625_173115"
