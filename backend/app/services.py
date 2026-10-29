import asyncio
from copy import deepcopy
from uuid import uuid4

from bson import Int64
import jwt
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from .domain import evaluate, validate_definition, _valid_id
from .question_content import canonical_question_content, validate_question_content
from .design_checks import analyze_definition
from .repositories import Repository
from .errors import ServiceError
from .question_sharing import grant_filter
from .security import (utcnow, normalize_username, PASSWORDS, verify_password, token,
                       decode_token, participant_key, canonical_answers, submission_digest, digest)


def public_user(user):
    return {"id": user["_id"], "username": user["username"], "createdAt": user["createdAt"]}


def survey_view(survey):
    allowed = ["publicId", "draft", "draftRevision", "currentVersionId", "lastVersionNo", "status",
               "deadlineAt", "allowMultiple", "policyLocked", "settingsRevision", "submitSequence",
               "createdAt", "updatedAt"]
    result = {"id": survey["_id"], **{key: survey[key] for key in allowed}}
    if survey.get("draftQuestionSources"):
        result["draftQuestionSources"] = survey["draftQuestionSources"]
    return result


def version_summary_view(version):
    """History-list metadata; full immutable definitions stay on detail reads."""
    return {"id": version["_id"], **{key: version[key] for key in
            ("surveyId", "versionNo", "sourceDraftRevision", "publishedAt")},
            "title": version["definition"]["title"]}


def version_view(version):
    result = {"id": version["_id"], **{key: version[key] for key in
              ["surveyId", "versionNo", "sourceDraftRevision", "definition", "publishedAt"]}}
    if version.get("questionSources"):
        result["questionSources"] = version["questionSources"]
    return result


def receipt(document):
    return {"receiptId": document["_id"], "versionId": document["versionId"],
            "acceptedAt": document["acceptedAt"], "anonymous": document["anonymous"], "status": "accepted"}


def response_view(document):
    result = {**receipt(document), "answers": document["answers"], "path": document["path"],
              "acceptedSeq": document["acceptedSeq"]}
    if not document["anonymous"]:
        result["respondentName"] = document["identity"]["usernameAtSubmit"]
    return result


