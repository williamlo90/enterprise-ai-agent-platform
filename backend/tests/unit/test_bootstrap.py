import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from enterprise_ai_platform.config import ConfigurationError, Settings, load_settings
from enterprise_ai_platform.main import create_app


def write_config(path: Path) -> None:
    path.write_text(
        "APP_ENV=development\nAPP_HOST=127.0.0.1\nAPP_PORT=8000\n", encoding="utf-8"
    )


def test_missing_required_configuration_fails_closed():
    with pytest.raises(ConfigurationError) as exc:
        load_settings(None)
    assert str(exc.value) == "Missing or invalid configuration: APP_ENV, APP_HOST, APP_PORT"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("APP_ENV", "synthetic-private-marker"),
        ("APP_HOST", "https://synthetic-private-marker@example.invalid"),
        ("APP_HOST", "fe80::1%synthetic-private-marker"),
        ("APP_PORT", "synthetic-private-marker"),
        ("APP_PORT", "0"),
        ("APP_PORT", "65536"),
        ("APP_PORT", "8000.5"),
    ],
)
def test_invalid_configuration_reports_field_without_input(monkeypatch, name, value):
    for key, item in {"APP_ENV": "test", "APP_HOST": "127.0.0.1", "APP_PORT": "8000"}.items():
        monkeypatch.setenv(key, item)
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError) as exc:
        load_settings(None)
    assert name in str(exc.value)
    assert "synthetic-private-marker" not in str(exc.value)


def test_dotenv_process_precedence_and_no_environment_mutation(monkeypatch, tmp_path):
    file = tmp_path / ".env"
    write_config(file)
    monkeypatch.setenv("APP_PORT", "9000")
    before = dict(os.environ)
    settings = load_settings(file)
    assert settings.env == "development"
    assert settings.port == 9000
    assert dict(os.environ) == before
    monkeypatch.setenv("APP_PORT", "")
    with pytest.raises(ConfigurationError, match="APP_PORT"):
        load_settings(file)


def test_only_working_directory_dotenv_is_loaded(monkeypatch, tmp_path):
    write_config(tmp_path / ".env")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    with pytest.raises(ConfigurationError, match="APP_ENV"):
        load_settings()
    assert load_settings(tmp_path / ".env").port == 8000


def test_no_env_file_means_no_dotenv_read(tmp_path):
    write_config(tmp_path / ".env")
    with pytest.raises(ConfigurationError):
        load_settings(None)


def test_missing_explicit_file_and_directory_are_rejected(tmp_path):
    with pytest.raises(ConfigurationError, match="file is unavailable"):
        load_settings(tmp_path / "missing", require_file=True)
    with pytest.raises(ConfigurationError, match="file is unavailable"):
        load_settings(tmp_path)


def test_unreadable_encoding_is_sanitized(tmp_path):
    file = tmp_path / "synthetic-private-marker.env"
    file.write_bytes(b"\xff\xfe\xff")
    with pytest.raises(ConfigurationError) as exc:
        load_settings(file, require_file=True)
    assert str(exc.value) == "Configuration could not be loaded."
    assert "synthetic-private-marker" not in str(exc.value)


def test_settings_reject_boolean_port_and_are_immutable():
    with pytest.raises(ValidationError):
        Settings(env="test", host="127.0.0.1", port=True)
    settings = Settings(env="test", host="::1", port=65535)
    assert settings.host == "::1"
    with pytest.raises(ValidationError):
        settings.port = 1


@pytest.mark.parametrize("mode", ["test", "production"])
def test_non_development_has_no_docs_or_business_routes(mode):
    app = create_app(Settings(env=mode, host="127.0.0.1", port=8000))
    with TestClient(app) as client:
        for path in ["/docs", "/openapi.json", "/redoc", "/api/v1/claims"]:
            assert client.get(path).status_code == 404
    assert app.debug is False


def test_development_schema_and_app_instances_are_isolated():
    first = create_app(Settings(env="development", host="127.0.0.1", port=8000))
    second = create_app(Settings(env="production", host="127.0.0.1", port=9000))
    first.state.probe = "isolated"
    assert not hasattr(second.state, "probe")
    with TestClient(first) as client:
        schema = client.get("/openapi.json").json()
        assert schema["info"]["title"] == "Enterprise AI Agent Platform API"
        assert set(schema["paths"]) == {"/health/live", "/health/ready"}
        assert client.get("/docs").status_code == 200
        assert client.get("/").status_code == 404


def test_cli_check_config_and_module_entry_point(tmp_path):
    file = tmp_path / "bootstrap.env"
    write_config(file)
    result = subprocess.run(
        [sys.executable, "-m", "enterprise_ai_platform", "--env-file", str(file), "--check-config"],
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "Configuration valid."
    assert result.stderr == ""


def test_cli_invalid_values_and_missing_file_do_not_print_inputs(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_HOST", "synthetic-private-marker")
    monkeypatch.setenv("APP_PORT", "8000")
    commands = [
        ["--no-env-file", "--check-config"],
        ["--env-file", str(tmp_path / "synthetic-private-marker.env"), "--check-config"],
    ]
    for args in commands:
        result = subprocess.run(
            [sys.executable, "-m", "enterprise_ai_platform", *args],
            text=True, capture_output=True, timeout=15,
        )
        assert result.returncode == 2
        assert "Configuration error:" in result.stderr
        assert "synthetic-private-marker" not in result.stdout + result.stderr
        assert "Traceback" not in result.stderr
