from __future__ import annotations

from dataclasses import replace

import pytest

from pipeline.scripts.ingest_hook import iqvia_nsa_mart_activation as activation
from pipeline.scripts.ingest_hook.iqvia_nsa_publication import PublicationEvidence


EVIDENCE = PublicationEvidence(
    inventory_sha256="c" * 64,
    inventory_json="[]",
    window_start="2021Q2",
    window_end="2026Q1",
)


def _config() -> activation.NsaMartActivation:
    return activation.NsaMartActivation(
        source_db="jw_mart_d2",
        target_db="jw_mart_d2",
        build_db="jw_ingest_nsa_build_run1",
        builder_commit="a" * 40,
        image_digest=f"sha256:{'b' * 64}",
        image_ref=f"registry/jw-market@sha256:{'b' * 64}",
    )


def test_production_activation_requires_explicit_pl_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(activation.ENV_PROMOTION_APPROVED, raising=False)
    monkeypatch.setenv(activation.ENV_BUILDER_COMMIT, "a" * 40)
    monkeypatch.setenv(activation.ENV_IMAGE_DIGEST, f"sha256:{'b' * 64}")
    monkeypatch.setenv(
        activation.ENV_IMAGE_REF,
        f"registry/jw-market@sha256:{'b' * 64}",
    )

    with pytest.raises(RuntimeError, match="explicit PL gate"):
        activation.from_env(run_id="run1")


def test_shadow_mode_is_rejected_before_production_activation() -> None:
    with pytest.raises(RuntimeError, match="shadow"):
        activation.require_production_mode("shadow")


def test_activation_captures_full_image_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(activation.ENV_PROMOTION_APPROVED, "1")
    monkeypatch.setenv(activation.ENV_BUILDER_COMMIT, "a" * 40)
    monkeypatch.setenv(activation.ENV_IMAGE_DIGEST, f"sha256:{'b' * 64}")
    monkeypatch.setenv(
        activation.ENV_IMAGE_REF,
        f"registry/jw-market@sha256:{'b' * 64}",
    )

    config = activation.from_env(run_id="run-1")

    assert config.build_db == "jw_ingest_nsa_build_run_1"
    assert config.builder_commit == "a" * 40
    assert config.image_digest == f"sha256:{'b' * 64}"
    assert config.image_ref == f"registry/jw-market@sha256:{'b' * 64}"


def test_empty_build_schema_is_initialized_for_full_load(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        activation.iqvia_loader,
        "init_target_schema",
        lambda target, source: observed.append((target, source)),
    )

    activation.initialize_build_schema(_config())

    assert observed == [("jw_ingest_nsa_build_run1", "jw_mart_d2")]


def test_nonempty_serving_schema_is_replaced_as_atomic_group(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    observed: dict[str, object] = {}
    actions = tuple(
        activation.PublishAction(table, "atomic_group_rename", table, f"{table}__old_run1", 1)
        for table in activation.NSA_PUBLISH_TABLES
    )
    monkeypatch.setattr(activation, "record_publication_provenance", lambda *_a, **_k: None)
    monkeypatch.setattr(
        activation,
        "publish_table_group_atomically",
        lambda _conn, **kwargs: observed.update(kwargs) or actions,
    )
    monkeypatch.setattr(activation, "record_mysql_component", lambda *_a, **_k: ())

    assert activation.publish(
        object(),
        config,
        run_id="run1",
        epoch="2026Q1",
        post_gate_verified=True,
        publication_evidence=EVIDENCE,
    ) == actions
    assert observed["tables"] == activation.NSA_PUBLISH_TABLES
    assert observed["target_db"] == config.target_db


def test_publish_failure_leaves_serving_group_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activation, "record_publication_provenance", lambda *_a, **_k: None)
    monkeypatch.setattr(
        activation,
        "publish_table_group_atomically",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("load failed")),
    )
    with pytest.raises(RuntimeError, match="load failed"):
        activation.publish(
            object(),
            _config(),
            run_id="run1",
            epoch="2026Q1",
            post_gate_verified=True,
            publication_evidence=EVIDENCE,
        )


