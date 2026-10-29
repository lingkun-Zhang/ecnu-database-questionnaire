"""Real MongoDB component tests. Each test owns a disposable survey_test_ database.

Run with PYTHONPATH=backend and the project's virtualenv. MongoDB must be a
replica set. API and component fixtures share one session URI: explicit
MONGO_URI, then legacy MONGODB_URI, then the installed local configuration.
"""

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from pymongo import AsyncMongoClient
from pymongo.errors import DuplicateKeyError, WriteError

from app.database import INDEXES, init_database
from app.statistics import StatsError, get_stats


def fixture_definition():
    def question(qid, kind, config, target, required=False, rules=None):
        return {"id": qid, "kind": kind, "title": qid, "required": required,
                "config": config, "defaultTarget": target, "rules": rules or []}

    def options(*ids):
        return [{"id": value, "label": value} for value in ids]

    return {"title": "六份人工核算", "description": "数据库组件测试", "allowAnonymous": True,
            "entryQuestionId": "q_age", "questions": [
        question("q_age", "number", {"min": 0, "max": 120, "integerOnly": True}, "q_adult", True,
                 [{"id": "r_minor", "priority": 10, "condition": {"operator": "lt", "value": 18}, "target": "q_minor"}]),
        question("q_minor", "text", {"minLength": 1, "maxLength": 100}, "q_hobby"),
        question("q_adult", "single", {"options": options("student", "worker", "other")}, "q_hobby", True),
        question("q_hobby", "multi", {"options": options("read", "sport", "music"), "minSelected": 1, "maxSelected": 2}, "q_score", True),
        question("q_score", "number", {"min": 0, "max": 10, "integerOnly": False}, "q_note"),
        question("q_note", "text", {"minLength": 1, "maxLength": 100}, "END"),
    ]}


def fixture_documents():
    now = datetime.now(timezone.utc)
    definition = fixture_definition()
    survey = {"_id": "s1", "ownerId": "u0", "publicId": "public1", "draft": definition,
              "draftRevision": 1, "currentVersionId": "v1", "lastVersionNo": 1,
              "status": "open", "deadlineAt": None, "allowMultiple": True, "policyLocked": True,
              "settingsRevision": 0, "submitSequence": 6, "createdAt": now,
              "updatedAt": now, "schemaVersion": 1}
    version = {"_id": "v1", "surveyId": "s1", "versionNo": 1, "sourceDraftRevision": 1,
               "definition": definition, "publishKey": "publish1", "publishRequestDigest": "digest1",
               "publishedAt": now, "schemaVersion": 1}
    samples = [
        ("u1", True, 17, "学生", None, ["read", "sport"], 0, None),
        ("u2", False, 18, None, "student", ["read"], 8, "课程很好"),
        ("u3", True, 20, None, "worker", ["sport", "music"], 6, None),
        ("u4", False, 16, None, None, ["music"], None, "希望增加活动"),
        ("u1", True, 18, None, "student", ["read", "music"], 10, None),
        ("u5", False, 12, "参加兴趣班", None, ["sport"], 4, None),
    ]
    kinds = {q["id"]: q["kind"] for q in definition["questions"]}
    documents = []
    for seq, (user, anonymous, age, minor, adult, hobbies, score, note) in enumerate(samples, 1):
        path = ["q_age", "q_minor" if age < 18 else "q_adult", "q_hobby", "q_score", "q_note"]
        values = dict(zip(["q_age", "q_minor", "q_adult", "q_hobby", "q_score", "q_note"],
                          [age, minor, adult, hobbies, score, note]))
        document = {"_id": f"r{seq}", "surveyId": "s1", "versionId": "v1", "participantKey": user,
                    "submissionKey": f"submit{seq}", "payloadDigest": f"digest{seq}",
                    "canonicalizationVersion": 1, "anonymous": anonymous,
                    "answers": [{"questionId": qid, "kind": kinds[qid], "value": values[qid]}
                                for qid in path if values[qid] is not None],
                    "path": path, "acceptedSeq": seq, "acceptedAt": now, "schemaVersion": 1}
        if not anonymous:
            document["identity"] = {"userId": user, "usernameAtSubmit": "name_" + user}
        documents.append(document)
    return survey, version, documents


@pytest_asyncio.fixture
async def component_db(mongo_uri):
    client = AsyncMongoClient(
        mongo_uri,
        serverSelectionTimeoutMS=5000,
    )
    name = "survey_test_components_" + uuid4().hex
    created = False
    try:
        await client.admin.command("ping")
        db = client[name]
        await init_database(db)
        created = True
        yield db
    finally:
        try:
            # Never derive a cleanup target from a user-provided database name.
            if created and name.startswith("survey_test_components_"):
                await client.drop_database(name)
        finally:
            await client.close()


async def seed(db):
    survey, version, documents = fixture_documents()
    await db.surveys.insert_one(survey)
    await db.survey_versions.insert_one(version)
    await db.responses.insert_many(documents)
    return survey, version, documents


@pytest.mark.asyncio
async def test_init_is_repeatable_and_empty_draft_is_valid(component_db):
    db = component_db
    await init_database(db)
    for collection, definitions in INDEXES.items():
        indexes = await db[collection].index_information()
        assert all(index.document["name"] in indexes for index in definitions)
    survey, _, _ = fixture_documents()
    survey.update(status="draft", currentVersionId=None, lastVersionNo=0, submitSequence=0,
                  policyLocked=False, draft={"title": "新草稿", "description": "", "allowAnonymous": False,
                                             "entryQuestionId": None, "questions": []})
    await db.surveys.insert_one(survey)
    assert await db.surveys.count_documents({}) == 1


