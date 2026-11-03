"""Probe the real cloud entrypoint in a fresh process without developer dotenv."""

import os
from pathlib import Path
import subprocess
import sys


ENTRYPOINT = Path(__file__).resolve().parents[2] / "asgi.py"


def test_cloud_entrypoint_serves_health_with_lifespan_and_ignores_dotenv(tmp_path):
    (tmp_path / ".env").write_text(
        "APP_ENV=development\nAPP_DATABASE_ENABLED=true\n"
        "DATABASE_URL=synthetic-private-marker\n", encoding="utf-8",
    )
    environment = dict(os.environ, APP_ENV="production", APP_HOST="127.0.0.1",
                       APP_PORT="8000", APP_DATABASE_ENABLED="false")
    result = subprocess.run(
        [sys.executable, "-c", """
import runpy, sys
from fastapi.testclient import TestClient
app = runpy.run_path(sys.argv[1])["app"]
assert app.state.resources.database is None
assert app.state.settings.env == "production"
with TestClient(app) as client:
    assert client.get('/health/live').status_code == 200
    assert client.get('/health/ready').status_code == 200
    assert client.get('/docs').status_code == 404
    assert client.get('/api/v1/auth/callback').status_code == 404
assert app.state.resources.readiness.ready is False
print('Cloud entrypoint lifecycle passed.')
""", str(ENTRYPOINT)], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "synthetic-private-marker" not in result.stdout + result.stderr


def test_cloud_entrypoint_fails_closed_without_process_settings(tmp_path):
    (tmp_path / ".env").write_text(
        "APP_ENV=development\nAPP_HOST=127.0.0.1\nAPP_PORT=8000\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-c", """
import runpy, sys
from enterprise_ai_platform.config import ConfigurationError
try:
    runpy.run_path(sys.argv[1])
except ConfigurationError as error:
    assert str(error) == 'Missing or invalid configuration: APP_ENV, APP_HOST, APP_PORT'
    print('Missing deployment settings rejected.')
else:
    raise AssertionError('Local dotenv must not supply cloud configuration.')
""", str(ENTRYPOINT)], cwd=tmp_path, env=dict(os.environ),
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
