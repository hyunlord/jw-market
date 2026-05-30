from pathlib import Path
import os
import subprocess


SCRIPT = Path("pipeline/scripts/deploy_to_galera_bluegreen.sh")


def test_bluegreen_deploy_script_contract():
    assert SCRIPT.exists(), "blue-green deploy script must exist"
    text = SCRIPT.read_text()

    assert "--dry-run" in text
    assert "--no-switch" in text
    assert "--switch" in text
    assert "--rollback" in text
    assert "CREATE TABLE ${table}_staging LIKE ${table}" in text
    assert "RENAME TABLE" in text
    assert "cache_deep_analysis_ai_analysis" in text
    assert "cache_deep_analysis_ai_analysis_staging" not in text
    assert "build_cache" not in text


def test_bluegreen_deploy_script_defaults_to_no_switch():
    text = SCRIPT.read_text()

    assert 'MODE="${1:---dry-run}"' in text
    assert "RENAME 보류" in text
    assert "staging 유지" in text


def test_bluegreen_deploy_help_does_not_require_db_password():
    env = os.environ.copy()
    env.pop("DB_PASS", None)

    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert "--dry-run" in result.stdout
