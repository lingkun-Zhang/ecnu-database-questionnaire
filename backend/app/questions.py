"""Reusable questions and append-only content versions; management stays owner-only.

    Existing survey definitions, snapshots and responses are not changed here.
"""

from copy import deepcopy
from uuid import uuid4

from pymongo.errors import PyMongoError

from .question_content import canonical_question_content, validate_question_content
from .question_sharing import readable_question
from .question_usage import (DRAFT_USAGE_PROJECTION, draft_usage_filter,
                             matching_instances, published_usage_pipeline)
from .repositories import Repository
from .security import digest, utcnow
from .services import ServiceError


def question_view(document):
    return {"id": document["_id"], **{key: document[key] for key in
            ("currentVersionId", "lastVersionNo", "createdAt", "updatedAt")}}


def question_version_view(document):
    result = {"id": document["_id"], **{key: document[key] for key in
              ("questionId", "versionNo", "parentVersionId", "content", "createdAt")}}
    # Missing metadata stays missing: do not invent actions for legacy versions.
    result.update({key: document[key] for key in ("operation", "previousCurrentVersionId") if key in document})
    return result


class QuestionServices:
    def __init__(self, db, settings, clock=utcnow, hooks=None):
        self.db, self.settings, self.clock, self.hooks = db, settings, clock, hooks or {}
        self.repo = Repository(db, settings.transaction_seconds)

    async def hook(self, name, **context):
        if self.settings.testing and name in self.hooks:
            await self.hooks[name](**context)

    async def owned(self, question_id, user, session=None):
        question = await self.db.questions.find_one(
            {"_id": question_id, "ownerId": user["_id"]}, session=session)
        if not question:
            raise ServiceError("NOT_FOUND", "题目不存在", 404)
        return question

    async def readable(self, question_id, user, session=None):
        return await readable_question(self.db, question_id, user, session)

    async def version(self, question_id, version_id, session=None):
        document = await self.db.question_versions.find_one(
            {"_id": version_id, "questionId": question_id}, session=session)
        if not document:
            raise ServiceError("QUESTION_VERSION_NOT_FOUND", "题目版本不存在", 404)
        return document

    async def usages(self, user, question_id, scope, version_id=None, limit=30, after_id=None):
        await self.readable(question_id, user)
        if version_id is not None:
            await self.version(question_id, version_id)
        if scope == "draft":
            rows = await self.db.surveys.find(
                draft_usage_filter(user["_id"], question_id, version_id, after_id),
                DRAFT_USAGE_PROJECTION).sort("_id", 1).limit(limit + 1).max_time_ms(10000).to_list(length=limit + 1)
        else:
            cursor = await self.db.survey_versions.aggregate(
                published_usage_pipeline(user["_id"], question_id, version_id, after_id, limit),
                maxTimeMS=10000)
            rows = await cursor.to_list(length=limit + 1)
        items = []
        for row in rows[:limit]:
            if scope == "draft":
                item = {"surveyId": row["_id"], "surveyTitle": row["draft"]["title"],
                        "surveyStatus": row["status"], "draftRevision": row["draftRevision"],
                        "instances": matching_instances(row["draftQuestionSources"], question_id, version_id)}
            else:
                item = {"surveyId": row["surveyId"], "surveyTitle": row["definition"]["title"],
                        "surveyStatus": row["surveyStatus"], "surveyVersionId": row["_id"],
                        "versionNo": row["versionNo"], "publishedAt": row["publishedAt"],
                        "isCurrent": row["isCurrent"],
                        "instances": matching_instances(row["questionSources"], question_id, version_id)}
            items.append(item)
        return {"questionId": question_id, "questionVersionId": version_id, "scope": scope,
                "limit": limit, "items": items,
                "nextAfterId": rows[limit - 1]["_id"] if len(rows) > limit else None}

    def content(self, raw):
        errors = validate_question_content(raw)
        if errors:
            raise ServiceError("INVALID_QUESTION_CONTENT", "请修正题目内容和约束", 422, errors)
        return canonical_question_content(raw)

    @staticmethod
    def replay(document, request_digest):
        if document["requestDigest"] != request_digest:
            raise ServiceError("IDEMPOTENCY_CONFLICT", "该操作标识已用于不同题目内容或版本预期")
        return question_version_view(document)

    async def create(self, user, raw_content, key):
        content = self.content(raw_content)
        request_digest = digest({"operation": "create_question", "ownerId": user["_id"],
                                 "content": content})

        async def recover():
            question = await self.db.questions.find_one({"ownerId": user["_id"], "creationKey": key})
            if question:
                # Creation always recovers v1, even after the current pointer moves.
                first = await self.db.question_versions.find_one(
                    {"questionId": question["_id"], "versionNo": 1})
                if not first:
                    raise ServiceError("DATA_INVARIANT", "题目初始版本缺失", 500)
                return self.replay(first, request_digest)

        previous = await recover()
        if previous:
            return previous, True

        async def operation(session):
            now = self.clock()
            question_id, version_id = str(uuid4()), str(uuid4())
            question = {"_id": question_id, "ownerId": user["_id"], "creationKey": key,
                        "currentVersionId": version_id, "lastVersionNo": 1,
                        "createdAt": now, "updatedAt": now, "schemaVersion": 1}
            document = {"_id": version_id, "questionId": question_id, "versionNo": 1,
                        "parentVersionId": None, "content": deepcopy(content),
                        "requestKey": key, "requestDigest": request_digest,
                        "createdAt": now, "schemaVersion": 1}
            await self.db.questions.insert_one(question, session=session)
            await self.db.question_versions.insert_one(document, session=session)
            await self.hook("after_question_version_insert", document=document)
            return question_version_view(document)

        return await self.write(operation, recover)

    async def derive(self, user, question_id, parent_id, expected_current, raw_content, key):
        question = await self.owned(question_id, user)
        content = self.content(raw_content)
        request_digest = digest({"operation": "derive_question_version", "questionId": question_id,
                                 "parentVersionId": parent_id, "expectedCurrentVersionId": expected_current,
                                 "content": content})
        return await self._append_version(user, question, parent_id, expected_current, key,
                                          request_digest, content=content)

    async def restore(self, user, question_id, target_id, expected_current, key):
        question = await self.owned(question_id, user)
        request_digest = digest({"operation": "restore_question_version", "questionId": question_id,
                                 "targetVersionId": target_id, "expectedCurrentVersionId": expected_current})
        return await self._append_version(user, question, target_id, expected_current, key,
                                          request_digest, restoring=True)

    async def _append_version(self, user, question, parent_id, expected_current, key,
                              request_digest, *, content=None, restoring=False):
        question_id = question["_id"]

        async def recover():
            document = await self.db.question_versions.find_one({"questionId": question_id, "requestKey": key})
            if document:
                return self.replay(document, request_digest)

        previous = await recover()
        if previous:
            return previous, True
        await self.hook("before_question_version_transaction", question=question)

        async def operation(session):
            current = await self.owned(question_id, user, session)
            if current["currentVersionId"] != expected_current:
                raise ServiceError("QUESTION_VERSION_CONFLICT", "题目的当前版本已变化，请刷新后确认")
            parent = await self.version(question_id, parent_id, session)
            now = self.clock()
            document = {"_id": str(uuid4()), "questionId": question_id,
                        "versionNo": current["lastVersionNo"] + 1, "parentVersionId": parent_id,
                        "content": deepcopy(parent["content"] if restoring else content),
                        "requestKey": key, "requestDigest": request_digest,
                        "createdAt": now, "schemaVersion": 1}
            if restoring:
                document.update({"operation": "restore", "previousCurrentVersionId": current["currentVersionId"]})
            await self.db.question_versions.insert_one(document, session=session)
            await self.hook("after_question_version_insert", document=document)
            result = await self.db.questions.update_one(
                {"_id": question_id, "ownerId": user["_id"], "currentVersionId": expected_current,
                 "lastVersionNo": current["lastVersionNo"]},
                {"$set": {"currentVersionId": document["_id"], "lastVersionNo": document["versionNo"],
                          "updatedAt": now}}, session=session)
            if not result.matched_count:
                raise ServiceError("QUESTION_VERSION_CONFLICT", "创建新版本时题目已变化，请刷新后确认")
            return question_version_view(document)

        return await self.write(operation, recover)

    async def write(self, operation, recover):
        try:
            return await self.repo.transaction(operation), False
        except (ServiceError, PyMongoError) as exc:
            try:
                recovered = await recover()
            except PyMongoError as recovery_error:
                raise ServiceError("QUESTION_WRITE_OUTCOME_UNKNOWN",
                                   "暂时无法确认题目保存结果，请使用原操作标识和内容重试", 503) from recovery_error
            if recovered:
                return recovered, True
            if isinstance(exc, PyMongoError):
                raise ServiceError("QUESTION_WRITE_OUTCOME_UNKNOWN",
                                   "暂时无法确认题目保存结果，请使用原操作标识和内容重试", 503) from exc
            raise
