"""Serve the real built UI/API in an owned disposable database for browser QA.

This runner performs no browser clicks and seeds no accounts, questions or
surveys. Business records come from the operator's real browser actions. It
records only safe post-write summaries, never authentication bodies or headers.
Create stop.request in the new output directory to stop and clean up normally.
Ownership metadata stays in the repository's private .runtime directory so an
interrupted process can be identified without retaining connection settings.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
from uuid import uuid4

import dotenv
from pymongo import AsyncMongoClient
from pymongo.read_concern import ReadConcern
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]
PREFIX = "survey_ui_"
MARKER = "browser_runner_ownership"
WRITE_PATHS = (
    ("POST", re.compile(r"^/api/question-banks$")),
    ("PUT", re.compile(r"^/api/question-banks/[^/]+(?:/questions/[^/]+)?$")),
    ("DELETE", re.compile(r"^/api/question-banks/[^/]+/questions/[^/]+$")),
    ("POST", re.compile(r"^/api/questions(?:/[^/]+/(?:versions|restore))?$")),
    ("PUT", re.compile(r"^/api/surveys/[^/]+/draft$")),
    ("PUT", re.compile(r"^/api/questions/[^/]+/shares$")),
    ("POST", re.compile(r"^/api/questions/[^/]+/statistics/scopes$")),
    ("PUT", re.compile(r"^/api/surveys/[^/]+/question-statistics-grants/[^/]+$")),
    ("POST", re.compile(r"^/api/surveys/[^/]+/publish$")),
    ("POST", re.compile(r"^/api/survey/[^/]+/submit$")),
)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def question_content(content):
    """Whitelist inspectable content; no answer text or personal identity."""
    config = content.get("config", {})
    return {"kind": content.get("kind"), "title": content.get("title"),
            "content": {key: config[key] for key in
                        ("min", "max", "integerOnly", "minLength", "maxLength", "minSelected", "maxSelected")
                        if key in config}}


def survey_definition(definition):
    return [{"instanceId": item["id"], **question_content(item)}
            for item in definition.get("questions", [])]


def numeric_answers(responses):
    """Keep numeric-question values, including BSON Int64, but never booleans."""
    return [{"instanceId": answer["questionId"], "value": answer["value"]}
            for response in responses for answer in response.get("answers", [])
            if answer.get("kind") == "number"
            and isinstance(answer.get("value"), (int, float))
            and not isinstance(answer["value"], bool)]


def bank_records(banks, members):
    """Observe only synthetic organization metadata, never owner or request keys."""
    return {"banks": [{"id": bank["_id"], **{key: bank[key] for key in
                      ("name", "archived", "revision")}} for bank in banks],
            "bankItems": [{"id": member["_id"], **{key: member[key] for key in
                          ("bankId", "questionId", "questionVersionId")}} for member in members]}


STATISTICS_FIELDS = {
    "statScopes": ("questionId", "questionVersionId", "respondentType", "createdAt", "expiresAt",
                   "publicationCount", "surveyCount", "instanceCount", "hasForeignSources"),
    "statSources": ("scopeId", "surveyId", "surveyVersionId", "asOfSeq", "accessMode", "grantId", "grantRevision"),
    "statGrants": ("surveyId", "questionId", "permission", "active", "revision"),
}


def statistics_records(scopes, sources, grants):
    """Keep fixed-range provenance and grant state, never account IDs or answers.

    Explicit projection at both boundaries protects observations if a future
    document grows identity, request, content or answer fields. Dates become
    ISO strings so the audit remains plain JSON without a general serializer.
    """
    result = {}
    for label, rows in (("statScopes", scopes), ("statSources", sources), ("statGrants", grants)):
        result[label] = []
        for row in rows:
            metadata = {"id": row["_id"]}
            for key in STATISTICS_FIELDS[label]:
                if key in row:
                    value = row[key]
                    metadata[key] = value.isoformat() if key in ("createdAt", "expiresAt") else value
            result[label].append(metadata)
    return result


async def safe_summary(db):
    """A read transaction keeps each observation internally consistent."""
    async def observe(session):
        roots = await db.questions.find({}, {"ownerId": 0, "creationKey": 0}, session=session).sort("_id", 1).to_list(length=None)
        versions = await db.question_versions.find({}, {"requestKey": 0, "requestDigest": 0}, session=session).sort("versionNo", 1).to_list(length=None)
        surveys = await db.surveys.find({}, {"ownerId": 0, "publicId": 0}, session=session).sort("_id", 1).to_list(length=None)
        publications = await db.survey_versions.find({}, {"publishKey": 0, "publishRequestDigest": 0}, session=session).sort("versionNo", 1).to_list(length=None)
        responses = await db.responses.find({}, {"surveyId": 1, "versionId": 1, "answers.questionId": 1,
                                               "answers.kind": 1, "answers.value": 1}, session=session).to_list(length=None)
        shares = await db.question_shares.find({}, {"questionId": 1, "active": 1, "revision": 1,
                  "permission": 1, "versionScope": 1, "useSequence": 1}, session=session).sort("_id", 1).to_list(length=None)
        banks = await db.question_banks.find({}, {"name": 1, "archived": 1, "revision": 1},
                  session=session).sort("_id", 1).to_list(length=None)
        members = await db.question_bank_items.find({}, {"bankId": 1, "questionId": 1, "questionVersionId": 1},
                    session=session).sort("_id", 1).to_list(length=None)
        scopes = await db.question_stat_scopes.find({}, {key: 1 for key in STATISTICS_FIELDS["statScopes"]},
                    session=session).sort("_id", 1).to_list(length=None)
        stat_sources = await db.question_stat_sources.find({}, {key: 1 for key in STATISTICS_FIELDS["statSources"]},
                    session=session).sort("_id", 1).to_list(length=None)
        stat_grants = await db.question_stat_grants.find({}, {key: 1 for key in STATISTICS_FIELDS["statGrants"]},
                    session=session).sort("_id", 1).to_list(length=None)
        counts = {"users": await db.users.count_documents({}, session=session),
                  "questions": len(roots), "question_versions": len(versions), "surveys": len(surveys),
                  "survey_versions": len(publications), "responses": len(responses), "question_shares": len(shares),
                  "question_banks": len(banks), "question_bank_items": len(members),
                  "question_stat_scopes": len(scopes), "question_stat_sources": len(stat_sources),
                  "question_stat_grants": len(stat_grants)}
        question_rows = []
        for root in roots:
            history = []
            for item in versions:
                if item["questionId"] != root["_id"]:
                    continue
                metadata = {key: item[key] for key in ("operation", "previousCurrentVersionId") if key in item}
                history.append({"id": item["_id"], "versionNo": item["versionNo"],
                                "parentVersionId": item["parentVersionId"],
                                **question_content(item["content"]), **metadata})
            question_rows.append({"id": root["_id"], "currentVersionId": root["currentVersionId"],
                                  "lastVersionNo": root["lastVersionNo"], "versions": history})
        survey_rows = []
        for survey in surveys:
            history = []
            for item in publications:
                if item["surveyId"] != survey["_id"]:
                    continue
                answers = [row for row in responses if row["surveyId"] == survey["_id"] and row["versionId"] == item["_id"]]
                history.append({"id": item["_id"], "versionNo": item["versionNo"],
                                "title": item["definition"]["title"],
                                "questionSources": deepcopy(item.get("questionSources", [])),
                                "questions": survey_definition(item["definition"]),
                                "responseCount": len(answers), "numericAnswers": numeric_answers(answers)})
            survey_rows.append({"id": survey["_id"], "title": survey["draft"]["title"],
                                "status": survey["status"], "draftRevision": survey["draftRevision"],
                                "currentVersionId": survey["currentVersionId"],
                                "draftQuestionSources": deepcopy(survey.get("draftQuestionSources", [])),
                                "draftQuestions": survey_definition(survey["draft"]), "versions": history})
        sharing_rows = [{"id": row["_id"], **{key: row[key] for key in
                        ("questionId", "active", "revision", "permission", "versionScope", "useSequence")}} for row in shares]
        return {"counts": counts, "questions": question_rows, "surveys": survey_rows, "shares": sharing_rows, **bank_records(banks, members),
                **statistics_records(scopes, stat_sources, stat_grants)}

    async with db.client.start_session() as session:
        return await session.with_transaction(observe, read_concern=ReadConcern("snapshot"), max_commit_time_ms=5000)


class BrowserAudit:
    """Observe allowlisted successful writes without changing their payloads."""
    def __init__(self, app, output):
        self.app, self.output = app, output
        self.lock = asyncio.Lock()
        self.state = {"runner": "question-library-browser", "runnerAutomatesBrowser": False,
                      "businessDataSeeded": False, "events": [], "cleanedUp": False}

    def save(self):
        write_json(self.output / "audit.json", self.state)

    async def record(self, method, path, status):
        async with self.lock:
            try:
                business = await safe_summary(self.app.state.db)
                self.state["events"].append({"number": len(self.state["events"]) + 1,
                                             "method": method, "path": path, "status": status,
                                             "business": business})
                self.state["latest"] = business
                self.save()
            except Exception as error:
                # Audit failure must not turn a real accepted write into a fake
                # failure response; expose its type without request/secret text.
                self.state["auditErrorType"] = type(error).__name__
                try:
                    self.save()
                except OSError:
                    print("BROWSER_AUDIT_WRITE_FAILED", flush=True)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method, path = scope["method"], scope["path"]
        monitored = any(method == wanted and pattern.fullmatch(path) for wanted, pattern in WRITE_PATHS)
        status, recorded = 0, False

        async def observe_send(message):
            nonlocal status, recorded
            if message["type"] == "http.response.start":
                status = message["status"]
            if (monitored and not recorded and 200 <= status < 300 and message["type"] == "http.response.body"
                    and not message.get("more_body", False)):
                recorded = True
                await self.record(method, path, status)
            await send(message)

        await self.app(scope, receive, observe_send)


async def run(uri, output):
    if not (ROOT / "frontend/dist/index.html").is_file():
        raise RuntimeError("Build the current frontend before browser acceptance")
    output = output.resolve()
    runtime = (ROOT / ".runtime").resolve()
    if not output.is_relative_to(runtime) or output == runtime:
        raise RuntimeError("Browser output must be a new directory inside the repository's private .runtime")
    output.mkdir(parents=True, exist_ok=False)
    database, run_id = PREFIX + uuid4().hex, uuid4().hex
    ownership = {"runner": "question-library-browser", "database": database, "runId": run_id}
    # main.py also constructs a default app at import time. Disable dotenv first
    # and supply only temporary process configuration for that inert instance.
    dotenv.load_dotenv = lambda *args, **kwargs: False
    temporary = {name: secrets.token_urlsafe(48) for name in ("AUTH_SECRET", "FILL_SECRET", "PARTICIPANT_SECRET")}
    os.environ.update(temporary)
    os.environ.update({"MONGO_URI": uri, "MONGO_DATABASE": database, "ACCESS_SECONDS": "43200", "FILL_SECONDS": "7200"})
    from app.config import Settings
    from app.main import create_app

    settings = Settings(mongo_uri=uri, database=database, auth_secret=temporary["AUTH_SECRET"],
                        fill_secret=temporary["FILL_SECRET"], participant_secret=temporary["PARTICIPANT_SECRET"])
    control = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000)
    owned = False
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    app = create_app(settings)
    audit = BrowserAudit(app, output)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            try:
                audit.state["initial"] = await safe_summary(application.state.db)
                audit.save()
                write_json(output / "bootstrap.json", {"url": origin, "ready": True})
                print("QUESTION_LIBRARY_BROWSER_READY " + json.dumps({"url": origin}), flush=True)
                yield
            finally:
                try:
                    audit.state["final"] = await safe_summary(application.state.db)
                    audit.save()
                except Exception as error:
                    audit.state["finalAuditErrorType"] = type(error).__name__

    app.router.lifespan_context = lifespan
    server = uvicorn.Server(uvicorn.Config(audit, log_level="warning", access_log=False))

    async def stop_watcher():
        while not server.should_exit:
            if (output / "stop.request").is_file():
                server.should_exit = True
                return
            await asyncio.sleep(0.25)

    watcher = None
    try:
        if database in await control.list_database_names():
            raise RuntimeError("Generated browser database already exists")
        await control[database][MARKER].insert_one({"_id": "owner", "runId": run_id})
        owned = True
        # Persist only the random test database and its exact ownership token.
        # A killed process cannot run finally; this private manifest lets a
        # later operator verify the unchanged Mongo marker before cleanup.
        write_json(output / "ownership.json", ownership)
        watcher = asyncio.create_task(stop_watcher())
        await server.serve(sockets=[listener])
    finally:
        if watcher:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        listener.close()
        try:
            if owned:
                marker = await control[database][MARKER].find_one({"_id": "owner"})
                if (not re.fullmatch(PREFIX + r"[0-9a-f]{32}", database)
                        or marker != {"_id": "owner", "runId": run_id}):
                    raise RuntimeError("Browser database ownership mismatch; retained")
                await control.drop_database(database)
                absent = database not in await control.list_database_names()
                audit.state.update(cleanedUp=absent, ownershipVerified=True, databaseAbsentAfterCleanup=absent)
                if not absent:
                    raise RuntimeError("Browser database remains after cleanup")
                write_json(output / "cleanup-receipt.json", {**ownership, "cleanedUp": True,
                           "ownershipVerified": True, "databaseAbsentAfterCleanup": True})
        except Exception as error:
            audit.state["cleanupFailureType"] = type(error).__name__
            raise
        finally:
            try:
                audit.save()
            finally:
                await control.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo-uri", required=True, help="Explicit isolated test replica-set URI; no .env fallback")
    parser.add_argument("--output", type=Path, required=True, help="A new output directory inside the repository's private .runtime")
    args = parser.parse_args()
    asyncio.run(run(args.mongo_uri, args.output.resolve()))