@pytest.mark.parametrize(
    "config",
    [
        replace(_config(), builder_commit=""),
        replace(_config(), builder_commit="abc"),
        replace(_config(), image_digest=""),
        replace(_config(), image_digest="sha256:abc"),
        replace(_config(), image_ref="registry/jw-market:mutable"),
    ],
)
def test_invalid_provenance_blocks_publish(
    config: activation.NsaMartActivation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[bool] = []
    monkeypatch.setattr(
        activation, "publish_table_group_atomically", lambda *_a, **_k: called.append(True)
    )

    with pytest.raises(RuntimeError, match="provenance"):
        activation.publish(
            object(),
            config,
            run_id="run1",
            epoch="2026Q1",
            post_gate_verified=True,
            publication_evidence=EVIDENCE,
        )
    assert called == []


def test_provenance_record_failure_restores_published_group(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    actions = tuple(
        activation.PublishAction(table, "atomic_group_rename", table, f"{table}__old_run1", 1)
        for table in activation.NSA_PUBLISH_TABLES
    )
    monkeypatch.setattr(activation, "record_mysql_component", lambda *_a, **_k: ())
    monkeypatch.setattr(activation, "publish_table_group_atomically", lambda *_a, **_k: actions)
    monkeypatch.setattr(
        activation,
        "record_publication_provenance",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("provenance denied")),
    )
    rollback_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        activation,
        "rollback_publication",
        lambda *_a, **kwargs: rollback_calls.append(kwargs),
    )

    with pytest.raises(RuntimeError, match="provenance denied"):
        activation.publish(
            object(),
            config,
            run_id="run1",
            epoch="2026Q1",
            post_gate_verified=True,
            publication_evidence=EVIDENCE,
        )
    assert rollback_calls == [
        {
            "actions": actions,
            "run_id": "run1",
            "provenance_recorded": False,
            "component_recorded": True,
        }
    ]


def test_component_record_failure_does_not_write_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions = tuple(
        activation.PublishAction(table, "atomic_group_rename", table, f"{table}__old_run1", 1)
        for table in activation.NSA_PUBLISH_TABLES
    )
    monkeypatch.setattr(activation, "publish_table_group_atomically", lambda *_a, **_k: actions)
    monkeypatch.setattr(
        activation,
        "record_mysql_component",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("component denied")),
    )
    provenance_calls: list[bool] = []
    monkeypatch.setattr(
        activation,
        "record_publication_provenance",
        lambda *_a, **_k: provenance_calls.append(True),
    )
    rollback_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        activation,
        "rollback_publication",
        lambda *_a, **kwargs: rollback_calls.append(kwargs),
    )

    with pytest.raises(RuntimeError, match="component denied"):
        activation.publish(
            object(),
            _config(),
            run_id="run1",
            epoch="2026Q1",
            post_gate_verified=True,
            publication_evidence=EVIDENCE,
        )

    assert provenance_calls == []
    assert rollback_calls[0]["component_recorded"] is False


def test_publish_requires_verified_post_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(
        activation,
        "publish_table_group_atomically",
        lambda *_a, **_k: called.append(True),
    )

    with pytest.raises(RuntimeError, match="post_gate was not verified"):
        activation.publish(
            object(),
            _config(),
            run_id="run1",
            epoch="2026Q1",
            post_gate_verified=False,
            publication_evidence=EVIDENCE,
        )

    assert called == []


def test_activation_accepts_declared_mart_ingest_build_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(activation.ENV_PROMOTION_APPROVED, "1")
    monkeypatch.setenv(activation.ENV_BUILD_PREFIX, "jw_mart_ingest")
    monkeypatch.setenv(activation.ENV_BUILDER_COMMIT, "a" * 40)
    monkeypatch.setenv(activation.ENV_IMAGE_DIGEST, f"sha256:{'b' * 64}")
    monkeypatch.setenv(
        activation.ENV_IMAGE_REF,
        f"registry/jw-market@sha256:{'b' * 64}",
    )

    config = activation.from_env(run_id="run1")

    assert config.build_db == "jw_mart_ingest_run1"


def test_retention_keeps_latest_24_of_25_quarters_and_commits() -> None:
    periods = [f"{2020 + index // 4}Q{index % 4 + 1}" for index in range(25)]

    class Cursor:
        def __init__(self) -> None:
            self.deleted: tuple[str, ...] = ()

        def execute(self, sql: str, params: tuple[str, ...] = ()) -> None:
            if sql.startswith("DELETE"):
                self.deleted = params

        def fetchall(self):
            return [(period,) for period in periods]

        def close(self) -> None:
            pass

    cursor = Cursor()

    class Connection:
        def __init__(self) -> None:
            self.commits = 0

        def cursor(self):
            return cursor

        def commit(self) -> None:
            self.commits += 1

    connection = Connection()
    retained = activation.trim_raw_retention(connection, _config())

    assert retained == tuple(periods[-24:])
    assert cursor.deleted == (periods[0],)
    assert connection.commits == 1
