from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook import ubist_mart_activation as activation


FULL_SHA = "a" * 40
IMAGE = "registry.example/jw-pipeline-orchestrator@sha256:" + ("b" * 64)


def _journal(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "target_db": "jw_mart_ingest_shadow_test",
                "tables": list(activation.GENERAL_TABLES),
            }
        ),
        encoding="utf-8",
    )
    return path


def _action() -> object:
    return type(
        "Action",
        (),
        {
            "table": "mart_general_brand_metric",
            "backup_table": "mart_general_brand_metric__old_run1",
        },
    )()


def test_publish_records_running_image_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("APP_VERSION", FULL_SHA)
    monkeypatch.setenv("INGEST_JOB_IMAGE", IMAGE)
    monkeypatch.setattr(
        activation, "require_completed_post_gate", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        activation,
        "publish_table_group_atomically",
        lambda *_args, **_kwargs: (_action(),),
    )
    monkeypatch.setattr(
        activation, "record_mysql_component", lambda *_args, **_kwargs: None
    )
    journal = _journal(tmp_path / "activation.json")
    target = activation.MartActivation(
        "jw_mart", "jw_mart_ingest_shadow_test", "jw_mart_ingest_shadow_build_run1"
    )

    activation.publish_shadow(
        object(),
        target,
        run_id="run1",
        epoch="2026-07",
        ingest_run_id="ingest-run1",
        activation_journal=journal,
    )

    provenance = json.loads(journal.read_text(encoding="utf-8"))[
        "publication_provenance"
    ]
    assert provenance["builder_commit"] == FULL_SHA
    assert provenance["image_digest"] == IMAGE
    assert provenance["target_db"] == target.target_db
    assert provenance["tables"] == list(activation.GENERAL_TABLES)
    assert provenance["published_at_utc"].endswith("+00:00")


def test_publish_rejects_missing_app_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.setenv("INGEST_JOB_IMAGE", IMAGE)

    with pytest.raises(RuntimeError, match="APP_VERSION is required"):
        activation.build_publication_provenance(
            target_db="jw_mart_ingest_shadow_test",
            tables=activation.GENERAL_TABLES,
        )


def test_publish_rejects_short_app_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_VERSION", "a" * 8)
    monkeypatch.setenv("INGEST_JOB_IMAGE", IMAGE)

    with pytest.raises(RuntimeError, match="full 40-character"):
        activation.build_publication_provenance(
            target_db="jw_mart_ingest_shadow_test",
            tables=activation.GENERAL_TABLES,
        )


def test_publish_rejects_builder_commit_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_VERSION", FULL_SHA)
    monkeypatch.setenv("INGEST_JOB_IMAGE", IMAGE)

    with pytest.raises(RuntimeError, match=f"{'c' * 40}.*{FULL_SHA}"):
        activation.build_publication_provenance(
            target_db="jw_mart_ingest_shadow_test",
            tables=activation.GENERAL_TABLES,
            builder_commit="c" * 40,
        )


def test_publish_rolls_back_when_provenance_record_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("APP_VERSION", FULL_SHA)
    monkeypatch.setenv("INGEST_JOB_IMAGE", IMAGE)
    action = _action()
    restored: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        activation, "require_completed_post_gate", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        activation,
        "publish_table_group_atomically",
        lambda *_args, **_kwargs: (action,),
    )
    monkeypatch.setattr(
        activation, "record_mysql_component", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        activation,
        "record_publication_provenance",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("provenance record failed")
        ),
    )
    monkeypatch.setattr(
        activation,
        "restore_table_group_atomically",
        lambda *_args, **kwargs: restored.append(kwargs["actions"]),
    )
    target = activation.MartActivation(
        "jw_mart", "jw_mart_ingest_shadow_test", "jw_mart_ingest_shadow_build_run1"
    )

    with pytest.raises(RuntimeError, match="provenance record failed"):
        activation.publish_shadow(
            object(),
            target,
            run_id="run1",
            epoch="2026-07",
            ingest_run_id="ingest-run1",
            activation_journal=_journal(tmp_path / "activation.json"),
        )

    assert restored == [(action,)]