class Services:
    def __init__(self, db, settings, clock=utcnow, hooks=None):
        self.db, self.settings, self.clock, self.hooks = db, settings, clock, hooks or {}
        self.repo = Repository(db, settings.transaction_seconds)

    async def hook(self, name, **context):
        if self.settings.testing and name in self.hooks:
            await self.hooks[name](**context)

    async def owned(self, survey_id, user):
        result = await self.repo.owner_survey(survey_id, user["_id"])
        if not result:
            raise ServiceError("NOT_FOUND", "问卷不存在", 404)
        return result

    async def public(self, public_id):
        survey = await self.repo.public_survey(public_id)
        if not survey or not survey["currentVersionId"]:
            raise ServiceError("NOT_FOUND", "问卷尚未发布或不存在", 404)
        return survey

    def check_open(self, survey, now=None):
        if survey["status"] != "open":
            raise ServiceError("SURVEY_CLOSED", "问卷已停止收集")
        now = now or self.clock()
        if survey["deadlineAt"] is not None and now >= survey["deadlineAt"]:
            raise ServiceError("SURVEY_EXPIRED", "问卷已截止")

    async def register(self, username, password):
        try:
            display, key = normalize_username(username)
        except ValueError as exc:
            raise ServiceError("INVALID_USERNAME", str(exc), 422) from exc
        document = {"_id": str(uuid4()), "username": display, "usernameKey": key,
                    "passwordHash": await asyncio.to_thread(PASSWORDS.hash, password),
                    "createdAt": self.clock(), "schemaVersion": 1}
        try:
            await self.db.users.insert_one(document)
        except DuplicateKeyError as exc:
            raise ServiceError("USERNAME_EXISTS", "用户名已被使用") from exc
        return self.login_result(document)

    async def login(self, username, password):
        try:
            _, key = normalize_username(username)
        except ValueError:
            key = ""
        user = await self.db.users.find_one({"usernameKey": key})
        if not user or not await asyncio.to_thread(verify_password, user["passwordHash"], password):
            raise ServiceError("INVALID_CREDENTIALS", "用户名或密码不正确", 401)
        return self.login_result(user)

    def login_result(self, user):
        value, expires = token(self.settings.auth_secret, "access", user["_id"], self.settings.access_seconds)
        return {"accessToken": value, "expiresAt": expires, "tokenType": "bearer", "user": public_user(user)}

    async def create(self, user, title):
        now = self.clock()
        document = {"_id": str(uuid4()), "ownerId": user["_id"], "publicId": str(uuid4()).replace("-", ""),
                    "draft": {"title": title, "description": "", "allowAnonymous": True,
                              "entryQuestionId": None, "questions": []},
                    "draftRevision": 0, "currentVersionId": None, "lastVersionNo": 0,
                    "status": "draft", "deadlineAt": None, "allowMultiple": False, "policyLocked": False,
                    "settingsRevision": 0, "submitSequence": 0, "createdAt": now, "updatedAt": now,
                    "schemaVersion": 1}
        await self.db.surveys.insert_one(document)
        return survey_view(document)

    async def checked_question_sources(self, user, definition, sources, session=None, retained_sources=()):
        """Resolve explicit provenance, never infer identity from equal wording.

        A source version supplies content only. Local IDs, requiredness and
        routing remain in the executable questionnaire definition.
        """
        def invalid(message, field, instance_id=None):
            raise ServiceError("INVALID_QUESTION_SOURCE", message, 422,
                               [{"code": "invalid_question_source", "message": message,
                                 "questionId": instance_id, "ruleId": None, "field": field}])

        if type(sources) is not list or len(sources) > 100:
            invalid("题目来源必须是最多100项的数组", "questionSources")
        if not sources:
            return []
        instances = {}
        for index, question in enumerate(definition.get("questions", [])):
            if type(question) is dict and type(question.get("id")) is str:
                instances.setdefault(question["id"], []).append((index, question))
        seen = set()
        for index, source in enumerate(sources):
            field = f"questionSources[{index}]"
            if (type(source) is not dict or set(source) != {"instanceId", "questionId", "questionVersionId"}
                    or any(type(value) is not str or not 1 <= len(value) <= 160 for value in source.values())):
                invalid("请提供完整且合法的题目来源标识", field)
            instance_id = source["instanceId"]
            if not _valid_id(instance_id) or instance_id in seen or len(instances.get(instance_id, [])) != 1:
                invalid("来源必须指向唯一且存在的卷内题目实例", field + ".instanceId", instance_id)
            seen.add(instance_id)

        question_ids = list({source["questionId"] for source in sources})
        questions = await self.db.questions.find(
            {"_id": {"$in": question_ids}}, session=session).to_list(length=100)
        if len(questions) != len(question_ids):
            raise ServiceError("NOT_FOUND", "引用的题目不存在或当前不可使用", 404)
        # Only the server's currently persisted binding carries prior-use rights.
        # Copying wording, a published snapshot or a client claim grants nothing.
        retained = {(item["instanceId"], item["questionId"], item["questionVersionId"])
                    for item in retained_sources}
        questions = {item["_id"]: item for item in questions}
        needs_grant = {source["questionId"] for source in sources
                       if questions[source["questionId"]]["ownerId"] != user["_id"]
                       and (source["instanceId"], source["questionId"], source["questionVersionId"]) not in retained}
        for question_id in sorted(needs_grant):
            if session is None:
                raise ServiceError("DATA_INVARIANT", "新增共享引用必须在保存事务内核对权限", 500)
            await self.hook("before_question_share_gate", question_id=question_id)
            grant = await self.db.question_shares.find_one_and_update(
                grant_filter(questions[question_id], user["_id"]), {"$inc": {"useSequence": 1}},
                return_document=ReturnDocument.AFTER, session=session)
            if not grant:
                raise ServiceError("NOT_FOUND", "引用的题目不存在或当前不可使用", 404)
            await self.hook("after_question_share_gate", question_id=question_id)
        versions = await self.db.question_versions.find(
            {"_id": {"$in": list({source["questionVersionId"] for source in sources})},
             "questionId": {"$in": question_ids}}, session=session).to_list(length=100)
        versions = {version["_id"]: version for version in versions}
        for index, source in enumerate(sources):
            version = versions.get(source["questionVersionId"])
            if not version or version["questionId"] != source["questionId"]:
                raise ServiceError("QUESTION_VERSION_NOT_FOUND", "引用的版本不属于指定题目或不存在", 404)
            position, question = instances[source["instanceId"]][0]
            content = {key: question.get(key) for key in ("kind", "title", "config")}
            errors = validate_question_content(content)
            if errors:
                errors = [{**error, "questionId": source["instanceId"],
                           "field": error["field"].replace("content", f"questions[{position}]", 1)}
                          for error in errors]
                raise ServiceError("INVALID_QUESTION_SOURCE", "引用题目的内容不合法，请检查题型和约束", 422, errors)
            canonical = canonical_question_content(content)
            if canonical != version["content"]:
                changed = next(key for key in ("kind", "title", "config") if canonical[key] != version["content"][key])
                invalid("题目内容与引用版本不一致，请选择对应版本或明确解除引用后修改",
                        f"questions[{position}].{changed}", source["instanceId"])
        return deepcopy(sorted(sources, key=lambda source: instances[source["instanceId"]][0][0]))

    async def save_draft(self, user, survey_id, revision, definition, question_sources=None):
        survey = await self.owned(survey_id, user)
        if survey["draftRevision"] != revision:
            raise ServiceError("DRAFT_CONFLICT", "草稿已在其他页面修改，请刷新后比较")
        if question_sources is None and survey.get("draftQuestionSources"):
            raise ServiceError("QUESTION_SOURCES_REQUIRED", "当前草稿含题目引用，请明确提交来源列表；解除全部引用请传空数组")
        # Incomplete drafts are allowed, but bounded and with a known envelope.
        if set(definition) - {"title", "description", "allowAnonymous", "entryQuestionId", "questions"}:
            raise ServiceError("INVALID_DEFINITION", "草稿包含未知字段", 422)
        if (not isinstance(definition.get("title"), str) or not definition["title"].strip()
                or len(definition["title"]) > 200 or not isinstance(definition.get("questions"), list)
                or len(definition["questions"]) > 100):
            raise ServiceError("INVALID_DEFINITION", "请检查标题与题目数量", 422)
        if question_sources is None:
            result = await self.repo.save_draft(survey_id, user["_id"], revision, definition, self.clock())
        else:
            async def operation(session):
                current = await self.repo.owner_survey(survey_id, user["_id"], session)
                if not current:
                    raise ServiceError("NOT_FOUND", "问卷不存在", 404)
                if current["draftRevision"] != revision:
                    raise ServiceError("DRAFT_CONFLICT", "草稿已在其他页面修改，请刷新后比较")
                sources = await self.checked_question_sources(
                    user, definition, question_sources, session,
                    retained_sources=current.get("draftQuestionSources", []))
                saved = await self.repo.save_draft(survey_id, user["_id"], revision, definition, self.clock(),
                                                   question_sources=sources, session=session)
                if not saved:
                    raise ServiceError("DRAFT_CONFLICT", "草稿已在其他页面修改，请刷新后比较")
                return saved
            result = await self.repo.transaction(operation)
        if not result:
            raise ServiceError("DRAFT_CONFLICT", "草稿已在其他页面修改，请刷新后比较")
        return survey_view(result)

    async def preview(self, user, survey_id, definition, answers, mode):
        await self.owned(survey_id, user)
        errors = validate_definition(definition)
        if errors:
            raise ServiceError("INVALID_DEFINITION", "问卷定义尚不能执行，请修正配置", 422, errors)
        result = evaluate(definition, answers, mode)
        result["warnings"] = analyze_definition(definition)
        return result

    async def publish(self, user, survey_id, revision, expected_current, key):
        await self.owned(survey_id, user)
        request_digest = digest({"expectedDraftRevision": revision, "expectedCurrentVersionId": expected_current})

        async def recover():
            old = await self.repo.prior_publish(survey_id, key)
            if old:
                if old["publishRequestDigest"] != request_digest:
                    raise ServiceError("IDEMPOTENCY_CONFLICT", "发布标识已用于不同内容")
                return version_view(old)

        old = await recover()
        if old:
            return old, True

        async def operation(session):
            survey = await self.repo.owner_survey(survey_id, user["_id"], session)
            if not survey:
                raise ServiceError("NOT_FOUND", "问卷不存在", 404)
            if survey["draftRevision"] != revision or survey["currentVersionId"] != expected_current:
                raise ServiceError("PUBLISH_CONFLICT", "草稿或当前版本已变化，请重新确认")
            errors = validate_definition(survey["draft"])
            if errors:
                raise ServiceError("INVALID_DEFINITION", "发布检查未通过", 422, errors)
            sources = await self.checked_question_sources(
                user, survey["draft"], survey.get("draftQuestionSources", []), session,
                retained_sources=survey.get("draftQuestionSources", []))
            now = self.clock()
            if survey["status"] == "draft" and survey["deadlineAt"] and now >= survey["deadlineAt"]:
                raise ServiceError("SURVEY_EXPIRED", "首次发布的截止时间必须在未来")
            document = {"_id": str(uuid4()), "surveyId": survey_id, "versionNo": survey["lastVersionNo"] + 1,
                        "sourceDraftRevision": revision, "definition": deepcopy(survey["draft"]),
                        "publishKey": key, "publishRequestDigest": request_digest,
                        "publishedAt": now, "schemaVersion": 1}
            if sources:
                document["questionSources"] = deepcopy(sources)
            await self.repo.insert_version(document, session)
            await self.hook("after_version_insert", document=document)
            result = await self.repo.switch_version(survey, user["_id"], revision, expected_current,
                                                   document["_id"], document["versionNo"], now, session)
            if not result.matched_count:
                raise ServiceError("PUBLISH_CONFLICT", "发布时草稿或当前版本发生变化")
            return version_view(document)

        try:
            return await self.repo.transaction(operation), False
        except (ServiceError, PyMongoError):
            recovered = await recover()
            if recovered:
                return recovered, True
            raise

    async def settings_update(self, user, survey_id, revision, changes):
        survey = await self.owned(survey_id, user)
        if survey["status"] == "closed":
            raise ServiceError("SURVEY_CLOSED", "已关闭问卷的收集设置不能修改")
        query = {"_id": survey_id, "ownerId": user["_id"], "settingsRevision": revision,
                 "status": {"$ne": "closed"}}
        if "allowMultiple" in changes:
            query["policyLocked"] = False
        result = await self.db.surveys.find_one_and_update(
            query, {"$set": {**changes, "updatedAt": self.clock()}, "$inc": {"settingsRevision": 1}},
            return_document=ReturnDocument.AFTER)
        if not result:
            latest = await self.owned(survey_id, user)
            if "allowMultiple" in changes and latest["policyLocked"]:
                raise ServiceError("POLICY_LOCKED", "已有正式答卷，多填政策已锁定")
            raise ServiceError("SETTINGS_CONFLICT", "收集设置已变化，请刷新后重试")
        return survey_view(result)

    async def close(self, user, survey_id, revision):
        survey = await self.owned(survey_id, user)
        if survey["status"] == "closed":
            return survey_view(survey)
        if survey["status"] != "open":
            raise ServiceError("NOT_PUBLISHED", "请先发布问卷")
        result = await self.db.surveys.find_one_and_update(
            {"_id": survey_id, "ownerId": user["_id"], "status": "open", "settingsRevision": revision},
            {"$set": {"status": "closed", "updatedAt": self.clock()}, "$inc": {"settingsRevision": 1}},
            return_document=ReturnDocument.AFTER)
        if not result:
            raise ServiceError("SETTINGS_CONFLICT", "收集设置已变化，请刷新")
        return survey_view(result)

    async def start(self, user, public_id):
        survey = await self.public(public_id)
        self.check_open(survey)
        version = await self.repo.version(survey["_id"], survey["currentVersionId"])
        if not version:
            raise ServiceError("VERSION_NOT_FOUND", "发布版本不存在", 404)
        value, expires = token(self.settings.fill_secret, "fill", user["_id"], self.settings.fill_seconds,
                               surveyId=survey["_id"], versionId=version["_id"])
        return {"fillToken": value, "versionId": version["_id"], "expiresAt": expires,
                "definition": version["definition"], "allowMultiple": survey["allowMultiple"],
                "deadlineAt": survey["deadlineAt"], "publicId": public_id, "versionNo": version["versionNo"]}

    def fill_claims(self, value, user, survey, version_id):
        try:
            claims = decode_token(value or "", self.settings.fill_secret, "fill")
        except jwt.ExpiredSignatureError as exc:
            raise ServiceError("FILL_CREDENTIAL_EXPIRED", "填写凭据已过期，请重新开始") from exc
        except jwt.PyJWTError as exc:
            raise ServiceError("INVALID_FILL_CREDENTIAL", "请通过问卷入口重新开始填写", 403) from exc
        if (claims["sub"] != user["_id"] or claims.get("surveyId") != survey["_id"]
                or claims.get("versionId") != version_id):
            raise ServiceError("INVALID_FILL_CREDENTIAL", "填写凭据不属于当前用户或版本", 403)

    async def evaluate_fill(self, user, public_id, version_id, answers, fill_token):
        survey = await self.public(public_id)
        self.fill_claims(fill_token, user, survey, version_id)
        self.check_open(survey)
        version = await self.repo.version(survey["_id"], version_id)
        if not version:
            raise ServiceError("VERSION_NOT_FOUND", "版本不存在", 404)
        return evaluate(version["definition"], answers, "interactive")

    async def submit(self, user, public_id, version_id, anonymous, answers, key, fill_token):
        survey = await self.public(public_id)
        participant = participant_key(self.settings.participant_secret, survey["_id"], user["_id"])

        async def recover():
            old = await self.repo.prior_submission(survey["_id"], participant, key)
            if not old:
                return None
            if old["versionId"] != version_id or old["anonymous"] != anonymous:
                raise ServiceError("IDEMPOTENCY_CONFLICT", "该提交标识已用于不同版本或匿名选择")
            prior_version = await self.repo.version(survey["_id"], old["versionId"])
            result = evaluate(prior_version["definition"], answers, "final")
            normalized = canonical_answers(prior_version["definition"], result["activeAnswers"])
            if result["errors"] or old["payloadDigest"] != submission_digest(version_id, anonymous, normalized):
                raise ServiceError("IDEMPOTENCY_CONFLICT", "该提交标识已用于不同答案")
            return receipt(old)

        previous = await recover()
        if previous:
            return previous, True
        self.fill_claims(fill_token, user, survey, version_id)
        version = await self.repo.version(survey["_id"], version_id)
        if not version:
            raise ServiceError("VERSION_NOT_FOUND", "版本不存在", 404)
        definition = version["definition"]
        if anonymous and not definition["allowAnonymous"]:
            raise ServiceError("ANONYMOUS_DISABLED", "此发布版本不允许匿名填写", 422)
        evaluated = evaluate(definition, answers, "final")
        if evaluated["errors"] or not evaluated["complete"] or evaluated["provisional"]:
            raise ServiceError("ANSWER_VALIDATION_FAILED", "请修正填写内容", 422, evaluated["errors"])
        normalized = canonical_answers(definition, evaluated["activeAnswers"])
        request_digest = submission_digest(version_id, anonymous, normalized)
        kinds = {q["id"]: q["kind"] for q in definition["questions"]}
        saved_answers = []
        for qid in evaluated["path"]:
            if qid not in normalized:
                continue
            value = normalized[qid]
            if kinds[qid] == "number" and isinstance(value, int):
                value = Int64(value)
            saved_answers.append({"questionId": qid, "kind": kinds[qid], "value": value})
        await self.hook("before_submission_transaction", survey=survey)

        async def operation(session):
            gate = await self.repo.submission_gate(survey["_id"], self.clock(), session)
            if not gate:
                raise ServiceError("SURVEY_CLOSED", "问卷已停止收集")
            await self.hook("after_submission_gate", survey=gate)
            accepted = self.clock()
            self.check_open(gate, accepted)
            document = {"_id": str(uuid4()), "surveyId": survey["_id"], "versionId": version_id,
                        "participantKey": participant, "submissionKey": key, "payloadDigest": request_digest,
                        "canonicalizationVersion": 1, "anonymous": anonymous, "answers": saved_answers,
                        "path": evaluated["path"], "acceptedSeq": gate["submitSequence"], "acceptedAt": accepted,
                        "schemaVersion": 1}
            if not gate["allowMultiple"]:
                document["oneTimeParticipantKey"] = participant
            if not anonymous:
                document["identity"] = {"userId": user["_id"], "usernameAtSubmit": user["username"]}
            await self.repo.insert_response(document, session)
            await self.hook("after_response_insert", document=document)
            return receipt(document)

        try:
            return await self.repo.transaction(operation), False
        except (ServiceError, PyMongoError) as exc:
            recovered = await recover()
            if recovered:
                return recovered, True
            if isinstance(exc, DuplicateKeyError):
                raise ServiceError("ALREADY_SUBMITTED", "此问卷不允许同一账号重复提交") from exc
            if isinstance(exc, PyMongoError):
                raise ServiceError("SUBMISSION_OUTCOME_UNKNOWN", "暂时无法确认结果，请保持当前提交标识重试", 503) from exc
            raise
