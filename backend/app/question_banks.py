"""Private version-pinned banks organize questions without granting access.

A bank-wide revision serializes metadata and membership decisions. Removing an
item never deletes its question, survey provenance or published content.
"""
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from .errors import ServiceError
from .question_sharing import grant_filter, readable_question
from .questions import question_version_view
from .repositories import Repository
from .security import digest, utcnow


def bank_view(document):
    return {"id": document["_id"], **{key: document[key] for key in
            ("name", "description", "archived", "revision", "createdAt", "updatedAt")}}


def item_view(document):
    return {"id": document["_id"], **{key: document[key] for key in
            ("questionId", "questionVersionId", "createdAt", "updatedAt")}}


class QuestionBankServices:
    def __init__(self, db, settings, clock=utcnow, hooks=None):
        self.db, self.settings, self.clock, self.hooks = db, settings, clock, hooks or {}
        self.repo = Repository(db, settings.transaction_seconds)

    async def hook(self, name, **context):
        if self.settings.testing and name in self.hooks:
            await self.hooks[name](**context)

    async def owned(self, bank_id, user, session=None):
        document = await self.db.question_banks.find_one(
            {"_id": bank_id, "ownerId": user["_id"]}, session=session)
        if not document:
            raise ServiceError("NOT_FOUND", "题库不存在", 404)
        return document

    async def list(self, user, archived=False, limit=30, after_id=None):
        query = {"ownerId": user["_id"], "archived": archived}
        if after_id is not None:
            query["_id"] = {"$gt": after_id}
        rows = await self.db.question_banks.find(query).sort("_id", 1).limit(limit + 1).to_list(length=limit + 1)
        return {"items": [bank_view(row) for row in rows[:limit]], "limit": limit,
                "nextAfterId": rows[limit - 1]["_id"] if len(rows) > limit else None}

    async def items(self, user, bank_id, limit=30, after_id=None):
        # A single snapshot keeps the membership, access decisions and displayed
        # bank revision consistent. No item itself acts as a permission grant.
        async def observe(session):
            bank = await self.owned(bank_id, user, session)
            query = {"bankId": bank_id}
            if after_id is not None:
                query["_id"] = {"$gt": after_id}
            rows = await self.db.question_bank_items.find(query, session=session).sort(
                "_id", 1).limit(limit + 1).to_list(length=limit + 1)
            page = rows[:limit]
            roots = await self.db.questions.find(
                {"_id": {"$in": [row["questionId"] for row in page]}}, session=session).to_list(length=limit)
            questions = {root["_id"]: root for root in roots}
            grants = await self.db.question_shares.find({
                "recipientId": user["_id"], "active": True, "permission": "use", "versionScope": "series",
                "questionId": {"$in": list(questions)},
            }, session=session).to_list(length=limit)
            allowed = {row["questionId"] for row in grants
                       if questions[row["questionId"]]["ownerId"] == row["ownerId"]}
            versions = await self.db.question_versions.find({
                "_id": {"$in": [row["questionVersionId"] for row in page]},
            }, session=session).to_list(length=limit)
            by_id = {version["_id"]: version for version in versions}
            items = []
            for row in page:
                root, version = questions.get(row["questionId"]), by_id.get(row["questionVersionId"])
                available = bool(root and version and version["questionId"] == root["_id"]
                                 and (root["ownerId"] == user["_id"] or root["_id"] in allowed))
                item = {**item_view(row), "available": available}
                if available:
                    item.update(version=question_version_view(version), currentVersionId=root["currentVersionId"])
                items.append(item)
            return {"bank": bank_view(bank), "items": items, "limit": limit,
                    "nextAfterId": page[-1]["_id"] if len(rows) > limit else None}

        return await self.repo.transaction(observe)

    async def create(self, user, name, description, key):
        request_digest = digest({"operation": "create_bank", "ownerId": user["_id"],
                                 "name": name, "description": description})

        async def recover():
            bank = await self.db.question_banks.find_one({"ownerId": user["_id"], "creationKey": key})
            if bank:
                if bank["creationDigest"] != request_digest:
                    raise ServiceError("IDEMPOTENCY_CONFLICT", "该操作标识已用于不同题库内容")
                # Permanent creation identity; later edits are not rolled back.
                return bank_view(bank), True

        previous = await recover()
        if previous:
            return previous

        async def operation(session):
            now = self.clock()
            bank = {"_id": str(uuid4()), "ownerId": user["_id"], "creationKey": key,
                    "creationDigest": request_digest, "name": name, "description": description,
                    "archived": False, "revision": 0, "createdAt": now, "updatedAt": now, "schemaVersion": 1}
            await self.db.question_banks.insert_one(bank, session=session)
            await self.hook("after_bank_metadata_write", session=session, document=bank)
            return bank_view(bank), False

        return await self.write(operation, recover)

    async def update(self, user, bank_id, name, description, archived, expected_revision):
        changes = {"name": name, "description": description, "archived": archived}
        request_digest = digest({"operation": "update_bank", "bankId": bank_id,
                                 "expectedRevision": expected_revision, **changes})
        return await self.mutate(user, bank_id, expected_revision, request_digest, metadata=changes)

    async def set_item(self, user, bank_id, question_id, version_id, expected_revision):
        request_digest = digest({"operation": "set_bank_item", "bankId": bank_id,
                                 "questionId": question_id, "questionVersionId": version_id,
                                 "expectedRevision": expected_revision})

        async def change(session, now):
            question = await readable_question(self.db, question_id, user, session)
            version = await self.db.question_versions.find_one(
                {"_id": version_id, "questionId": question_id}, {"_id": 1}, session=session)
            if not version:
                raise ServiceError("QUESTION_VERSION_NOT_FOUND", "题目版本不存在", 404)
            if question["ownerId"] != user["_id"]:
                await self.hook("before_bank_grant_touch", session=session, question=question)
                gate = await self.db.question_shares.update_one(
                    grant_filter(question, user["_id"]), {"$inc": {"useSequence": 1}}, session=session)
                if not gate.matched_count:
                    raise ServiceError("NOT_FOUND", "题目不存在或当前不可读取", 404)
                await self.hook("after_bank_grant_touch", session=session, question=question)
            query = {"bankId": bank_id, "questionId": question_id}
            document = await self.db.question_bank_items.find_one(query, session=session)
            if document:
                document = await self.db.question_bank_items.find_one_and_update(
                    {"_id": document["_id"]},
                    {"$set": {"questionVersionId": version_id, "updatedAt": now}},
                    return_document=ReturnDocument.AFTER, session=session)
            else:
                document = {"_id": str(uuid4()), **query, "questionVersionId": version_id,
                            "createdAt": now, "updatedAt": now, "schemaVersion": 1}
                await self.db.question_bank_items.insert_one(document, session=session)
            await self.hook("after_bank_item_write", session=session, document=document)

        return await self.mutate(user, bank_id, expected_revision, request_digest, change=change)

    async def remove_item(self, user, bank_id, question_id, expected_revision):
        request_digest = digest({"operation": "remove_bank_item", "bankId": bank_id,
                                 "questionId": question_id, "expectedRevision": expected_revision})

        async def change(session, now):
            document = await self.db.question_bank_items.find_one_and_delete(
                {"bankId": bank_id, "questionId": question_id}, session=session)
            if not document:
                raise ServiceError("BANK_ITEM_NOT_FOUND", "题库中没有这道题", 404)
            await self.hook("after_bank_item_write", session=session, document=document)

        return await self.mutate(user, bank_id, expected_revision, request_digest, change=change)

    async def mutate(self, user, bank_id, expected_revision, request_digest, *, metadata=None, change=None):
        bank = await self.owned(bank_id, user)

        async def recover():
            current = await self.db.question_banks.find_one({
                "_id": bank_id, "ownerId": user["_id"], "lastRequestDigest": request_digest,
            })
            if current:
                return bank_view(current), True

        previous = await recover()
        if previous:
            return previous
        await self.hook("before_bank_transaction", bank=bank)

        async def operation(session):
            current = await self.owned(bank_id, user, session)
            if current.get("lastRequestDigest") == request_digest:
                return bank_view(current), True
            if current["revision"] != expected_revision:
                raise ServiceError("BANK_CONFLICT", "题库已变化，请重新读取后确认")
            if change is not None and current["archived"]:
                raise ServiceError("BANK_ARCHIVED", "题库已归档，请先恢复再调整题目")
            now = self.clock()
            if change is not None:
                await change(session, now)
            document = await self.db.question_banks.find_one_and_update(
                {"_id": bank_id, "ownerId": user["_id"], "revision": expected_revision},
                {"$set": {**(metadata or {}), "lastRequestDigest": request_digest, "updatedAt": now},
                 "$inc": {"revision": 1}}, return_document=ReturnDocument.AFTER, session=session)
            if not document:
                raise ServiceError("BANK_CONFLICT", "更新时题库已变化，请重新读取后确认")
            if metadata is not None:
                await self.hook("after_bank_metadata_write", session=session, document=document)
            return bank_view(document), False

        return await self.write(operation, recover)

    async def write(self, operation, recover):
        try:
            return await self.repo.transaction(operation)
        except (ServiceError, PyMongoError) as exc:
            try:
                recovered = await recover()
            except PyMongoError as recovery_error:
                raise ServiceError("BANK_WRITE_OUTCOME_UNKNOWN",
                                   "暂时无法确认题库保存结果，请保留原请求和预期修订重试", 503) from recovery_error
            if recovered:
                return recovered
            if isinstance(exc, PyMongoError):
                raise ServiceError("BANK_WRITE_OUTCOME_UNKNOWN",
                                   "暂时无法确认题库保存结果，请保留原请求和预期修订重试", 503) from exc
            raise
