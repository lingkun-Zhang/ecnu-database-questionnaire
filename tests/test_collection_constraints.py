"""T23: real Mongo validation for required fields and representative BSON shapes.

Fixtures are created by the real application. Mutations bypass HTTP/domain
validation and go directly to the isolated database, without validator bypass,
mocked writes or mocked errors. Case tables are independent of schema constants.
"""
from copy import deepcopy
from datetime import timedelta
import json
from uuid import uuid4

from bson import Int64
from pymongo.errors import PyMongoError
import pytest

from conftest import account, published, start, submit
from scripts.fixtures import example_definition


REQUIRED_PATHS = {
    "users": ["username", "usernameKey", "passwordHash", "createdAt", "schemaVersion"],
    "surveys": [
        "ownerId", "publicId", "draft", "draftRevision", "currentVersionId", "lastVersionNo",
        "status", "deadlineAt", "allowMultiple", "policyLocked", "settingsRevision", "submitSequence",
        "createdAt", "updatedAt", "schemaVersion", "draft.title", "draft.entryQuestionId",
        "draft.questions.0.id", "draft.questions.0.kind", "draft.questions.0.config",
        "draft.questions.0.rules.0.condition", "draft.questions.0.rules.0.target",
        "draft.questions.2.config.options.0.label",
    ],
    "survey_versions": [
        "surveyId", "versionNo", "sourceDraftRevision", "definition", "publishKey", "publishRequestDigest",
        "publishedAt", "schemaVersion", "definition.title", "definition.entryQuestionId",
        "definition.questions.0.id", "definition.questions.0.rules.0.condition",
        "definition.questions.2.config.options.0.id", "definition.questions.2.config.options.0.label",
    ],
    "responses": [
        "surveyId", "versionId", "participantKey", "submissionKey", "payloadDigest", "canonicalizationVersion",
        "anonymous", "answers", "path", "acceptedSeq", "acceptedAt", "schemaVersion", "identity",
        "identity.userId", "identity.usernameAtSubmit", "answers.0.questionId", "answers.0.kind", "answers.0.value",
    ],
}

INVALID_VALUES = {
    "users": [("createdAt", "2026-10-21T00:00:00Z"), ("schemaVersion", True),
              ("passwordHash", ""), ("username", 17), ("unrecognized", True)],
    "surveys": [("status", "expired"), ("draftRevision", True), ("submitSequence", -1),
                ("deadlineAt", "2026-10-21T00:00:00Z"), ("allowMultiple", 1),
                ("draft.questions.0.config.integerOnly", 1), ("unrecognized", True)],
    "survey_versions": [("versionNo", 0), ("sourceDraftRevision", True),
                        ("publishedAt", "2026-10-21T00:00:00Z"), ("definition.questions", []),
                        ("definition.questions.0.title", ""),
                        ("definition.questions.2.config.options.0.label", ""), ("unrecognized", True)],
}


async def snapshot(context):
    return {name: await context.db[name].find({}).sort("_id", 1).to_list(length=None)
            for name in ("users", "surveys", "survey_versions", "responses")}


async def same_documents(context, expected, label):
    # Full snapshots include passwords/identities; report only equality on failure.
    equal = await snapshot(context) == expected
    assert equal, f"{label}: persisted business documents changed"


def proof(name, **details):
    print("COLLECTION_CONSTRAINT_RESULT " + json.dumps({"scenario": name, **details}, ensure_ascii=False))


async def prepared_documents(context):
    headers, _ = await account(context, "collection_contract_owner")
    survey, _ = await published(context, headers, example_definition(), multiple=True)
    attempt = await start(context, headers, survey)
    for anonymous, answers in [
        (False, {"q_age": 17, "q_minor": "学生", "q_hobby": ["read", "sport"], "q_score": 0, "q_note": "建议"}),
        (True, {"q_age": 18, "q_adult": "student", "q_hobby": ["read"], "q_score": 0, "q_note": "建议"}),
    ]:
        response = await submit(context, headers, survey, attempt, answers, anonymous=anonymous)
        assert response.status_code == 201
    before = await snapshot(context)
    assert [len(before[name]) for name in ("users", "surveys", "survey_versions", "responses")] == [1, 1, 1, 2]
    documents = {name: before[name][0] for name in ("users", "surveys", "survey_versions")}
    documents["responses"] = next(item for item in before["responses"] if item["anonymous"] is False)
    for name in before:
        options = await context.db[name].options()
        assert options["validationLevel"] == "strict" and options["validationAction"] == "error"
    return before, documents


