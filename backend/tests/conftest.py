"""Bootstrap tests use synthetic configuration, never a developer's dotenv file."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch, tmp_path):
    for name in tuple(os.environ):
        if name.upper().startswith(("APP_", "OIDC_", "SPIKE_")) or name.upper() in {"DATABASE_URL", "DATABASE_DIRECT_URL", "PUBLIC_ORIGIN"}:
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
