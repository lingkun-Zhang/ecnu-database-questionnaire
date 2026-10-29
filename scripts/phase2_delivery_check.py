"""Replay a two-account phase-two workflow through an owned real HTTP server.

Example, after separately starting the dedicated local test replica set:
    python scripts/phase2_delivery_check.py --mongo-uri "mongodb://127.0.0.1:27227/?replicaSet=phase2test" --output .runtime/delivery-check-new

This verifies continuous API behavior, not installation, shipped start/stop
scripts, browser clicks, or production performance. It never reads .env, accepts
an existing database or business URL, or starts/stops the MongoDB server. Only
its randomly named database with an exact ownership marker may be cleaned up.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]
from scripts.phase2_upgrade_rehearsal import check_marker, validate_output, validate_uri

DATABASE_PREFIX = "survey_delivery_"
MARKER_COLLECTION = "delivery_check_ownership"
MARKER_STAGE = "delivery"


class DeliveryCheckError(RuntimeError):
    """A fixed diagnostic without authentication data or response bodies."""


def validate_database(name):
    if not re.fullmatch(DATABASE_PREFIX + r"[a-f0-9]{32}", name):
        raise ValueError("Only a generated delivery-check database is allowed")
    return name


def verify_ownership(name, marker, run_id):
    validate_database(name)
    check_marker(marker, run_id, MARKER_STAGE)


def validate_run_output(path, root=ROOT):
    """Reject overwrite, escape, and redirected runtime ancestors before writes."""
    root = Path(root).resolve()
    runtime = root / ".runtime"
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    for ancestor in (candidate, *candidate.parents):
        if ancestor == root:
            break
        if ancestor.is_symlink() or ancestor.is_junction():
            raise ValueError("Delivery-check output must not use links or junctions")
    if runtime.exists() and not runtime.is_dir():
        raise ValueError("The private runtime path is not a directory")
    return validate_output(candidate, root)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def backend_fingerprints():
    return {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "backend").rglob("*.py"))
    }


def require(condition, message):
    if not condition:
        raise DeliveryCheckError(message)


def public_route(path):
    identity = r"(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})"
    return re.sub(r"/" + identity + r"(?=/|$)", "/{id}", path, flags=re.IGNORECASE)


def pinned_source(version):
    return {"instanceId": "score", "questionId": version["questionId"], "questionVersionId": version["id"]}


def survey_definition(version, title):
    return {
        "title": title, "description": "Synthetic delivery-check data only", "allowAnonymous": True,
        "entryQuestionId": "score", "questions": [
            {"id": "score", **deepcopy(version["content"]), "required": True,
             "defaultTarget": "END", "rules": []}
        ],
    }


class Workflow:
    """Keep HTTP contracts and public observations separate from credentials."""

    def __init__(self, client, output, report):
        self.client, self.output, self.report = client, output, report
        self.accounts = {}

    async def request(self, actor, method, path, body=None, *, status=200, code=None,
                      params=None, key=False, headers=None):
        authorization = {"Authorization": "Bearer " + self.accounts[actor]["accessToken"]} if actor else {}
        response = await self.client.request(method, path, json=body, params=params, headers={
            **authorization, **({"Idempotency-Key": str(uuid4())} if key else {}), **(headers or {}),
        })
        code_matches = True
        if code is not None:
            try:
                error_body = response.json()
                code_matches = isinstance(error_body, dict) and error_body.get("code") == code
            except ValueError:
                code_matches = False
        event = {"number": len(self.report["steps"]) + 1, "actor": actor or "unauthenticated",
                 "method": method, "route": public_route(path), "expectedStatus": status,
                 "status": response.status_code, "passed": response.status_code == status and code_matches}
        if code is not None:
            event["expectedCode"] = code
        self.report["steps"].append(event)
        self.report["requestCount"] = len(self.report["steps"])
        write_json(self.output / "results.json", self.report)
        require(response.status_code == status, "Unexpected HTTP status; see the last recorded step")
        require(code_matches, "Unexpected business error code")
        return response

    async def call(self, *args, **kwargs):
        return (await self.request(*args, **kwargs)).json()

    async def read_survey(self, actor, survey):
        return await self.call(actor, "GET", f'/surveys/{survey["id"]}')

    async def save(self, actor, survey, definition, sources, **kwargs):
        return await self.call(actor, "PUT", f'/surveys/{survey["id"]}/draft', {
            "expectedDraftRevision": survey["draftRevision"], "definition": definition,
            "questionSources": sources,
        }, **kwargs)

    async def create_survey(self, actor, version, title):
        survey = await self.call(actor, "POST", "/surveys", {"title": title}, status=201)
        return await self.save(actor, survey, survey_definition(version, title), [pinned_source(version)])

    async def publish(self, actor, survey):
        return await self.call(actor, "POST", f'/surveys/{survey["id"]}/publish', {
            "expectedDraftRevision": survey["draftRevision"],
            "expectedCurrentVersionId": survey["currentVersionId"], "publishKey": str(uuid4()),
        }, status=201)

    async def submit(self, actor, survey, value, *, reject_zero_first=False):
        attempt = await self.call(actor, "POST", f'/survey/{survey["publicId"]}/start')
        body = {"versionId": attempt["versionId"], "answers": {"score": value}, "anonymous": True}
        fill = {"X-Fill-Token": attempt["fillToken"]}
        if reject_zero_first:
            await self.call(actor, "POST", f'/survey/{survey["publicId"]}/submit',
                            {**body, "answers": {"score": 0}}, headers=fill, key=True, status=422)
        return await self.call(actor, "POST", f'/survey/{survey["publicId"]}/submit',
                               body, headers=fill, key=True, status=201)

    async def scope(self, question):
        return await self.call("author", "POST", f'/questions/{question["questionId"]}/statistics/scopes',
                               {"questionVersionId": None, "respondentType": "all"}, key=True, status=201)

    async def statistics(self, question, scope, suffix="", **kwargs):
        return await self.request("author", "GET", f'/questions/{question["questionId"]}/statistics{suffix}',
                                  params={"scopeId": scope["id"]}, **kwargs)

    async def check_totals(self, question, scope, expected):
        result = (await self.statistics(question, scope)).json()
        actual = {row["versionNo"]: (row["A"], row["sum"], row["avg"]) for row in result["versions"]}
        require(actual == expected, "Per-version numeric totals differ from the synthetic input")
        return result

    async def run(self):
        health = await self.call(None, "GET", "/health")
        require(health["status"] == "ok", "Application health check did not succeed")
        self.report["health"] = health
        for actor in ("author", "recipient"):
            self.accounts[actor] = await self.call(None, "POST", "/auth/register", {
                "username": "delivery_" + actor, "password": secrets.token_urlsafe(24),
            }, status=201)

        content = {"kind": "number", "title": "Delivery score V1",
                   "config": {"min": 0, "max": 7, "integerOnly": True}}
        first = await self.call("author", "POST", "/questions", {"content": content}, key=True, status=201)
        question_id = first["questionId"]
        bank = await self.call("author", "POST", "/question-banks", {
            "name": "Delivery pinned questions", "description": "Synthetic data",
        }, key=True, status=201)

        async def pin(version):
            nonlocal bank
            bank = await self.call("author", "PUT", f'/question-banks/{bank["id"]}/questions/{question_id}', {
                "questionVersionId": version["id"], "expectedRevision": bank["revision"],
            })

        async def share(active, revision):
            return await self.call("author", "PUT", f"/questions/{question_id}/shares", {
                "recipientUsername": self.accounts["recipient"]["user"]["username"],
                "active": active, "expectedRevision": revision,
            }, status=201 if revision == 0 else 200)

        await pin(first)
        await share(True, 0)
        own_a = await self.create_survey("author", first, "Delivery A fixed V1")
        own_publication = await self.publish("author", own_a)
        own_a = await self.read_survey("author", own_a)
        foreign = await self.create_survey("recipient", first, "Private delivery D")
        await self.publish("recipient", foreign)
        foreign = await self.read_survey("recipient", foreign)
        impact = await self.call("author", "GET", f"/questions/{question_id}/impact")
        require(impact["summary"]["own"]["surveyCount"] == impact["summary"]["others"]["surveyCount"] == 1,
                "Impact did not include both actual source surveys")
        foreign_impacts = [row for row in impact["items"] if row["ownership"] == "other"]
        require(len(foreign_impacts) == 1 and not {"surveyId", "surveyTitle", "surveyStatus"}.intersection(foreign_impacts[0]),
                "Impact exposed foreign survey metadata")

        second = await self.call("author", "POST", f"/questions/{question_id}/versions", {
            "content": {**content, "title": "Delivery score V2", "config": {"min": 1, "max": 5, "integerOnly": True}},
            "parentVersionId": first["id"], "expectedCurrentVersionId": first["id"],
        }, key=True, status=201)
        require((await self.read_survey("author", own_a))["draftQuestionSources"] == [pinned_source(first)],
                "A new question version changed an existing survey pin")
        members = await self.call("author", "GET", f'/question-banks/{bank["id"]}/questions')
        require(members["items"][0]["questionVersionId"] == first["id"], "A bank member changed version implicitly")
        await pin(second)
        own_b = await self.create_survey("author", second, "Delivery B fixed V2")
        await self.publish("author", own_b)
        own_b = await self.read_survey("author", own_b)

        await share(False, 1)
        await self.call("recipient", "GET", f"/questions/{question_id}", status=404, code="NOT_FOUND")
        rejected = await self.call("recipient", "POST", "/surveys", {"title": "Rejected new source"}, status=201)
        await self.save("recipient", rejected, survey_definition(first, "Rejected new source"),
                        [pinned_source(first)], status=404, code="NOT_FOUND")
        require((await self.read_survey("recipient", rejected))["draftRevision"] == 0,
                "A rejected new binding partially saved its draft")
        await self.save("recipient", foreign, survey_definition(second, foreign["draft"]["title"]),
                        [pinned_source(second)], status=404, code="NOT_FOUND")
        retained = deepcopy(foreign["draft"])
        retained["description"] = "Retained saved V1 binding after use revocation"
        foreign = await self.save("recipient", foreign, retained, [pinned_source(first)])
        foreign_publication = await self.publish("recipient", foreign)
        foreign = await self.read_survey("recipient", foreign)
        require(foreign_publication["questionSources"] == own_publication["questionSources"] == [pinned_source(first)],
                "Retained publication did not preserve its exact fixed binding")
        await self.submit("author", own_a, 0)
        await self.submit("author", own_b, 3, reject_zero_first=True)
        await self.submit("recipient", foreign, 3)

        own_only = {1: (1, 0, 0), 2: (1, 3, 3)}
        authorized = {1: (2, 3, 1.5), 2: (1, 3, 3)}
        before = await self.scope(first)
        before_statistics = await self.check_totals(first, before, own_only)
        grant_path = f'/surveys/{foreign["id"]}/question-statistics-grants/{question_id}'
        await self.call("recipient", "PUT", grant_path, {"active": True, "expectedRevision": 0})
        require(await self.check_totals(first, before, own_only) == before_statistics,
                "A previously captured scope silently gained an authorized source")
        combined = await self.scope(first)
        await self.check_totals(first, combined, authorized)
        sources = (await self.statistics(first, combined, "/sources")).json()["items"]
        aggregate_sources = [row for row in sources if row["accessMode"] == "aggregate"]
        require(aggregate_sources and all(not {"surveyId", "surveyVersionId"}.intersection(row)
                and not row["canReadAnswers"] for row in aggregate_sources), "Foreign source metadata exposed raw access")
        answers = (await self.statistics(first, combined, "/answers")).json()["items"]
        require(sorted(row["value"] for row in answers) == [0, 3], "Foreign answer entered the owner's detail rows")
        exported = await self.statistics(first, combined, "/export")
        csv_rows = list(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff"))))
        require(sorted(float(row["答案"]) for row in csv_rows) == [0, 3], "CSV included a foreign answer or lost own data")
        await self.call("author", "GET", f'/surveys/{foreign["id"]}/stats',
                        params={"versionId": foreign_publication["id"]}, status=404, code="NOT_FOUND")

        await self.call("recipient", "PUT", grant_path, {"active": False, "expectedRevision": 1})
        for suffix in ("", "/sources", "/answers", "/export"):
            await self.statistics(first, combined, suffix, status=409, code="STATISTICS_SCOPE_INVALIDATED")
        revoked = await self.scope(first)
        await self.check_totals(first, revoked, own_only)
        await self.call("recipient", "PUT", grant_path, {"active": True, "expectedRevision": 2})
        await self.statistics(first, combined, status=409, code="STATISTICS_SCOPE_INVALIDATED")
        refreshed = await self.scope(first)
        await self.check_totals(first, refreshed, authorized)

        third = await self.call("author", "POST", f"/questions/{question_id}/restore", {
            "targetVersionId": first["id"], "expectedCurrentVersionId": second["id"],
        }, key=True, status=201)
        require(third["versionNo"] == 3 and third["content"] == first["content"] and third["operation"] == "restore",
                "Restore did not append a new version containing V1 content")
        require(await self.call("author", "GET", f'/questions/{question_id}/versions/{first["id"]}') == first,
                "Restore rewrote historical V1 content")
        for actor, survey, pinned in (("author", own_a, first), ("author", own_b, second), ("recipient", foreign, first)):
            require((await self.read_survey(actor, survey))["draftQuestionSources"] == [pinned_source(pinned)],
                    "Restore changed a saved survey binding")
        members = await self.call("author", "GET", f'/question-banks/{bank["id"]}/questions')
        require(members["items"][0]["questionVersionId"] == second["id"], "Restore changed a fixed bank member")
        self.report["checks"] = {
            "versionPinsPreserved": True, "foreignImpactMetadataHidden": True,
            "revocationDeniesNewBindingAndVersionSwitch": True, "revokedSavedBindingRepublished": True,
            "zeroRejectedByV2SubmitApi": True, "acceptedResponses": 3,
            "authorizedV1": {"A": 2, "sum": 3, "avg": 1.5}, "authorizedV2": {"A": 1, "sum": 3, "avg": 3},
            "ownDetailAndCsvValues": [0, 3], "foreignRawAccessDenied": True,
            "grantRevocationInvalidatesAllFourReadEndpoints": True, "regrantDoesNotReviveOldScope": True,
            "restoreAppendsV3WithoutChangingV1OrPins": True,
        }


async def run(uri, output):
    """Own the whole app/database lifetime; do not attach to another service."""
    validate_uri(uri)
    output = validate_run_output(output)
    if "app.main" in sys.modules:
        raise RuntimeError("Run the delivery check in a fresh Python process")
    output.mkdir(parents=True, exist_ok=False)
    database, run_id = validate_database(DATABASE_PREFIX + uuid4().hex), uuid4().hex
    marker = {"_id": "owner", "runId": run_id, "stage": MARKER_STAGE}
    write_json(output / "ownership.private.json", {"database": database, "marker": marker})
    sources = backend_fingerprints()
    report = {"mode": "owned real TCP HTTP API workflow", "startedAt": datetime.now(timezone.utc).isoformat(),
              "passed": False, "steps": [], "requestCount": 0, "backendSourceSha256": sources,
              "runnerSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "temporarySyntheticDataOnly": True, "installationOrShippedLifecycleTested": False,
              "browserClicksTested": False, "mongoServerStartedOrStopped": False,
              "databaseCleanup": {"ownedDatabaseAbsent": False}, "serverStoppedNormally": False}
    write_json(output / "results.json", report)

    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    temporary = {name: secrets.token_urlsafe(48) for name in ("AUTH_SECRET", "FILL_SECRET", "PARTICIPANT_SECRET")}
    os.environ.update({**temporary, "MONGO_URI": uri, "MONGO_DATABASE": database})
    from app import config
    config.load_dotenv = lambda *args, **kwargs: False
    from app.main import create_app
    from pymongo import AsyncMongoClient
    import httpx
    import uvicorn

    settings = config.Settings(mongo_uri=uri, database=database, auth_secret=temporary["AUTH_SECRET"],
                               fill_secret=temporary["FILL_SECRET"], participant_secret=temporary["PARTICIPANT_SECRET"])
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000)
    listener, server, service = None, None, None
    candidate_created, workflow_passed = False, False
    failure = None
    try:
        hello = await client.admin.command("hello")
        require(hello.get("setName") == "phase2test" and hello.get("isWritablePrimary") is True,
                "The explicit test replica set is not a writable phase2test primary")
        require(database not in await client.list_database_names(), "Generated database already exists")
        candidate_created = True
        await client[database][MARKER_COLLECTION].insert_one(marker)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        log_config = {"version": 1, "disable_existing_loggers": False,
                      "formatters": {"plain": {"format": "%(levelname)s %(message)s"}},
                      "handlers": {"private": {"class": "logging.FileHandler", "filename": str(output / "server.private.log"),
                                               "encoding": "utf-8", "formatter": "plain"}},
                      "loggers": {name: {"handlers": ["private"], "level": "INFO", "propagate": False}
                                  for name in ("uvicorn", "uvicorn.error", "survey")}}
        server = uvicorn.Server(uvicorn.Config(create_app(settings), access_log=False, log_config=log_config))
        service = asyncio.create_task(server.serve(sockets=[listener]))
        deadline = asyncio.get_running_loop().time() + 30
        while not server.started:
            require(not service.done(), "The owned HTTP application did not start")
            require(asyncio.get_running_loop().time() < deadline, "The owned HTTP application startup timed out")
            await asyncio.sleep(0.05)
        async with httpx.AsyncClient(base_url=origin + "/api", trust_env=False, timeout=30) as http:
            await Workflow(http, output, report).run()
        require(backend_fingerprints() == sources, "Backend sources changed during verification")
        report["sourceFilesUnchangedDuringRun"] = True
        workflow_passed = True
    except BaseException as error:
        failure = error
        report["failureType"] = type(error).__name__
    finally:
        try:
            if server is not None and service is not None:
                server.should_exit = True
                await asyncio.wait_for(asyncio.shield(service), timeout=20)
                report["serverStoppedNormally"] = True
        except BaseException as error:
            failure = failure or error
            report["shutdownFailureType"] = type(error).__name__
        finally:
            if listener is not None:
                listener.close()
            try:
                if candidate_created:
                    require(service is None or service.done(),
                            "Owned HTTP application has not stopped; retain its database")
                    if database in await client.list_database_names():
                        current = await client[database][MARKER_COLLECTION].find_one({"_id": "owner"})
                        verify_ownership(database, current, run_id)
                        await client.drop_database(database)
                        report["databaseCleanup"]["exactOwnershipVerified"] = True
                    absent = database not in await client.list_database_names()
                    require(absent, "Owned database remains after cleanup")
                    report["databaseCleanup"]["ownedDatabaseAbsent"] = True
            except BaseException as error:
                failure = failure or error
                report["databaseCleanup"]["failureType"] = type(error).__name__
                report["databaseCleanup"]["retainedOnOwnershipMismatch"] = True
            finally:
                await client.close()
                report["finishedAt"] = datetime.now(timezone.utc).isoformat()
                report["passed"] = bool(workflow_passed and failure is None and report["serverStoppedNormally"]
                                        and report["databaseCleanup"]["ownedDatabaseAbsent"])
                write_json(output / "results.json", report)
    if failure is not None:
        raise DeliveryCheckError("Continuous verification failed; inspect results.json and the private service log") from None
    return report


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo-uri", required=True, help="Explicit credential-free 127.0.0.1:27227 phase2test URI")
    parser.add_argument("--output", required=True, type=Path, help="A new directory inside this repository's .runtime")
    return parser


def main():
    args = build_parser().parse_args()
    try:
        report = asyncio.run(run(args.mongo_uri, args.output))
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({"passed": False, "failureType": type(error).__name__,
                          "diagnostic": "Check parameters or the new private output directory; no response bodies were printed"}))
        return 1
    print(json.dumps({"passed": report["passed"], "requestCount": report["requestCount"],
                      "ownedDatabaseAbsent": report["databaseCleanup"]["ownedDatabaseAbsent"],
                      "serverStoppedNormally": report["serverStoppedNormally"],
                      "resultsFile": "results.json", "installationOrShippedLifecycleTested": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