def parent_field(document, path):
    parts = path.split(".")
    parent = document
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    return parent, parts[-1]


def remove_field(document, path):
    parent, field = parent_field(document, path)
    del parent[field]


def set_field(document, path, value):
    parent, field = parent_field(document, path)
    parent[field] = deepcopy(value)


def unique_clone(collection, original):
    clone = deepcopy(original)
    clone["_id"] = str(uuid4())
    assert clone["_id"] != original["_id"]
    if collection == "users":
        clone["username"] = clone["usernameKey"] = "clone_" + uuid4().hex[:20]
    elif collection == "surveys":
        clone.update(publicId=uuid4().hex, currentVersionId=None, deadlineAt=None,
                     status="draft", lastVersionNo=0, policyLocked=False, submitSequence=0)
    elif collection == "survey_versions":
        clone.update(versionNo=original["versionNo"] + 1000, publishKey=str(uuid4()))
    else:
        assert "oneTimeParticipantKey" not in original
        clone.update(submissionKey=str(uuid4()), acceptedSeq=original["acceptedSeq"] + 1000)
    return clone


def marker_change(collection, original, excluded_path):
    if excluded_path != "schemaVersion":
        return {"schemaVersion": 2}
    timestamp = {"users": "createdAt", "surveys": "createdAt",
                 "survey_versions": "publishedAt", "responses": "acceptedAt"}[collection]
    return {timestamp: original[timestamp] + timedelta(seconds=1)}


async def reject_without_writes(context, before, operation, label):
    code = None
    try:
        await operation()
    except PyMongoError as error:
        code = getattr(error, "code", None)
    # Do not print Mongo diagnostic payloads, which may include considered values.
    assert code == 121, f"{label}: expected Mongo validation error 121, received {code}"
    await same_documents(context, before, label)


async def exercise_rejection(context, collection, original, before, path, value=None, remove=False):
    candidate = unique_clone(collection, original)
    if remove:
        remove_field(candidate, path)
    else:
        set_field(candidate, path, value)
    await reject_without_writes(context, before, lambda: context.db[collection].insert_one(candidate),
                                f"{collection}:insert:{path}")
    replacement = deepcopy(original)
    replacement.update(marker_change(collection, original, path))
    if remove:
        remove_field(replacement, path)
    else:
        set_field(replacement, path, value)
    await reject_without_writes(context, before,
        lambda: context.db[collection].replace_one({"_id": original["_id"]}, replacement),
        f"{collection}:replace:{path}")
    update = {"$set": marker_change(collection, original, path)}
    if remove:
        update["$unset"] = {path: ""}
    else:
        update["$set"][path] = deepcopy(value)
    await reject_without_writes(context, before,
        lambda: context.db[collection].update_one({"_id": original["_id"]}, update),
        f"{collection}:update:{path}")


def with_clone(before, collection, clone):
    expected = deepcopy(before)
    expected[collection].append(deepcopy(clone))
    expected[collection].sort(key=lambda item: item["_id"])
    return expected


async def valid_round_trip(context, collection, original, before):
    # Positive controls change only a fresh clone, never an original document.
    # These are structural checks, not app-supported schema migration workflows.
    clone = unique_clone(collection, original)
    inserted = await context.db[collection].insert_one(clone)
    assert inserted.acknowledged and inserted.inserted_id == clone["_id"]
    await same_documents(context, with_clone(before, collection, clone), "valid-clone-insert")
    clone["schemaVersion"] = 2
    replaced = await context.db[collection].replace_one({"_id": clone["_id"]}, clone)
    assert replaced.matched_count == replaced.modified_count == 1
    await same_documents(context, with_clone(before, collection, clone), "valid-clone-replace")
    clone["schemaVersion"] = 3
    updated = await context.db[collection].update_one({"_id": clone["_id"]}, {"$set": {"schemaVersion": 3}})
    assert updated.matched_count == updated.modified_count == 1
    await same_documents(context, with_clone(before, collection, clone), "valid-clone-update")
    deleted = await context.db[collection].delete_one({"_id": clone["_id"]})
    assert deleted.deleted_count == 1
    await same_documents(context, before, "valid-clone-cleanup")


