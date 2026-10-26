"""Pure browser-audit checks; no app startup, dotenv loading or database access."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

from bson import Int64
import pytest


SPEC = importlib.util.spec_from_file_location(
    "question_library_browser", Path(__file__).parent / "support" / "question_library_browser.py")
browser_runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(browser_runner)


@pytest.mark.parametrize("value", [Int64(0), Int64(3), 0, 3, 0.0, 3.25],
                         ids=["bson-zero", "bson-three", "int-zero", "int-three", "float-zero", "float-fraction"])
def test_numeric_audit_preserves_stored_numbers(value):
    responses = [{"answers": [{"questionId": "score", "kind": "number", "value": value}]}]
    result = browser_runner.numeric_answers(responses)
    assert result == [{"instanceId": "score", "value": value}]
    assert json.loads(json.dumps(result)) == [{"instanceId": "score", "value": value}]


@pytest.mark.parametrize("kind,value", [
    ("number", True), ("number", False), ("number", "3"), ("number", None),
    ("text", Int64(3)), ("text", 3.25), ("text", "private free text"),
], ids=["true", "false", "numeric-string", "null", "text-bson", "text-float", "text-answer"])
def test_numeric_audit_excludes_booleans_strings_and_other_question_kinds(kind, value):
    responses = [{"answers": [{"questionId": "excluded", "kind": kind, "value": value}]}]
    assert browser_runner.numeric_answers(responses) == []


def test_numeric_audit_keeps_only_safe_fields_without_changing_responses():
    responses = [{"respondentId": "private identity", "answers": [
        {"questionId": "first", "kind": "number", "value": Int64(0), "private": "must not copy"},
        {"questionId": "note", "kind": "text", "value": "private free text"},
        {"questionId": "flag", "kind": "number", "value": True},
    ]}, {}, {"answers": [{"questionId": "second", "kind": "number", "value": Int64(3)}]}]
    original = deepcopy(responses)
    assert browser_runner.numeric_answers(responses) == [
        {"instanceId": "first", "value": 0}, {"instanceId": "second", "value": 3}]
    assert responses == original


@pytest.mark.parametrize("method,path,expected", [
    ("POST", "/api/question-banks", True),
    ("PUT", "/api/question-banks/bank1", True),
    ("PUT", "/api/question-banks/bank1/questions/question1", True),
    ("DELETE", "/api/question-banks/bank1/questions/question1", True),
    ("GET", "/api/question-banks/bank1/questions", False),
    ("DELETE", "/api/question-banks/bank1", False),
    ("POST", "/api/question-banks/bank1", False),
    ("PUT", "/api/question-banks/bank1/shares", False),
    ("POST", "/api/auth/register", False),
])
def test_bank_audit_monitors_only_the_successful_write_routes(method, path, expected):
    assert any(method == wanted and pattern.fullmatch(path)
               for wanted, pattern in browser_runner.WRITE_PATHS) is expected


def test_bank_audit_whitelists_pointers_without_copying_owner_or_creation_secrets():
    banks = [{"_id": "bank1", "name": "合成题库", "archived": False, "revision": 3,
              "ownerId": "private account", "description": "must not copy", "creationKey": "private key",
              "creationDigest": "private digest", "lastRequestDigest": "private digest"}]
    members = [{"_id": "item1", "bankId": "bank1", "questionId": "question1", "questionVersionId": "version1",
                "ownerId": "must not copy", "content": {"title": "must not copy"}}]
    original = deepcopy((banks, members))
    assert browser_runner.bank_records(banks, members) == {
        "banks": [{"id": "bank1", "name": "合成题库", "archived": False, "revision": 3}],
        "bankItems": [{"id": "item1", "bankId": "bank1", "questionId": "question1", "questionVersionId": "version1"}],
    }
    assert (banks, members) == original
