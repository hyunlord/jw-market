from pathlib import Path


SQL = Path("deploy/k8s/jw-market/report-download-writer-grants.sql").read_text()


def test_report_download_writer_has_only_required_table_privileges() -> None:
    normalized = " ".join(SQL.split())

    assert (
        "GRANT INSERT, DELETE ON `jw_market_audit_stage`.`report_download_event` "
        "TO 'jw_market_audit_writer_stage'@'10.%';"
    ) in normalized
    assert "GRANT SELECT" not in normalized
    assert "GRANT UPDATE" not in normalized
    assert "GRANT ALL" not in normalized