@pytest.mark.asyncio
async def test_six_samples_are_counted_by_mongodb(component_db):
    survey, version, _ = await seed(component_db)
    result = await get_stats(component_db, survey, version)
    assert (result["totalResponses"], result["uniqueParticipants"],
            result["anonymousResponses"], result["namedResponses"]) == (6, 5, 3, 3)
    by = {q["questionId"]: q for q in result["questions"]}
    expected = {"q_age": (6, 6, 0, 0), "q_minor": (3, 2, 1, 3), "q_adult": (3, 3, 0, 3),
                "q_hobby": (6, 6, 0, 0), "q_score": (6, 5, 1, 0), "q_note": (6, 2, 4, 0)}
    for qid, counts in expected.items():
        assert tuple(by[qid][key] for key in ("V", "A", "U", "S")) == counts
    assert by["q_score"]["sum"] == 28
    assert by["q_score"]["avg"] == pytest.approx(5.6)
    assert by["q_age"]["avg"] == pytest.approx(101 / 6)
    assert [(o["count"], o["uniqueParticipants"]) for o in by["q_hobby"]["options"]] == [(3, 2), (3, 3), (3, 3)]
    assert [o["percentage"] for o in by["q_hobby"]["options"]] == [50, 50, 50]
    assert by["q_adult"]["options"][-1]["count"] == 0
    for internal in ("participantKey", "oneTimeParticipantKey", "payloadDigest", "usernameAtSubmit"):
        assert internal not in str(result)


@pytest.mark.asyncio
async def test_fixed_sequence_and_empty_statistics(component_db):
    db = component_db
    survey, version, documents = await seed(db)
    before = await get_stats(db, survey, version, as_of=6)
    newer = {**deepcopy(documents[0]), "_id": "r7", "submissionKey": "submit7", "acceptedSeq": 7}
    async with db.client.start_session() as session:
        async def append(transaction):
            await db.surveys.update_one({"_id": "s1"}, {"$set": {"submitSequence": 7}}, session=transaction)
            await db.responses.insert_one(newer, session=transaction)
        await session.with_transaction(append)
    assert await get_stats(db, survey, version, as_of=6) == before
    assert (await get_stats(db, survey, version))["totalResponses"] == 7
    empty = await get_stats(db, survey, version, as_of=0)
    assert empty["totalResponses"] == empty["uniqueParticipants"] == 0
    assert all(q["R"] == q["A"] == q["V"] == q["U"] == q["S"] == 0 for q in empty["questions"])
    assert all(q["avg"] is None for q in empty["questions"] if q["kind"] == "number")
    assert len((await get_stats(db, survey, version, question_id="q_score"))["questions"]) == 1
    for invalid in (-1, True, 1.5, 8):
        with pytest.raises(StatsError):
            await get_stats(db, survey, version, as_of=invalid)
    with pytest.raises(StatsError):
        await get_stats(db, survey, version, question_id="missing")


@pytest.mark.asyncio
async def test_response_schema_rejects_invalid_bson(component_db):
    db = component_db
    _, _, documents = await seed(db)
    changes = {
        "identity_in_anonymous": {"identity": {"userId": "u1", "usernameAtSubmit": "name_u1"}},
        "boolean_number": {"answers": [{"questionId": "q_age", "kind": "number", "value": True}]},
        "duplicate_answer": {"answers": [documents[0]["answers"][0]] * 2},
        "answer_outside_path": {"path": ["q_note"]},
        "null_once": {"oneTimeParticipantKey": None},
        "wrong_once": {"oneTimeParticipantKey": "other"},
        "unknown_field": {"attackerField": True},
        "wrong_answers_type": {"answers": "bad"},
        "empty_path": {"path": []},
    }
    for label, change in changes.items():
        document = {**deepcopy(documents[0]), "_id": "bad_" + label,
                    "submissionKey": "bad_" + label, "acceptedSeq": 100}
        document.update(change)
        with pytest.raises(WriteError) as failure:
            await db.responses.insert_one(document)
        assert failure.value.code == 121, label
    assert await db.responses.count_documents({}) == 6


@pytest.mark.asyncio
async def test_unique_keys_and_partial_one_time_index(component_db):
    db = component_db
    _, _, documents = await seed(db)
    # A different document ID cannot bypass the same submission key.
    with pytest.raises(DuplicateKeyError):
        await db.responses.insert_one({**documents[0], "_id": "same_request", "acceptedSeq": 10})
    # Omitted one-time keys permit the same participant's distinct submissions.
    assert await db.responses.count_documents({"participantKey": "u1"}) == 2
    first = {**deepcopy(documents[0]), "_id": "once1", "submissionKey": "once1",
             "acceptedSeq": 10, "oneTimeParticipantKey": "u1"}
    await db.responses.insert_one(first)
    with pytest.raises(DuplicateKeyError):
        await db.responses.insert_one({**first, "_id": "once2", "submissionKey": "once2", "acceptedSeq": 11})
    with pytest.raises(DuplicateKeyError):
        await db.responses.insert_one({**deepcopy(documents[1]), "_id": "same_seq", "submissionKey": "new", "acceptedSeq": 10})
    info = await db.responses.index_information()
    partial = info["uq_responses_one_time_participant"]
    assert partial["unique"] is True
    assert partial["partialFilterExpression"] == {"oneTimeParticipantKey": {"$type": "string"}}