@pytest.mark.parametrize("collection", list(REQUIRED_PATHS))
async def test_required_fields_rejected_on_insert_replace_and_unset(context, collection):
    before, documents = await prepared_documents(context)
    paths = REQUIRED_PATHS[collection]
    for path in paths:
        await exercise_rejection(context, collection, documents[collection], before, path, remove=True)
    await valid_round_trip(context, collection, documents[collection], before)
    proof("required-fields", collection=collection, paths=paths, pathCount=len(paths),
          topLevelPaths=sum("." not in path for path in paths),
          nestedPaths=sum("." in path for path in paths), writeForms=["insert", "replace", "$unset-with-valid-marker"],
          rejectedOperations=len(paths) * 3, mongoErrorCode=121,
          fourCollectionsUnchangedAfterEveryRejection=True, validInsertReplaceUpdate=True,
          cloneRemovedAndBaselineRestored=True, strictErrorValidatorVerified=True)


@pytest.mark.parametrize("collection", list(INVALID_VALUES))
async def test_other_collection_bson_shapes_rejected_by_mongo(context, collection):
    before, documents = await prepared_documents(context)
    cases = INVALID_VALUES[collection]
    for path, value in cases:
        await exercise_rejection(context, collection, documents[collection], before, path, value)
    await valid_round_trip(context, collection, documents[collection], before)
    proof("invalid-bson-shapes", collection=collection, paths=[path for path, _ in cases],
          mutationCount=len(cases), writeForms=["insert", "replace", "$set-with-valid-marker"],
          rejectedOperations=len(cases) * 3, mongoErrorCode=121,
          fourCollectionsUnchangedAfterEveryRejection=True, validInsertReplaceUpdate=True,
          cloneRemovedAndBaselineRestored=True, strictErrorValidatorVerified=True)


async def test_anonymous_optional_fields_and_numeric_bson_types_are_accepted(context):
    before, _ = await prepared_documents(context)
    anonymous = next(item for item in before["responses"] if item["anonymous"] is True)
    assert "identity" not in anonymous and "oneTimeParticipantKey" not in anonymous
    observed = []
    for with_one_time in (False, True):
        clone = unique_clone("responses", anonymous)
        score = next(answer for answer in clone["answers"] if answer["questionId"] == "q_score")
        score["value"] = Int64(0) if with_one_time else 0.0
        if with_one_time:
            clone["oneTimeParticipantKey"] = clone["participantKey"]
        result = await context.db.responses.insert_one(clone)
        assert result.acknowledged
        await same_documents(context, with_clone(before, "responses", clone), "valid-anonymous-optional-fields")
        stored = await context.db.responses.find_one({"_id": clone["_id"]})
        assert stored["anonymous"] is True and "identity" not in stored
        assert ("oneTimeParticipantKey" in stored) is with_one_time
        stored_score = next(answer["value"] for answer in stored["answers"] if answer["questionId"] == "q_score")
        assert stored_score == 0
        assert isinstance(stored_score, Int64 if with_one_time else float)
        if with_one_time:
            equal_keys = stored["oneTimeParticipantKey"] == stored["participantKey"]
            assert equal_keys, "Optional one-time key did not preserve the participant equality"
        removed = await context.db.responses.delete_one({"_id": clone["_id"]})
        assert removed.deleted_count == 1
        await same_documents(context, before, "anonymous-clone-cleanup")
        observed.append({"identityAbsent": True, "oneTimeFieldPresent": with_one_time,
                         "scoreBsonType": "long" if with_one_time else "double", "zeroPreserved": True})
    proof("valid-optional-and-bson-types", cases=observed, acceptedInserts=2,
          fourCollectionsRestoredAfterEach=True, strictErrorValidatorVerified=True)
