import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CHECK = ROOT / "scripts/checks/check_architecture.py"


def run_check(root):
    return subprocess.run(
        [sys.executable, str(CHECK), "--root", str(root)],
        capture_output=True, text=True, timeout=10,
    )


def test_actual_application_imports_follow_boundaries():
    result = run_check(ROOT)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("path,source", [
    ("modules/system/domain.py", "import fastapi"),
    ("modules/system/ports.py", "from .service import HealthService"),
    ("modules/system/service.py", "from ...infrastructure.database import connection"),
    ("modules/system/service.py", "from enterprise_ai_platform.modules.identity.repository import Users"),
    ("modules/system/service.py", "from enterprise_ai_platform import infrastructure"),
    ("api/health.py", "import psycopg"),
    ("api/health.py", "import sqlite3"),
    ("modules/system/domain.py", "from sqlite3 import connect"),
    ("modules/system/ports.py", "import sqlite3.dbapi2"),
    ("modules/system/service.py", "import _sqlite3 as sql"),
    ("modules/system/service.py", "import dbm"),
    ("config.py", "import shelve"),
    ("config.py", "from enterprise_ai_platform.infrastructure import database"),
    ("api/health.py", "from enterprise_ai_platform.composition import build_services"),
    ("infrastructure/database/repository.py", "from enterprise_ai_platform.api import health"),
    ("composition.py", "import reimbursement_service.modules.claims.service"),
    ("modules/system/service.py", "import importlib as loader\nloader.import_module('fastapi')"),
    ("modules/system/service.py", "from builtins import __import__ as load\nload('fastapi')"),
    ("modules/system/service.py", "loader = __import__\nloader('fastapi')"),
    ("modules/system/service.py", "import builtins as base\nbase.__import__('fastapi')"),
    ("modules/system/service.py", "from .ports import *"),
])
def test_guard_rejects_illegal_dependencies_and_dynamic_import_aliases(tmp_path, path, source):
    file = tmp_path / "backend/src/enterprise_ai_platform" / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(source, encoding="utf-8")
    result = run_check(tmp_path)
    assert result.returncode == 1
    assert path.replace("/", "\\") in result.stdout or path in result.stdout


@pytest.mark.parametrize("source", [
    "import enterprise_ai_platform.modules.drafts.service",
    "from sqlite3 import connect",
    "import sqlite3.dbapi2 as sql",
])
def test_guard_checks_reference_service_package_boundary_too(tmp_path, source):
    file = tmp_path / "reference_service/src/reimbursement_service/modules/claims/service.py"
    file.parent.mkdir(parents=True)
    file.write_text(source, encoding="utf-8")
    assert run_check(tmp_path).returncode == 1


@pytest.mark.parametrize("package,location", [
    ("enterprise_ai_platform", "backend"),
    ("reimbursement_service", "reference_service"),
])
def test_database_driver_is_allowed_in_adapter(tmp_path, package, location):
    file = tmp_path / location / "src" / package / "infrastructure/database/repository.py"
    file.parent.mkdir(parents=True)
    file.write_text("from sqlite3 import connect\nimport dbm", encoding="utf-8")
    assert run_check(tmp_path).returncode == 0


def test_guard_accepts_relative_domain_port_and_public_service_imports(tmp_path):
    file = tmp_path / "backend/src/enterprise_ai_platform/modules/system/service.py"
    file.parent.mkdir(parents=True)
    file.write_text(
        "from .domain import ProbeResult\nfrom . import ports\n"
        "from enterprise_ai_platform.modules.identity.ports import ActorVerifier\n",
        encoding="utf-8",
    )
    assert run_check(tmp_path).returncode == 0


def test_empty_or_invalid_source_is_not_a_false_pass(tmp_path):
    assert run_check(tmp_path).returncode == 1
    file = tmp_path / "backend/src/enterprise_ai_platform/main.py"
    file.parent.mkdir(parents=True)
    file.write_text("invalid syntax !", encoding="utf-8")
    assert run_check(tmp_path).returncode == 1
