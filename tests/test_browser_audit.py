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
