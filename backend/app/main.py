from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4
import asyncio
import csv
import io
import logging

from fastapi import FastAPI, APIRouter, Depends, Request, Header, Query
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
import jwt
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.json_schema import SkipJsonSchema
from pymongo import AsyncMongoClient, ReadPreference
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern
from pymongo.errors import PyMongoError, WriteError

from .config import Settings, ROOT
from .database import init_database
from .security import decode_token, utcnow
from .services import Services, ServiceError, public_user, survey_view, version_view, response_view
from .statistics import get_stats, StatsError, RespondentType, build_response_scope
from .questions import QuestionServices, question_view, question_version_view

log = logging.getLogger("survey")
bearer = HTTPBearer(auto_error=False)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Credentials(StrictModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=128)


class NewSurvey(StrictModel):
    title: str = Field(default="未命名问卷", min_length=1, max_length=200)


class QuestionSource(StrictModel):
    instanceId: str = Field(min_length=1, max_length=64)
    questionId: str = Field(min_length=1, max_length=160)
    questionVersionId: str = Field(min_length=1, max_length=160)


class Draft(StrictModel):
    definition: dict[str, Any]
    expectedDraftRevision: int = Field(ge=0)
    questionSources: Annotated[list[QuestionSource], Field(max_length=100)] | SkipJsonSchema[None] = None

    @field_validator("questionSources")
    @classmethod
    def explicit_sources_are_not_null(cls, value):
        if value is None:
            raise ValueError("questionSources不能为null，解除全部引用请传空数组")
        return value


class Preview(StrictModel):
    definition: dict[str, Any]
    answers: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["interactive", "final"] = "interactive"


class Publish(StrictModel):
    expectedDraftRevision: int = Field(ge=0)
    expectedCurrentVersionId: str | None
    publishKey: str = Field(min_length=1, max_length=128)


class CollectionSettings(StrictModel):
    expectedSettingsRevision: int = Field(ge=0)
    deadlineAt: datetime | None = None
    allowMultiple: bool | None = None

    @field_validator("deadlineAt", mode="before")
    @classmethod
    def parse_deadline(cls, value):
        # Strict model still accepts an explicit ISO 8601 string for a JSON date.
        if value is None or isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("截止时间须为带时区的ISO 8601格式") from exc
        else:
            raise ValueError("截止时间格式不正确")
        if parsed is not None:
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("截止时间必须带时区")
            parsed = parsed.astimezone(timezone.utc)
        return parsed


class Close(StrictModel):
    expectedSettingsRevision: int = Field(ge=0)


class Fill(StrictModel):
    versionId: str = Field(min_length=1, max_length=160)
    answers: dict[str, Any]


class Submit(Fill):
    anonymous: bool


class NewQuestion(StrictModel):
    content: dict[str, Any]


class NewQuestionVersion(NewQuestion):
    parentVersionId: str = Field(min_length=1, max_length=160)
    expectedCurrentVersionId: str = Field(min_length=1, max_length=160)


