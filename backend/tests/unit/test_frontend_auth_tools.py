"""Operator provenance and non-optimizable gates; no AWS, subprocess, or real database work."""
import ast
import importlib.util
from pathlib import Path
from uuid import UUID
import pytest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]


def load(name, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/spikes'))
    path = ROOT / ('scripts/spikes/' + name + '.py')
    spec = importlib.util.spec_from_file_location('test_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_operator_provenance_is_stable_server_database_channel_not_a_human_claim(monkeypatch):
    module = load('fixtures_frontend_auth', monkeypatch)
    first = module.database_operator('fictional_database', 'fictional_owner')
    second = module.database_operator('fictional_database', 'fictional_owner')
    other = module.database_operator('fictional_database', 'other_owner')
    assert first == second and UUID(first['id']) != UUID(other['id'])
    assert first['issuer'] == 'urn:eai:bootstrap:database-operator'
    assert first['channel'] == 'authenticated_database_bootstrap' and first['human_oidc_identity_verified'] is False


def test_fixture_target_binding_detects_another_branch_without_hashing_password(monkeypatch):
    module = load('fixtures_frontend_auth', monkeypatch)
    one = SimpleNamespace(host='branch-a.example.test', port=5432, dbname='fixture', user='owner', password='private-one')
    two = SimpleNamespace(**{**vars(one), 'password': 'private-two'})
    other = SimpleNamespace(**{**vars(one), 'host': 'branch-b.example.test'})
    assert module.target_fingerprint(one) == module.target_fingerprint(two)
    assert module.target_fingerprint(one) != module.target_fingerprint(other)


@pytest.mark.parametrize('name', ['build_frontend_auth', 'prepare_frontend_auth', 'check_frontend_auth_cloud', 'operator_frontend_auth', 'fixtures_frontend_auth'])
def test_operator_gates_cannot_disappear_under_python_optimization(name):
    tree = ast.parse((ROOT / ('scripts/spikes/' + name + '.py')).read_text())
    assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree))
    gate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'require')
    namespace = {}
    exec(compile(ast.Module(body=[gate], type_ignores=[]), 'gate_fixture', 'exec', optimize=2), namespace)
    with pytest.raises(ValueError):
        namespace['require'](False)
