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


@pytest.mark.parametrize("method,path,expected", [
    ("POST", "/api/questions/question1/statistics/scopes", True),
    ("PUT", "/api/surveys/survey1/question-statistics-grants/question1", True),
    ("GET", "/api/questions/question1/statistics/scopes", False),
    ("GET", "/api/questions/question1/statistics", False),
    ("GET", "/api/questions/question1/statistics/answers", False),
    ("GET", "/api/questions/question1/statistics/export", False),
    ("POST", "/api/questions/question1/statistics", False),
    ("POST", "/api/surveys/survey1/question-statistics-grants/question1", False),
    ("PUT", "/api/surveys/survey1/question-statistics-grants", False),
    ("DELETE", "/api/surveys/survey1/question-statistics-grants/question1", False),
    ("PUT", "/api/surveys/survey1/question-statistics-grants/question1/extra", False),
    ("POST", "/api/auth/login", False),
])
def test_statistics_audit_observes_only_scope_creation_and_single_grant_writes(method, path, expected):
    assert any(method == wanted and pattern.fullmatch(path)
               for wanted, pattern in browser_runner.WRITE_PATHS) is expected


def statistics_documents():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    private = {"viewerId": "private viewer identity", "ownerId": "private owner identity",
               "recipientId": "private recipient identity", "respondentId": "private respondent identity",
               "creationKey": "private creation key", "requestDigest": "private digest",
               "authSecret": "private auth secret", "answers": [{"value": "private text answer"}],
               "content": {"title": "private content"}, "purgeAt": now,
               "futurePrivateField": "must never become observable"}
    scopes = [{"_id": "scope1", "questionId": "q1", "questionVersionId": None, "respondentType": "all",
               "createdAt": now, "expiresAt": now, "publicationCount": 2, "surveyCount": 2,
               "instanceCount": 3, "hasForeignSources": True, **deepcopy(private)}]
    sources = [{"_id": "source1", "scopeId": "scope1", "surveyId": "survey1", "surveyVersionId": "pub1",
                "asOfSeq": Int64(7), "accessMode": "owner", **deepcopy(private)},
               {"_id": "source2", "scopeId": "scope1", "surveyId": "survey2", "surveyVersionId": "pub2",
                "asOfSeq": Int64(3), "accessMode": "aggregate", "grantId": "grant1", "grantRevision": 4,
                **deepcopy(private)}]
    grants = [{"_id": "grant1", "surveyId": "survey2", "questionId": "q1", "permission": "aggregate",
               "active": True, "revision": 4, **deepcopy(private)}]
    return scopes, sources, grants


def test_statistics_audit_whitelists_range_and_grant_metadata_without_identity_secrets_or_answers():
    documents = statistics_documents()
    original = deepcopy(documents)
    result = browser_runner.statistics_records(*documents)
    assert result == {
        "statScopes": [{"id": "scope1", "questionId": "q1", "questionVersionId": None,
                        "respondentType": "all", "createdAt": "2026-10-27T00:00:00+00:00",
                        "expiresAt": "2026-10-27T00:00:00+00:00", "publicationCount": 2,
                        "surveyCount": 2, "instanceCount": 3, "hasForeignSources": True}],
        "statSources": [{"id": "source1", "scopeId": "scope1", "surveyId": "survey1", "surveyVersionId": "pub1",
                         "asOfSeq": 7, "accessMode": "owner"},
                        {"id": "source2", "scopeId": "scope1", "surveyId": "survey2", "surveyVersionId": "pub2",
                         "asOfSeq": 3, "accessMode": "aggregate", "grantId": "grant1", "grantRevision": 4}],
        "statGrants": [{"id": "grant1", "surveyId": "survey2", "questionId": "q1", "permission": "aggregate",
                        "active": True, "revision": 4}],
    }
    assert documents == original
    encoded = json.dumps(result)
    for excluded in ("private", "viewerId", "ownerId", "recipientId", "respondentId", "creationKey",
                     "requestDigest", "authSecret", "answers", "content", "purgeAt", "futurePrivateField"):
        assert excluded not in encoded
    assert json.loads(encoded) == result


def test_statistics_audit_empty_metadata_has_explicit_empty_lists():
    assert browser_runner.statistics_records([], [], []) == {"statScopes": [], "statSources": [], "statGrants": []}


def test_safe_summary_keeps_old_counts_and_adds_all_three_statistics_collections_in_one_snapshot():
    import asyncio
    from types import SimpleNamespace

    scopes, sources, grants = statistics_documents()
    captured = []

    class Cursor:
        def __init__(self, rows): self.rows = rows
        def sort(self, *args): return self
        async def to_list(self, length=None): return deepcopy(self.rows)

    class Collection:
        def __init__(self, name, rows): self.name, self.rows = name, rows
        def find(self, query, projection, session=None):
            captured.append((self.name, projection, session))
            # Return deliberately over-complete documents: the output whitelist
            # must still protect the audit if database projection changes later.
            return Cursor(self.rows)
        async def count_documents(self, query, session=None):
            assert session is transaction
            return 0

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def with_transaction(self, action, **options):
            assert options["read_concern"].document == {"level": "snapshot"}
            return await action(self)

    transaction = Session()
    db = SimpleNamespace(client=SimpleNamespace(start_session=lambda: transaction))
    old_names = ("users", "questions", "question_versions", "surveys", "survey_versions", "responses",
                 "question_shares", "question_banks", "question_bank_items")
    for name in old_names:
        setattr(db, name, Collection(name, []))
    for name, rows in (("question_stat_scopes", scopes), ("question_stat_sources", sources),
                       ("question_stat_grants", grants)):
        setattr(db, name, Collection(name, rows))
    result = asyncio.run(browser_runner.safe_summary(db))
    assert result["counts"] == {**dict.fromkeys(old_names, 0), "question_stat_scopes": 1,
                                "question_stat_sources": 2, "question_stat_grants": 1}
    assert result["questions"] == []
    assert result["surveys"] == []
    assert result["shares"] == []
    assert result["banks"] == []
    assert result["bankItems"] == []
    for key, value in browser_runner.statistics_records(scopes, sources, grants).items():
        assert result[key] == value
    for name, projection, session in captured:
        assert session is transaction
        if name.startswith("question_stat_"):
            assert all(value == 1 for value in projection.values())
            assert not set(projection) & {"viewerId", "ownerId", "recipientId", "creationKey", "requestDigest", "answers"}
    assert "private" not in json.dumps(result)
