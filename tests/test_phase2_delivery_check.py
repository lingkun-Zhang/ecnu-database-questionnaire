"""Pure guards for the opt-in real HTTP delivery runner; no database connections."""
from pathlib import Path

import pytest

from scripts.phase2_delivery_check import (
    build_parser, public_route, validate_database, validate_run_output, validate_uri, verify_ownership,
)


@pytest.mark.parametrize("uri", [
    "mongodb://127.0.0.1:27117/?replicaSet=rs0",
    "mongodb://example.com:27227/?replicaSet=phase2test",
    "mongodb://127.0.0.1:27227/business?replicaSet=phase2test",
    "mongodb://user:password@127.0.0.1:27227/?replicaSet=phase2test",
    "mongodb://127.0.0.1:27227/?replicaSet=other",
    "mongodb://127.0.0.1:27227,127.0.0.1:27228/?replicaSet=phase2test",
])
def test_business_remote_credentialed_or_ambiguous_uri_is_refused_without_connecting(uri):
    with pytest.raises(ValueError):
        validate_uri(uri)


def test_explicit_test_uri_is_accepted_without_connecting():
    assert validate_uri("mongodb://127.0.0.1:27227/?replicaSet=phase2test")


@pytest.mark.parametrize("name", ["survey_phase1", "admin", "survey_delivery_" + "a" * 31,
                                  "survey_delivery_" + "g" * 32])
def test_existing_business_or_lookalike_names_cannot_authorize_cleanup(name):
    with pytest.raises(ValueError):
        validate_database(name)


@pytest.mark.parametrize("marker", [None, {"_id": "owner", "runId": "other", "stage": "delivery"},
                                    {"_id": "owner", "runId": "run", "stage": "seed"},
                                    {"_id": "owner", "runId": "run", "stage": "delivery", "extra": True}])
def test_missing_changed_or_nonexact_marker_cannot_authorize_cleanup(marker):
    with pytest.raises(RuntimeError):
        verify_ownership("survey_delivery_" + "a" * 32, marker, "run")


def test_exact_generated_name_and_run_marker_authorize_only_that_target():
    verify_ownership("survey_delivery_" + "a" * 32,
                     {"_id": "owner", "runId": "run", "stage": "delivery"}, "run")


def test_output_refuses_overwrite_runtime_root_and_escape_before_writing(tmp_path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    existing = runtime / "existing"
    existing.mkdir()
    sentinel = existing / "sentinel.txt"
    sentinel.write_text("preserve")
    for value in (runtime, existing, tmp_path / "outside", runtime / ".." / "escape"):
        with pytest.raises(ValueError):
            validate_run_output(value, tmp_path)
    assert sentinel.read_text() == "preserve"
    assert validate_run_output(Path(".runtime/new"), tmp_path) == runtime / "new"
    assert not (runtime / "new").exists()


def test_output_refuses_a_runtime_file_without_overwriting_it(tmp_path):
    runtime = tmp_path / ".runtime"
    runtime.write_text("preserve")
    with pytest.raises(ValueError):
        validate_run_output(runtime / "new", tmp_path)
    assert runtime.read_text() == "preserve"


@pytest.mark.parametrize("extra", [["--url", "http://127.0.0.1:8000"], ["--database", "survey_phase1"]])
def test_cli_does_not_accept_an_existing_business_target(extra):
    with pytest.raises(SystemExit) as result:
        build_parser().parse_args(["--mongo-uri", "mongodb://127.0.0.1:27227/?replicaSet=phase2test",
                                  "--output", ".runtime/new", *extra])
    assert result.value.code == 2


def test_public_route_keeps_contract_but_removes_synthetic_identifiers():
    assert public_route("/questions/" + "a" * 32 + "/statistics") == "/questions/{id}/statistics"
    assert public_route("/surveys/12345678-1234-1234-1234-123456789abc/draft") == "/surveys/{id}/draft"
    assert public_route("/auth/register") == "/auth/register"
