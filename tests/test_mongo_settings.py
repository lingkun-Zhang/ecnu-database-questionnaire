"""URI selection is pure; these checks do not read .env or connect."""
import pytest

from mongo_settings import resolve_test_mongo_uri


@pytest.mark.parametrize("environment,expected", [
    ({"MONGO_URI": "canonical"}, "canonical"),
    ({"MONGODB_URI": "legacy"}, "legacy"),
    ({"MONGO_URI": "canonical", "MONGODB_URI": "legacy"}, "canonical"),
])
def test_explicit_test_uri_does_not_consult_or_mutate_local_configuration(environment, expected):
    before = dict(environment)
    def forbidden_fallback():
        raise AssertionError("Local configuration must not override an explicit test URI")
    assert resolve_test_mongo_uri(environment, forbidden_fallback) == expected
    assert environment == before


def test_local_configuration_is_used_only_without_an_explicit_uri():
    calls = []
    def local_configuration():
        calls.append(True)
        return "local configured URI"
    assert resolve_test_mongo_uri({}, local_configuration) == "local configured URI"
    assert calls == [True]


@pytest.mark.parametrize("environment", [
    {"MONGO_URI": ""}, {"MONGODB_URI": " "},
    {"MONGO_URI": "", "MONGODB_URI": "legacy"},
])
def test_an_empty_explicit_setting_cannot_silently_connect_elsewhere(environment):
    with pytest.raises(ValueError, match="explicitly configured"):
        resolve_test_mongo_uri(environment, lambda: pytest.fail("Unexpected local fallback"))


def test_legacy_uri_is_frozen_before_importing_app_that_loads_local_configuration():
    """Exercise the real conftest import order in a fresh child, without I/O."""
    import os
    from pathlib import Path
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    script = r"""
import os, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path[:0] = [str(root / 'tests'), str(root / 'backend'), str(root)]
import dotenv
def synthetic_local_config(*args, **kwargs):
    os.environ.setdefault('MONGO_URI', 'mongodb://127.0.0.1:27117/?replicaSet=rs0')
    return True
dotenv.load_dotenv = synthetic_local_config
import conftest
assert os.environ['MONGO_URI'].endswith('27117/?replicaSet=rs0')
assert conftest.mongo_uri.__wrapped__() == 'mongodb://127.0.0.1:27227/?replicaSet=phase2test'
"""
    environment = dict(os.environ)
    environment.pop("MONGO_URI", None)
    environment["MONGODB_URI"] = "mongodb://127.0.0.1:27227/?replicaSet=phase2test"
    for name in ("AUTH_SECRET", "FILL_SECRET", "PARTICIPANT_SECRET"):
        environment[name] = "synthetic-test-import-secret-" * 3
    result = subprocess.run([sys.executable, "-c", script, str(root)], env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
