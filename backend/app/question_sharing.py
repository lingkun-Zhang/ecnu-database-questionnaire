"""Explicit series-use grants; survey ownership and answer rights stay separate."""
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from .errors import ServiceError
from .repositories import Repository
from .security import digest, normalize_username, utcnow


def grant_filter(question, recipient_id):
    return {"questionId": question["_id"], "ownerId": question["ownerId"],
            "recipientId": recipient_id, "active": True,
            "permission": "use", "versionScope": "series"}


async def readable_question(db, question_id, user, session=None):
    question = await db.questions.find_one({"_id": question_id}, session=session)
    if question and (question["ownerId"] == user["_id"] or await db.question_shares.find_one(
            grant_filter(question, user["_id"]), {"_id": 1}, session=session)):
        return question
    raise ServiceError("NOT_FOUND", "题目不存在或当前不可读取", 404)


async def with_current_version_summaries(db, questions, session):
    """Attach only list display fields, using the same permission snapshot."""
    if not questions:
        return []
    ids = list({row["currentVersionId"] for row in questions})
    versions = await db.question_versions.find({"_id": {"$in": ids}},
        {"questionId": 1, "versionNo": 1, "content.kind": 1, "content.title": 1},
        session=session).to_list(length=len(ids))
    by_id = {row["_id"]: row for row in versions}
    result = []
    for question in questions:
        version = by_id.get(question["currentVersionId"])
        if not version or version["questionId"] != question["_id"]:
            raise ServiceError("DATA_INVARIANT", "题目当前版本缺失或不属于该题目", 500)
        summary = {"id": version["_id"], "questionId": version["questionId"],
                   "versionNo": version["versionNo"], "content": version["content"]}
        result.append({**question, "currentVersion": summary})
    return result


def share_view(document, recipient_username=None):
    return {"id": document["_id"], **{key: document[key] for key in
            ("questionId", "recipientId", "active", "revision", "permission", "versionScope",
             "createdAt", "updatedAt")}, "recipientUsername": recipient_username}


class QuestionSharingServices:
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

    async def list_shares(self, user, question_id, limit, after_id=None):
        await self.owned(question_id, user)
        query = {"questionId": question_id, "ownerId": user["_id"]}
        if after_id is not None:
            query["_id"] = {"$gt": after_id}
        rows = await self.db.question_shares.find(query).sort("_id", 1).limit(limit + 1).to_list(length=limit + 1)
        users = await self.db.users.find(
            {"_id": {"$in": [row["recipientId"] for row in rows[:limit]]}},
            {"username": 1}).to_list(length=limit)
        names = {user["_id"]: user["username"] for user in users}
        return {"items": [share_view(row, names.get(row["recipientId"])) for row in rows[:limit]],
                "limit": limit, "nextAfterId": rows[limit - 1]["_id"] if len(rows) > limit else None}

    async def received_questions(self, user, limit, after_id=None, include_current_version=False):
        query = {"recipientId": user["_id"], "active": True, "permission": "use", "versionScope": "series"}
        if after_id is not None:
            query["_id"] = {"$gt": after_id}
        pipeline = [{"$match": query}, {"$sort": {"_id": 1}},
                    {"$lookup": {"from": "questions", "localField": "questionId", "foreignField": "_id", "as": "question"}},
                    {"$unwind": "$question"},
                    {"$match": {"$expr": {"$eq": ["$ownerId", "$question.ownerId"]}}},
                    {"$limit": limit + 1}]

        async def read(session=None):
            rows = await (await self.db.question_shares.aggregate(
                pipeline, session=session, maxTimeMS=10000)).to_list(length=limit + 1)
            questions = [row["question"] for row in rows[:limit]]
            if include_current_version:
                questions = await with_current_version_summaries(self.db, questions, session)
            return {"items": questions, "limit": limit,
                    "nextAfterId": rows[limit - 1]["_id"] if len(rows) > limit else None}

        # Legacy root-only responses keep their shape. Opt-in summaries and
        # active grants/current heads are read atomically, not in N HTTP calls.
        return await self.repo.transaction(read) if include_current_version else await read()

    async def set_share(self, user, question_id, recipient_username, active, expected_revision):
        # Check ownership first: an unrelated caller cannot probe recipients via this endpoint.
        await self.owned(question_id, user)
        try:
            _, username_key = normalize_username(recipient_username)
        except ValueError as exc:
            raise ServiceError("INVALID_USERNAME", str(exc), 422) from exc
        recipient = await self.db.users.find_one({"usernameKey": username_key}, {"username": 1})
        if not recipient:
            raise ServiceError("RECIPIENT_NOT_FOUND", "接收用户不存在，请核对用户名", 404)
        if recipient["_id"] == user["_id"]:
            raise ServiceError("INVALID_SHARE_RECIPIENT", "自己的题目可直接使用，无需共享给自己", 422)
        query = {"questionId": question_id, "recipientId": recipient["_id"]}
        request_digest = digest({**query, "ownerId": user["_id"], "active": active,
                                 "expectedRevision": expected_revision})

        async def recover():
            document = await self.db.question_shares.find_one({**query, "ownerId": user["_id"]})
            if document and document["requestDigest"] == request_digest:
                return share_view(document, recipient["username"]), True

        previous = await recover()
        if previous:
            return previous

        async def operation(session):
            await self.owned(question_id, user, session)
            current = await self.db.question_shares.find_one(query, session=session)
            if current and current["ownerId"] != user["_id"]:
                raise ServiceError("DATA_INVARIANT", "共享记录与题目所有者不一致", 500)
            if current and current["requestDigest"] == request_digest:
                return share_view(current, recipient["username"]), True
            if (current and current["revision"] != expected_revision) or (not current and expected_revision != 0):
                raise ServiceError("QUESTION_SHARE_CONFLICT", "共享状态已变化，请重新读取后确认")
            now = self.clock()
            if current:
                document = await self.db.question_shares.find_one_and_update(
                    {"_id": current["_id"], "ownerId": user["_id"], "revision": expected_revision},
                    {"$set": {"active": active, "requestDigest": request_digest, "updatedAt": now},
                     "$inc": {"revision": 1}}, return_document=ReturnDocument.AFTER, session=session)
                if not document:
                    raise ServiceError("QUESTION_SHARE_CONFLICT", "更新时共享状态已变化，请重新读取后确认")
            else:
                if not active:
                    raise ServiceError("SHARE_NOT_FOUND", "尚未向该用户共享题目", 404)
                document = {"_id": str(uuid4()), **query, "ownerId": user["_id"], "active": True,
                            "revision": 1, "useSequence": 0, "permission": "use", "versionScope": "series",
                            "requestDigest": request_digest, "createdAt": now, "updatedAt": now, "schemaVersion": 1}
                await self.db.question_shares.insert_one(document, session=session)
            await self.hook("after_question_share_write", document=document)
            return share_view(document, recipient["username"]), False

        try:
            return await self.repo.transaction(operation)
        except (ServiceError, PyMongoError) as exc:
            try:
                recovered = await recover()
            except PyMongoError as recovery_error:
                raise ServiceError("QUESTION_SHARE_WRITE_OUTCOME_UNKNOWN",
                                   "暂时无法确认共享结果，请保留原请求内容和预期修订重试", 503) from recovery_error
            if recovered:
                return recovered
            if isinstance(exc, PyMongoError):
                raise ServiceError("QUESTION_SHARE_WRITE_OUTCOME_UNKNOWN",
                                   "暂时无法确认共享结果，请保留原请求内容和预期修订重试", 503) from exc
            raise