async def current_user(request: Request, authorization: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if not authorization or authorization.scheme.lower() != "bearer":
        raise ServiceError("UNAUTHENTICATED", "请先登录", 401)
    try:
        claims = decode_token(authorization.credentials, request.app.state.settings.auth_secret, "access")
    except jwt.PyJWTError as exc:
        raise ServiceError("UNAUTHENTICATED", "登录已过期，请重新登录", 401) from exc
    user = await request.app.state.db.users.find_one({"_id": claims["sub"]})
    if not user:
        raise ServiceError("UNAUTHENTICATED", "账号不存在，请重新登录", 401)
    return user


def service(request: Request):
    return Services(request.app.state.db, request.app.state.settings,
                    request.app.state.clock, request.app.state.hooks)


def question_service(request: Request):
    return QuestionServices(request.app.state.db, request.app.state.settings,
                            request.app.state.clock, request.app.state.hooks)


def create_app(settings=None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        client = AsyncMongoClient(settings.mongo_uri, tz_aware=True,
                                  serverSelectionTimeoutMS=5000, connectTimeoutMS=5000,
                                  maxPoolSize=100, appname="DBLabQuestionnaire")
        try:
            hello = await client.admin.command("hello")
            if not hello.get("setName"):
                raise RuntimeError("本项目需要MongoDB副本集，请运行 scripts/mongo.ps1。")
            app.state.db = client.get_database(settings.database, read_concern=ReadConcern("majority"),
                                               write_concern=WriteConcern("majority"),
                                               read_preference=ReadPreference.PRIMARY)
            await init_database(app.state.db)
            app.state.mongo_version = (await client.server_info())["version"]
            yield
        finally:
            await client.close()

    app = FastAPI(title="DB Lab 问卷系统", version="1.0.0", lifespan=lifespan,
                  description="不可变发布版本、统一规则引擎、MongoDB事务提交与可核算统计")
    app.state.settings = settings
    app.state.clock = utcnow
    app.state.hooks = {}

    def error_response(request, code, message, status, details=None):
        return JSONResponse(status_code=status, content=jsonable_encoder({
            "code": code, "message": message, "fieldErrors": details or [],
            "requestId": getattr(request.state, "request_id", None),
        }))

    @app.middleware("http")
    async def request_boundary(request, call_next):
        request.state.request_id = str(uuid4())
        if request.url.path.startswith("/api") and request.method in {"POST", "PUT", "PATCH"}:
            limit = 512 * 1024 if request.url.path.endswith(("/draft", "/preview")) else 1024 * 1024
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > limit:
                    return error_response(request, "PAYLOAD_TOO_LARGE", "请求内容超过允许大小", 413)
            request._body = bytes(content)
        result = await call_next(request)
        result.headers["X-Request-ID"] = request.state.request_id
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api"):
            result.headers["Cache-Control"] = "no-store"
        return result

    @app.exception_handler(ServiceError)
    async def business_error(request, exc):
        return error_response(request, exc.code, exc.message, exc.status, exc.errors)

    @app.exception_handler(StatsError)
    async def stats_error(request, exc):
        return error_response(request, exc.code, exc.message, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        details = [{"field": ".".join(map(str, e["loc"])), "message": e["msg"], "code": e["type"]}
                   for e in exc.errors()]
        return error_response(request, "INVALID_REQUEST", "请求字段不符合接口要求", 422, details)

    @app.exception_handler(WriteError)
    async def database_validation_error(request, exc):
        if exc.code == 121:
            return error_response(request, "INVALID_DOCUMENT", "数据结构不符合存储契约，请检查配置", 422)
        return error_response(request, "DATABASE_UNAVAILABLE", "数据库暂时不可用，请稍后重试", 503)

    @app.exception_handler(PyMongoError)
    async def database_error(request, exc):
        log.error("database operation failed: %s request=%s", type(exc).__name__, request.state.request_id)
        return error_response(request, "DATABASE_UNAVAILABLE", "数据库暂时不可用，请稍后重试", 503)

    api = APIRouter(prefix="/api")

    @api.get("/health")
    async def health(request: Request):
        await request.app.state.db.command("ping")
        return {"status": "ok", "database": "MongoDB", "mongoVersion": request.app.state.mongo_version,
                "replicaSetRequired": True}

    @api.post("/auth/register", status_code=201)
    async def register(body: Credentials, svc=Depends(service)):
        return await svc.register(body.username, body.password)

    @api.post("/auth/login")
    async def login(body: Credentials, svc=Depends(service)):
        return await svc.login(body.username, body.password)

    @api.get("/auth/me")
    async def me(user=Depends(current_user)):
        return public_user(user)

    @api.post("/questions")
    async def create_question(body: NewQuestion, user=Depends(current_user), svc=Depends(question_service),
                              idempotency_key: str = Header(min_length=1, max_length=128)):
        result, replayed = await svc.create(user, body.content, idempotency_key)
        return JSONResponse(status_code=200 if replayed else 201, content=jsonable_encoder(result),
                            headers={"Idempotent-Replay": str(replayed).lower()})

    @api.get("/questions")
    async def list_questions(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
                             user=Depends(current_user), svc=Depends(question_service)):
        items = await svc.db.questions.find({"ownerId": user["_id"]}).sort(
            [("createdAt", -1), ("_id", -1)]).skip(offset).limit(limit).to_list(length=limit)
        return {"items": [question_view(item) for item in items], "offset": offset, "limit": limit}

    @api.get("/questions/{question_id}")
    async def question_detail(question_id: str, user=Depends(current_user), svc=Depends(question_service)):
        return question_view(await svc.owned(question_id, user))

    @api.get("/questions/{question_id}/usages")
    async def question_usages(question_id: str, scope: Literal["draft", "published"] = "draft",
                              questionVersionId: str | None = Query(None, min_length=1, max_length=160),
                              limit: int = Query(30, ge=1, le=100),
                              afterId: str | None = Query(None, min_length=1, max_length=160),
                              user=Depends(current_user), svc=Depends(question_service)):
        return await svc.usages(user, question_id, scope, questionVersionId, limit, afterId)

    @api.post("/questions/{question_id}/versions")
    async def create_question_version(question_id: str, body: NewQuestionVersion, user=Depends(current_user),
                                      svc=Depends(question_service),
                                      idempotency_key: str = Header(min_length=1, max_length=128)):
        result, replayed = await svc.derive(user, question_id, body.parentVersionId,
                                          body.expectedCurrentVersionId, body.content, idempotency_key)
        return JSONResponse(status_code=200 if replayed else 201, content=jsonable_encoder(result),
                            headers={"Idempotent-Replay": str(replayed).lower()})

    @api.get("/questions/{question_id}/versions")
    async def question_versions(question_id: str, limit: int = Query(30, ge=1, le=100),
                                offset: int = Query(0, ge=0, le=100000),
                                user=Depends(current_user), svc=Depends(question_service)):
        await svc.owned(question_id, user)
        items = await svc.db.question_versions.find({"questionId": question_id}).sort(
            "versionNo", -1).skip(offset).limit(limit).to_list(length=limit)
        return {"items": [question_version_view(item) for item in items], "offset": offset, "limit": limit}

    @api.get("/questions/{question_id}/versions/{version_id}")
    async def question_version(question_id: str, version_id: str, user=Depends(current_user),
                               svc=Depends(question_service)):
        await svc.owned(question_id, user)
        return question_version_view(await svc.version(question_id, version_id))

    @api.post("/surveys", status_code=201)
    async def create_survey(body: NewSurvey, user=Depends(current_user), svc=Depends(service)):
        return await svc.create(user, body.title)

    @api.get("/surveys")
    async def list_surveys(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
                           user=Depends(current_user), svc=Depends(service)):
        cursor = svc.db.surveys.find({"ownerId": user["_id"]}).sort([("createdAt", -1), ("_id", -1)])
        items = await cursor.skip(offset).limit(limit).to_list(length=limit)
        return {"items": [survey_view(item) for item in items], "offset": offset, "limit": limit}

    @api.get("/surveys/{survey_id}")
    async def survey_detail(survey_id: str, user=Depends(current_user), svc=Depends(service)):
        return survey_view(await svc.owned(survey_id, user))

    @api.put("/surveys/{survey_id}/draft")
    async def save_draft(survey_id: str, body: Draft, user=Depends(current_user), svc=Depends(service)):
        sources = None if body.questionSources is None else [item.model_dump() for item in body.questionSources]
        return await svc.save_draft(user, survey_id, body.expectedDraftRevision, body.definition, sources)

    @api.post("/surveys/{survey_id}/preview")
    async def preview(survey_id: str, body: Preview, user=Depends(current_user), svc=Depends(service)):
        return await svc.preview(user, survey_id, body.definition, body.answers, body.mode)

    @api.post("/surveys/{survey_id}/publish")
    async def publish(survey_id: str, body: Publish, user=Depends(current_user), svc=Depends(service)):
        result, replayed = await svc.publish(user, survey_id, body.expectedDraftRevision,
                                             body.expectedCurrentVersionId, body.publishKey)
        return JSONResponse(status_code=200 if replayed else 201, content=jsonable_encoder(result),
                            headers={"Idempotent-Replay": str(replayed).lower()})

    @api.patch("/surveys/{survey_id}/settings")
    async def update_settings(survey_id: str, body: CollectionSettings, user=Depends(current_user), svc=Depends(service)):
        changes = body.model_dump(exclude_unset=True)
        changes.pop("expectedSettingsRevision")
        if not changes or ("allowMultiple" in changes and changes["allowMultiple"] is None):
            raise ServiceError("INVALID_REQUEST", "请提供有效的收集设置", 422)
        return await svc.settings_update(user, survey_id, body.expectedSettingsRevision, changes)

    @api.post("/surveys/{survey_id}/close")
    async def close(survey_id: str, body: Close, user=Depends(current_user), svc=Depends(service)):
        return await svc.close(user, survey_id, body.expectedSettingsRevision)

    @api.get("/surveys/{survey_id}/versions")
    async def versions(survey_id: str, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0),
                       user=Depends(current_user), svc=Depends(service)):
        await svc.owned(survey_id, user)
        items = await svc.db.survey_versions.find({"surveyId": survey_id}).sort("versionNo", -1).skip(offset).limit(limit).to_list(length=limit)
        return {"items": [version_view(item) for item in items]}

    @api.get("/surveys/{survey_id}/versions/{version_id}")
    async def version(survey_id: str, version_id: str, user=Depends(current_user), svc=Depends(service)):
        await svc.owned(survey_id, user)
        result = await svc.repo.version(survey_id, version_id)
        if not result:
            raise ServiceError("NOT_FOUND", "版本不存在", 404)
        return version_view(result)

    @api.get("/survey/{public_id}")
    async def public_survey(public_id: str, user=Depends(current_user), svc=Depends(service)):
        survey = await svc.public(public_id)
        version = await svc.repo.version(survey["_id"], survey["currentVersionId"])
        now = svc.clock()
        expired = bool(survey["deadlineAt"] and now >= survey["deadlineAt"])
        return {"publicId": public_id, "status": "expired" if survey["status"] == "open" and expired else survey["status"],
                "deadlineAt": survey["deadlineAt"], "allowMultiple": survey["allowMultiple"],
                "versionId": version["_id"], "versionNo": version["versionNo"], "definition": version["definition"]}

    @api.post("/survey/{public_id}/start")
    async def start(public_id: str, user=Depends(current_user), svc=Depends(service)):
        return await svc.start(user, public_id)

    @api.post("/survey/{public_id}/evaluate")
    async def evaluate_answers(public_id: str, body: Fill, x_fill_token: str | None = Header(None),
                               user=Depends(current_user), svc=Depends(service)):
        return await svc.evaluate_fill(user, public_id, body.versionId, body.answers, x_fill_token)

    @api.post("/survey/{public_id}/submit")
    async def submit(public_id: str, body: Submit, idempotency_key: str | None = Header(None),
                     x_fill_token: str | None = Header(None), user=Depends(current_user), svc=Depends(service)):
        if not idempotency_key or not 1 <= len(idempotency_key) <= 128:
            raise ServiceError("INVALID_REQUEST", "必须提供1—128字符的Idempotency-Key", 422)
        result, replayed = await svc.submit(user, public_id, body.versionId, body.anonymous,
                                            body.answers, idempotency_key, x_fill_token)
        return JSONResponse(status_code=200 if replayed else 201, content=jsonable_encoder(result),
                            headers={"Idempotent-Replay": str(replayed).lower()})

    async def owned_version(svc, user, survey_id, version_id):
        survey = await svc.owned(survey_id, user)
        version = await svc.repo.version(survey_id, version_id)
        if not version:
            raise ServiceError("NOT_FOUND", "版本不存在", 404)
        return survey, version

    @api.get("/surveys/{survey_id}/stats")
    async def stats(survey_id: str, versionId: str, asOfSeq: int | None = Query(None, ge=0),
                    questionId: str | None = None, respondentType: RespondentType = Query("all"),
                    user=Depends(current_user), svc=Depends(service)):
        survey, version = await owned_version(svc, user, survey_id, versionId)
        return await get_stats(svc.db, survey, version, asOfSeq, questionId, respondentType)

    async def response_page(svc, user, survey_id, version_id, as_of, cursor, limit, question_id=None,
                            respondent_type: RespondentType = "all"):
        survey, version = await owned_version(svc, user, survey_id, version_id)
        bound = survey["submitSequence"] if as_of is None else as_of
        if bound > survey["submitSequence"]:
            raise ServiceError("INVALID_RANGE", "统计范围超过已提交序号", 422)
        query = build_response_scope(survey_id, version_id, bound, respondent_type)
        if cursor is not None:
            query["acceptedSeq"]["$lt"] = cursor
        if question_id:
            if question_id not in {q["id"] for q in version["definition"]["questions"]}:
                raise ServiceError("NOT_FOUND", "题目不存在", 404)
            query["answers.questionId"] = question_id
        found = await svc.db.responses.find(query).sort("acceptedSeq", -1).limit(limit + 1).to_list(length=limit + 1)
        items = found[:limit]
        return {"items": [response_view(item) for item in items], "asOfSeq": bound,
                "respondentType": respondent_type,
                "nextCursor": items[-1]["acceptedSeq"] if len(found) > limit else None}

    @api.get("/surveys/{survey_id}/responses")
    async def responses(survey_id: str, versionId: str, asOfSeq: int | None = Query(None, ge=0),
                        cursor: int | None = Query(None, ge=1), limit: int = Query(30, ge=1, le=100),
                        questionId: str | None = None, respondentType: RespondentType = Query("all"),
                        user=Depends(current_user), svc=Depends(service)):
        return await response_page(svc, user, survey_id, versionId, asOfSeq, cursor, limit,
                                   questionId, respondentType)

    @api.get("/surveys/{survey_id}/export")
    async def export(survey_id: str, versionId: str, asOfSeq: int | None = Query(None, ge=0),
                     respondentType: RespondentType = Query("all"),
                     user=Depends(current_user), svc=Depends(service)):
        survey, version = await owned_version(svc, user, survey_id, versionId)
        bound = survey["submitSequence"] if asOfSeq is None else asOfSeq
        if bound > survey["submitSequence"]:
            raise ServiceError("INVALID_RANGE", "统计范围超过已提交序号", 422)
        query = build_response_scope(survey_id, versionId, bound, respondentType)
        count = await svc.db.responses.count_documents(query)
        if count > 20000:
            raise ServiceError("EXPORT_TOO_LARGE", "单次导出最多20000份，请缩小范围", 422)
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        questions = version["definition"]["questions"]
        def cell(value):
            text = str(value)
            # Check the first non-whitespace character without altering database content.
            return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
        writer.writerow(["回执", "版本", "提交时间UTC", "匿名", "填写者"] + [cell(q["title"]) for q in questions])
        async for document in svc.db.responses.find(query).sort("acceptedSeq", 1):
            values = {a["questionId"]: a["value"] for a in document["answers"]}
            row = [document["_id"], version["versionNo"], document["acceptedAt"].isoformat(),
                   "是" if document["anonymous"] else "否",
                   "匿名" if document["anonymous"] else cell(document["identity"]["usernameAtSubmit"])]
            for q in questions:
                value = values.get(q["id"], "")
                if isinstance(value, list):
                    labels = {o["id"]: o["label"] for o in q["config"].get("options", [])}
                    value = "；".join(labels.get(v, v) for v in value)
                elif q["kind"] == "single":
                    labels = {o["id"]: o["label"] for o in q["config"]["options"]}
                    value = labels.get(value, value)
                row.append(cell(value))
            writer.writerow(row)
        suffix = "" if respondentType == "all" else f"-{respondentType}"
        return Response(content="\ufeff" + buffer.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="survey-v{version["versionNo"]}-seq{bound}{suffix}.csv"'})

    app.include_router(api)
    dist = ROOT / "frontend" / "dist"
    if dist.is_dir():
        if (dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
        @app.get("/{path:path}", include_in_schema=False)
        async def frontend(path: str):
            if path == "api" or path.startswith("api/"):
                raise ServiceError("NOT_FOUND", "接口不存在", 404)
            return FileResponse(dist / "index.html")
    return app


app = create_app()
