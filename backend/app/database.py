"""MongoDB collection contracts and the indexes used by the application.

The application owns the AsyncMongoClient and calls init_database during lifespan.
This module never creates a client or resets existing data.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import CollectionInvalid, OperationFailure


INTEGER = {"bsonType": ["int", "long"]}
NONNEGATIVE = {**INTEGER, "minimum": 0}
POSITIVE = {**INTEGER, "minimum": 1}
STRING_ID = {"bsonType": "string", "minLength": 1, "maxLength": 160}
TOKEN_ID = {**STRING_ID, "pattern": "^[A-Za-z0-9_-]+$"}
DATE = {"bsonType": "date"}
NUMBER = {
    "bsonType": ["int", "long", "double"],
    "minimum": -9007199254740991,
    "maximum": 9007199254740991,
}


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    schema = {
        "bsonType": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


OPTION_SCHEMA = _object(
    {"id": TOKEN_ID, "label": {"bsonType": "string", "minLength": 1, "maxLength": 1000}},
    ["id", "label"],
)

RULE_SCHEMA = _object(
    {
        "id": TOKEN_ID,
        "priority": INTEGER,
        # The domain layer validates the bounded, typed condition grammar.
        "condition": {"bsonType": "object"},
        "target": TOKEN_ID,
    },
    ["id", "priority", "condition", "target"],
)

QUESTION_SCHEMA = _object(
    {
        "id": TOKEN_ID,
        "kind": {"enum": ["single", "multi", "text", "number"]},
        "title": {"bsonType": "string", "minLength": 1, "maxLength": 1000},
        "required": {"bsonType": "bool"},
        "config": _object(
            {
                "options": {"bsonType": "array", "maxItems": 50, "items": OPTION_SCHEMA},
                "minSelected": {"bsonType": ["int", "long", "null"], "minimum": 0},
                "maxSelected": {"bsonType": ["int", "long", "null"], "minimum": 0},
                "minLength": {"bsonType": ["int", "long", "null"], "minimum": 0},
                "maxLength": {"bsonType": ["int", "long", "null"], "minimum": 0},
                "min": {"bsonType": ["int", "long", "double", "null"]},
                "max": {"bsonType": ["int", "long", "double", "null"]},
                "integerOnly": {"bsonType": "bool"},
            },
            [],
        ),
        "defaultTarget": TOKEN_ID,
        "rules": {"bsonType": "array", "maxItems": 20, "items": RULE_SCHEMA},
    },
    ["id", "kind", "title", "required", "config", "defaultTarget", "rules"],
)


def _definition_schema(*, draft: bool) -> dict[str, Any]:
    question_schema = deepcopy(QUESTION_SCHEMA)
    if draft:
        # Editing an unfinished title/option must remain saveable; publishing is
        # checked against the complete definition and the stricter schema.
        question_schema["properties"]["title"]["minLength"] = 0
        question_schema["properties"]["config"]["properties"]["options"]["items"]["properties"]["label"]["minLength"] = 0
    return _object(
        {
            "title": {"bsonType": "string", "minLength": 1, "maxLength": 500},
            "description": {"bsonType": "string", "maxLength": 20000},
            "allowAnonymous": {"bsonType": "bool"},
            "entryQuestionId": (
                {"bsonType": ["string", "null"], "maxLength": 160}
                if draft
                else TOKEN_ID
            ),
            "questions": {
                "bsonType": "array",
                "minItems": 0 if draft else 1,
                "maxItems": 100,
                "items": question_schema,
            },
        },
        ["title", "description", "allowAnonymous", "entryQuestionId", "questions"],
    )


def _answer_variant(kind: str, value: dict[str, Any]) -> dict[str, Any]:
    return _object(
        {"questionId": TOKEN_ID, "kind": {"enum": [kind]}, "value": value},
        ["questionId", "kind", "value"],
    )


ANSWER_SCHEMA = {
    "oneOf": [
        _answer_variant("single", TOKEN_ID),
        _answer_variant(
            "multi",
            {
                "bsonType": "array",
                "minItems": 1,
                "maxItems": 50,
                "uniqueItems": True,
                "items": TOKEN_ID,
            },
        ),
        _answer_variant("text", {"bsonType": "string", "minLength": 1, "maxLength": 10000}),
        _answer_variant("number", NUMBER),
    ]
}

USER_SCHEMA = _object(
    {
        "_id": STRING_ID,
        "username": {"bsonType": "string", "minLength": 3, "maxLength": 32},
        # Unicode casefold can expand characters, so it is not capped at 32.
        "usernameKey": {"bsonType": "string", "minLength": 1, "maxLength": 128},
        "passwordHash": {"bsonType": "string", "minLength": 1, "maxLength": 2000},
        "createdAt": DATE,
        "schemaVersion": POSITIVE,
    },
    ["_id", "username", "usernameKey", "passwordHash", "createdAt", "schemaVersion"],
)

SURVEY_SCHEMA = _object(
    {
        "_id": STRING_ID,
        "ownerId": STRING_ID,
        "publicId": STRING_ID,
        "draft": _definition_schema(draft=True),
        "draftRevision": NONNEGATIVE,
        "currentVersionId": {"bsonType": ["string", "null"], "maxLength": 160},
        "lastVersionNo": NONNEGATIVE,
        "status": {"enum": ["draft", "open", "closed"]},
        "deadlineAt": {"bsonType": ["date", "null"]},
        "allowMultiple": {"bsonType": "bool"},
        "policyLocked": {"bsonType": "bool"},
        "settingsRevision": NONNEGATIVE,
        "submitSequence": NONNEGATIVE,
        "createdAt": DATE,
        "updatedAt": DATE,
        "schemaVersion": POSITIVE,
    },
    [
        "_id", "ownerId", "publicId", "draft", "draftRevision", "currentVersionId",
        "lastVersionNo", "status", "deadlineAt", "allowMultiple", "policyLocked",
        "settingsRevision", "submitSequence", "createdAt", "updatedAt", "schemaVersion",
    ],
)

VERSION_SCHEMA = _object(
    {
        "_id": STRING_ID,
        "surveyId": STRING_ID,
        "versionNo": POSITIVE,
        "sourceDraftRevision": NONNEGATIVE,
        "definition": _definition_schema(draft=False),
        "publishKey": STRING_ID,
        "publishRequestDigest": {"bsonType": "string", "minLength": 1, "maxLength": 256},
        "publishedAt": DATE,
        "schemaVersion": POSITIVE,
    },
    [
        "_id", "surveyId", "versionNo", "sourceDraftRevision", "definition",
        "publishKey", "publishRequestDigest", "publishedAt", "schemaVersion",
    ],
)

RESPONSE_SCHEMA = _object(
    {
        "_id": STRING_ID,
        "surveyId": STRING_ID,
        "versionId": STRING_ID,
        "participantKey": STRING_ID,
        "submissionKey": STRING_ID,
        "payloadDigest": {"bsonType": "string", "minLength": 1, "maxLength": 256},
        "canonicalizationVersion": POSITIVE,
        "anonymous": {"bsonType": "bool"},
        "identity": _object(
            {"userId": STRING_ID, "usernameAtSubmit": {"bsonType": "string", "minLength": 3, "maxLength": 32}},
            ["userId", "usernameAtSubmit"],
        ),
        "answers": {"bsonType": "array", "maxItems": 100, "items": ANSWER_SCHEMA},
        "path": {"bsonType": "array", "minItems": 1, "maxItems": 100, "uniqueItems": True, "items": TOKEN_ID},
        "acceptedSeq": POSITIVE,
        "acceptedAt": DATE,
        "oneTimeParticipantKey": STRING_ID,
        "schemaVersion": POSITIVE,
    },
    [
        "_id", "surveyId", "versionId", "participantKey", "submissionKey",
        "payloadDigest", "canonicalizationVersion", "anonymous", "answers", "path",
        "acceptedSeq", "acceptedAt", "schemaVersion",
    ],
)
RESPONSE_SCHEMA["oneOf"] = [
    {"properties": {"anonymous": {"enum": [True]}}, "not": {"required": ["identity"]}},
    {"properties": {"anonymous": {"enum": [False]}}, "required": ["identity"]},
]

# Bounded provenance is separate from the unchanged executable definition.
# Existing documents may omit it; cross-collection authenticity is a service rule.
QUESTION_SOURCE_SCHEMA = _object(
    {"instanceId": {**TOKEN_ID, "maxLength": 64}, "questionId": STRING_ID, "questionVersionId": STRING_ID},
    ["instanceId", "questionId", "questionVersionId"],
)
QUESTION_SOURCES_SCHEMA = {
    "bsonType": "array", "maxItems": 100, "uniqueItems": True, "items": QUESTION_SOURCE_SCHEMA,
}
SURVEY_SCHEMA["properties"]["draftQuestionSources"] = deepcopy(QUESTION_SOURCES_SCHEMA)
VERSION_SCHEMA["properties"]["questionSources"] = deepcopy(QUESTION_SOURCES_SCHEMA)

VALIDATORS: dict[str, dict[str, Any]] = {
    "users": {"$jsonSchema": USER_SCHEMA},
    "surveys": {"$jsonSchema": SURVEY_SCHEMA},
    "survey_versions": {"$jsonSchema": VERSION_SCHEMA},
    "responses": {
        "$and": [
            {"$jsonSchema": RESPONSE_SCHEMA},
            {
                "$expr": {
                    "$let": {
                        "vars": {
                            "safeAnswers": {"$cond": [{"$isArray": "$answers"}, "$answers", []]},
                            "safePath": {"$cond": [{"$isArray": "$path"}, "$path", []]},
                        },
                        "in": {
                            "$let": {
                                "vars": {"ids": {"$map": {"input": "$$safeAnswers", "as": "answer", "in": "$$answer.questionId"}}},
                                "in": {"$and": [
                                    {"$eq": [{"$size": "$$safeAnswers"}, {"$size": {"$setUnion": ["$$ids", []]}}]},
                                    {"$setIsSubset": ["$$ids", "$$safePath"]},
                                    {"$or": [
                                        {"$eq": [{"$type": "$oneTimeParticipantKey"}, "missing"]},
                                        {"$eq": ["$oneTimeParticipantKey", "$participantKey"]},
                                    ]},
                                ]},
                            }
                        },
                    }
                }
            },
        ]
    },
}

INDEXES: dict[str, list[IndexModel]] = {
    "users": [IndexModel([("usernameKey", ASCENDING)], unique=True, name="uq_users_username_key")],
    "surveys": [
        IndexModel([("publicId", ASCENDING)], unique=True, name="uq_surveys_public_id"),
        IndexModel([("ownerId", ASCENDING), ("createdAt", DESCENDING), ("_id", DESCENDING)], name="ix_surveys_owner_created"),
        # Series and exact-version usage have different equality prefixes; keep
        # _id after those prefixes for container keyset pagination.
        IndexModel([("ownerId", ASCENDING), ("draftQuestionSources.questionId", ASCENDING),
                    ("_id", ASCENDING)], name="ix_surveys_question_usage"),
        IndexModel([("ownerId", ASCENDING), ("draftQuestionSources.questionId", ASCENDING),
                    ("draftQuestionSources.questionVersionId", ASCENDING), ("_id", ASCENDING)],
                   name="ix_surveys_question_version_usage"),
    ],
    "survey_versions": [
        IndexModel([("surveyId", ASCENDING), ("versionNo", ASCENDING)], unique=True, name="uq_versions_survey_number"),
        IndexModel([("surveyId", ASCENDING), ("publishKey", ASCENDING)], unique=True, name="uq_versions_publish_key"),
        IndexModel([("questionSources.questionId", ASCENDING), ("_id", ASCENDING)],
                   name="ix_versions_question_usage"),
        IndexModel([("questionSources.questionId", ASCENDING),
                    ("questionSources.questionVersionId", ASCENDING), ("_id", ASCENDING)],
                   name="ix_versions_question_version_usage"),
    ],
    "responses": [
        IndexModel([("surveyId", ASCENDING), ("participantKey", ASCENDING), ("submissionKey", ASCENDING)], unique=True, name="uq_responses_submission"),
        IndexModel(
            [("surveyId", ASCENDING), ("oneTimeParticipantKey", ASCENDING)],
            unique=True,
            partialFilterExpression={"oneTimeParticipantKey": {"$type": "string"}},
            name="uq_responses_one_time_participant",
        ),
        IndexModel([("surveyId", ASCENDING), ("acceptedSeq", ASCENDING)], unique=True, name="uq_responses_accepted_sequence"),
        IndexModel([("surveyId", ASCENDING), ("versionId", ASCENDING), ("acceptedSeq", DESCENDING)], name="ix_responses_version_sequence"),
    ],
}


# Independent entities follow; legacy fields above retain their constraints,
# with only optional provenance added to surveys and survey_versions.
QUESTION_ITEM_SCHEMA = _object(
    {
        "_id": STRING_ID, "ownerId": STRING_ID,
        "creationKey": {"bsonType": "string", "minLength": 1, "maxLength": 128},
        "currentVersionId": STRING_ID, "lastVersionNo": POSITIVE,
        "createdAt": DATE, "updatedAt": DATE, "schemaVersion": POSITIVE,
    },
    ["_id", "ownerId", "creationKey", "currentVersionId", "lastVersionNo",
     "createdAt", "updatedAt", "schemaVersion"],
)


def _question_content_schema():
    # Structural guarantees per kind; cross-field meaning remains domain logic.
    variants = []
    fields = {"single": ["options"], "multi": ["options", "minSelected", "maxSelected"],
              "text": ["minLength", "maxLength"], "number": ["min", "max", "integerOnly"]}
    config_properties = QUESTION_SCHEMA["properties"]["config"]["properties"]
    for kind, names in fields.items():
        properties = {name: deepcopy(config_properties[name]) for name in names}
        if "options" in properties:
            properties["options"]["minItems"] = 1
        variants.append(_object({
            "kind": {"enum": [kind]},
            "title": {"bsonType": "string", "minLength": 1, "maxLength": 500},
            "config": _object(properties, names),
        }, ["kind", "title", "config"]))
    return {"oneOf": variants}


QUESTION_VERSION_SCHEMA = _object(
    {
        "_id": STRING_ID, "questionId": STRING_ID, "versionNo": POSITIVE,
        "parentVersionId": {"bsonType": ["string", "null"], "minLength": 1, "maxLength": 160},
        "operation": {"enum": ["restore"]},
        "previousCurrentVersionId": STRING_ID,
        "content": _question_content_schema(),
        "requestKey": {"bsonType": "string", "minLength": 1, "maxLength": 128},
        "requestDigest": {"bsonType": "string", "pattern": "^[a-f0-9]{64}$"},
        "createdAt": DATE, "schemaVersion": POSITIVE,
    },
    ["_id", "questionId", "versionNo", "parentVersionId", "content",
     "requestKey", "requestDigest", "createdAt", "schemaVersion"],
)
QUESTION_VERSION_SCHEMA["oneOf"] = [
    {"properties": {"versionNo": {"enum": [1]}, "parentVersionId": {"bsonType": "null"}}},
    {"properties": {"versionNo": {"minimum": 2}, "parentVersionId": STRING_ID}},
]
# Optional restore metadata is a complete pair; legacy versions omit both.
# Cross-document ownership and lineage remain service constraints.
QUESTION_VERSION_SCHEMA["allOf"] = [{"oneOf": [
    {"not": {"anyOf": [{"required": ["operation"]}, {"required": ["previousCurrentVersionId"]}]}},
    {"required": ["operation", "previousCurrentVersionId"],
     "properties": {"versionNo": {"minimum": 2}}},
]}]

VALIDATORS.update({
    "questions": {"$jsonSchema": QUESTION_ITEM_SCHEMA},
    "question_versions": {"$jsonSchema": QUESTION_VERSION_SCHEMA},
})
INDEXES.update({
    "questions": [
        IndexModel([("ownerId", ASCENDING), ("creationKey", ASCENDING)], unique=True,
                   name="uq_questions_owner_creation"),
        IndexModel([("ownerId", ASCENDING), ("createdAt", DESCENDING), ("_id", DESCENDING)],
                   name="ix_questions_owner_created"),
    ],
    "question_versions": [
        IndexModel([("questionId", ASCENDING), ("versionNo", ASCENDING)], unique=True,
                   name="uq_question_versions_number"),
        IndexModel([("questionId", ASCENDING), ("requestKey", ASCENDING)], unique=True,
                   name="uq_question_versions_request"),
    ],
})


# Independent, revocable use grants do not grow arrays on question roots.
# revision orders owner decisions; useSequence orders authorized binding
# and bank membership writes against revocation. It is not a usage count.
QUESTION_SHARE_SCHEMA = _object(
    {"_id": STRING_ID, "questionId": STRING_ID, "ownerId": STRING_ID, "recipientId": STRING_ID,
     "active": {"bsonType": "bool"}, "revision": POSITIVE, "useSequence": NONNEGATIVE,
     "permission": {"enum": ["use"]}, "versionScope": {"enum": ["series"]},
     "requestDigest": {"bsonType": "string", "pattern": "^[a-f0-9]{64}$"},
     "createdAt": DATE, "updatedAt": DATE, "schemaVersion": POSITIVE},
    ["_id", "questionId", "ownerId", "recipientId", "active", "revision", "useSequence",
     "permission", "versionScope", "requestDigest", "createdAt", "updatedAt", "schemaVersion"],
)
VALIDATORS["question_shares"] = {"$jsonSchema": QUESTION_SHARE_SCHEMA}
INDEXES["question_shares"] = [
    IndexModel([("questionId", ASCENDING), ("recipientId", ASCENDING)], unique=True,
               name="uq_question_shares_recipient"),
    IndexModel([("questionId", ASCENDING), ("ownerId", ASCENDING), ("_id", ASCENDING)],
               name="ix_question_shares_owner_page"),
    IndexModel([("recipientId", ASCENDING), ("active", ASCENDING), ("permission", ASCENDING),
                ("versionScope", ASCENDING), ("_id", ASCENDING)], name="ix_question_shares_received"),
]


# Bank membership can grow independently; only immutable version references are
# stored here. Cross-document ownership and live share rights are service rules.
QUESTION_BANK_SCHEMA = _object(
    {"_id": STRING_ID, "ownerId": STRING_ID,
     "creationKey": {"bsonType": "string", "minLength": 1, "maxLength": 128},
     "creationDigest": {"bsonType": "string", "pattern": "^[a-f0-9]{64}$"},
     "lastRequestDigest": {"bsonType": "string", "pattern": "^[a-f0-9]{64}$"},
     "name": {"bsonType": "string", "minLength": 1, "maxLength": 120},
     "description": {"bsonType": "string", "maxLength": 2000},
     "archived": {"bsonType": "bool"}, "revision": NONNEGATIVE,
     "createdAt": DATE, "updatedAt": DATE, "schemaVersion": POSITIVE},
    ["_id", "ownerId", "creationKey", "creationDigest", "name", "description", "archived",
     "revision", "createdAt", "updatedAt", "schemaVersion"],
)
QUESTION_BANK_ITEM_SCHEMA = _object(
    {"_id": STRING_ID, "bankId": STRING_ID, "questionId": STRING_ID, "questionVersionId": STRING_ID,
     "createdAt": DATE, "updatedAt": DATE, "schemaVersion": POSITIVE},
    ["_id", "bankId", "questionId", "questionVersionId", "createdAt", "updatedAt", "schemaVersion"],
)
VALIDATORS.update({
    "question_banks": {"$jsonSchema": QUESTION_BANK_SCHEMA},
    "question_bank_items": {"$jsonSchema": QUESTION_BANK_ITEM_SCHEMA},
})
INDEXES.update({
    "question_banks": [
        IndexModel([("ownerId", ASCENDING), ("creationKey", ASCENDING)], unique=True,
                   name="uq_question_banks_creation"),
        IndexModel([("ownerId", ASCENDING), ("archived", ASCENDING), ("_id", ASCENDING)],
                   name="ix_question_banks_owner_page"),
    ],
    "question_bank_items": [
        IndexModel([("bankId", ASCENDING), ("questionId", ASCENDING)], unique=True,
                   name="uq_question_bank_items_question"),
        IndexModel([("bankId", ASCENDING), ("_id", ASCENDING)], name="ix_question_bank_items_page"),
    ],
})


async def init_database(db: Any) -> None:
    """Create or update contracts without dropping collections or business data.

    Existing invalid documents are not silently repaired. MongoDB will enforce the
    strict validator on subsequent writes, and conflicting indexes fail startup.
    """
    existing = set(await db.list_collection_names())
    for name, validator in VALIDATORS.items():
        options = {
            "validator": deepcopy(validator),
            "validationLevel": "strict",
            "validationAction": "error",
        }
        if name not in existing:
            try:
                await db.create_collection(name, **options)
            except (CollectionInvalid, OperationFailure) as exc:
                # Another app process may have initialized the same database.
                if not isinstance(exc, CollectionInvalid) and exc.code != 48:
                    raise
                await db.command({"collMod": name, **options})
        else:
            await db.command({"collMod": name, **options})
        await db[name].create_indexes(INDEXES[name])
