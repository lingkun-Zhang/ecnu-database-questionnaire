"""Reproduce read-only query comparisons in a newly owned, disposable MongoDB database.

Run from the project virtualenv: python scripts/index_experiment.py --output result.json
Uses the application's validators, indexes, scope and aggregation builder. Synthetic
documents are inserted directly; this is a database experiment, not an API load test.
No user-provided database name is accepted, and the configured business DB is unused.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import statistics
import sys
import time
from uuid import uuid4

from bson.json_util import dumps, RELAXED_JSON_OPTIONS
from dotenv import dotenv_values
import pymongo
from pymongo import AsyncMongoClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))
from app.database import init_database  # noqa: E402
from app.domain import evaluate  # noqa: E402
from app.statistics import build_response_scope, build_stats_pipeline  # noqa: E402
from scripts.fixtures import example_definition, ROWS  # noqa: E402

PREFIX = "survey_indexlab_"
SEED = 20261021
MODES = ("auto", "index", "collection_scan")


def stable(value, unordered=False):
    if isinstance(value, dict):
        return {key: stable(item, unordered) for key, item in sorted(value.items())}
    if isinstance(value, list):
        items = [stable(item, unordered) for item in value]
        return sorted(items, key=lambda item: dumps(item, sort_keys=True)) if unordered else items
    return value


def digest(value, unordered=False):
    data = dumps(stable(value, unordered), json_options=RELAXED_JSON_OPTIONS,
                 sort_keys=True, ensure_ascii=True).encode()
    return hashlib.sha256(data).hexdigest()


async def seed(db):
    definition = example_definition()
    prepared = [evaluate(definition, raw) for _, _, raw in ROWS]
    if any(item["errors"] or not item["complete"] or item["provisional"] for item in prepared):
        raise RuntimeError("Synthetic fixture does not pass the application domain engine")
    kinds = {q["id"]: q["kind"] for q in definition["questions"]}
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    usernames = [f"owner_{i:03d}" for i in range(20)] + sorted({row[0] for row in ROWS})
    # These accounts cannot log in: no real passwords or authentication secrets.
    await db.users.insert_many([
        {"_id": name, "username": name, "usernameKey": name,
         "passwordHash": "synthetic-account-no-login", "createdAt": base, "schemaVersion": 1}
        for name in usernames
    ])
    surveys, versions, responses = [], [], []
    for number in range(1000):
        sid = f"s_{number:04d}"
        definition_copy = deepcopy(definition)
        definition_copy["title"] = f"索引实验合成问卷 {number:04d}"
        created = base + timedelta(seconds=number // 40)  # Ties exercise the _id sort key.
        surveys.append({
            "_id": sid, "ownerId": f"owner_{number % 20:03d}", "publicId": f"public_{sid}",
            "draft": definition_copy, "draftRevision": 2, "currentVersionId": f"{sid}_v2",
            "lastVersionNo": 2, "status": "open", "deadlineAt": None,
            "allowMultiple": True, "policyLocked": number < 100, "settingsRevision": 0,
            "submitSequence": 200 if number < 100 else 0,
            "createdAt": created, "updatedAt": base + timedelta(days=1), "schemaVersion": 1,
        })
        for version_no in (1, 2):
            versions.append({
                "_id": f"{sid}_v{version_no}", "surveyId": sid, "versionNo": version_no,
                "sourceDraftRevision": version_no, "definition": deepcopy(definition_copy),
                "publishKey": f"publish_{version_no}", "publishRequestDigest": "synthetic-digest",
                "publishedAt": created + timedelta(seconds=version_no), "schemaVersion": 1,
            })
        if number >= 100:
            continue
        for seq in range(1, 201):
            row_index = (seq - 1) % len(ROWS)
            username, anonymous, _ = ROWS[row_index]
            result = prepared[row_index]
            document = {
                "_id": f"{sid}_r{seq:03d}", "surveyId": sid,
                "versionId": f"{sid}_v{1 if seq <= 100 else 2}",
                "participantKey": f"synthetic_{sid}_{username}", "submissionKey": f"submit_{seq}",
                "payloadDigest": "synthetic-digest", "canonicalizationVersion": 1,
                "anonymous": anonymous,
                "answers": [{"questionId": qid, "kind": kinds[qid],
                             "value": result["activeAnswers"][qid]}
                            for qid in result["path"] if qid in result["activeAnswers"]],
                "path": result["path"], "acceptedSeq": seq,
                "acceptedAt": base + timedelta(days=1, seconds=seq), "schemaVersion": 1,
            }
            if not anonymous:
                document["identity"] = {"userId": username, "usernameAtSubmit": username}
            responses.append(document)
    rng = random.Random(SEED)
    for name, documents in (("surveys", surveys), ("survey_versions", versions), ("responses", responses)):
        rng.shuffle(documents)
        for offset in range(0, len(documents), 1000):
            await db[name].insert_many(documents[offset:offset + 1000])
    counts = {name: await db[name].count_documents({}) for name in
              ("users", "surveys", "survey_versions", "responses")}
    if counts != {"users": 25, "surveys": 1000, "survey_versions": 2000, "responses": 20000}:
        raise RuntimeError("Unexpected fixture counts")
    return definition, counts


def scenarios(definition):
    sid, vid = "s_0000", "s_0000_v1"
    scope = build_response_scope(sid, vid, 80)
    scope["acceptedSeq"]["$lt"] = 60
    question_ids = [q["id"] for q in definition["questions"]]
    return [
        {"id": "owner_list", "label": "所有者问卷列表（首页30份）", "collection": "surveys",
         "index": "ix_surveys_owner_created", "kind": "find",
         "command": {"find": "surveys", "filter": {"ownerId": "owner_000"},
                     "sort": {"createdAt": -1, "_id": -1}, "limit": 30},
         "source": "backend/app/main.py:list_surveys"},
        {"id": "submission_replay", "label": "同一次提交的幂等回执查找", "collection": "responses",
         "index": "uq_responses_submission", "kind": "find",
         "command": {"find": "responses", "filter": {"surveyId": sid,
                     "participantKey": "synthetic_s_0000_sample_01", "submissionKey": "submit_1"},
                     "limit": 1}, "source": "backend/app/repositories.py:prior_submission"},
        {"id": "response_page", "label": "固定版本和范围的答卷游标页（30+1）", "collection": "responses",
         "index": "ix_responses_version_sequence", "kind": "find",
         "command": {"find": "responses", "filter": scope, "sort": {"acceptedSeq": -1}, "limit": 31},
         "source": "backend/app/main.py:response_page; app.statistics.build_response_scope"},
        *[{"id": f"stats_{mode}", "label": "完整统计聚合（" + ("全部" if mode == "all" else "匿名") + "）",
           "collection": "responses", "index": "ix_responses_version_sequence", "kind": "aggregate",
           "command": {"aggregate": "responses", "pipeline": build_stats_pipeline(sid, vid, 80, question_ids, mode),
                       "cursor": {}}, "source": "backend/app/statistics.py:build_stats_pipeline"}
          for mode in ("all", "anonymous")],
    ]


def command_for(case, mode):
    command = deepcopy(case["command"])
    if mode == "index":
        command["hint"] = case["index"]
    elif mode == "collection_scan":
        command["hint"] = {"$natural": 1}
    command["maxTimeMS"] = 30000
    return command


async def execute(db, case, mode):
    command = command_for(case, mode)
    collection = db[case["collection"]]
    if case["kind"] == "find":
        cursor = collection.find(command["filter"], max_time_ms=30000)
        if "sort" in command:
            cursor = cursor.sort(list(command["sort"].items()))
        if "hint" in command:
            cursor = cursor.hint(command["hint"])
        return await cursor.limit(command["limit"]).to_list(length=None)
    kwargs = {"maxTimeMS": 30000}
    if "hint" in command:
        kwargs["hint"] = command["hint"]
    cursor = await collection.aggregate(command["pipeline"], **kwargs)
    return await cursor.to_list(length=None)


def plan_summary(explained):
    if "executionStats" in explained:
        cursor = explained
    else:
        cursor = next(stage["$cursor"] for stage in explained["stages"] if "$cursor" in stage)
    winning = cursor["queryPlanner"]["winningPlan"]
    stages, indexes = set(), set()

    def visit(node):
        if isinstance(node, dict):
            if "stage" in node:
                stages.add(node["stage"])
            if "indexName" in node:
                indexes.add(node["indexName"])
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(winning)
    stats = cursor["executionStats"]
    return {"winningStages": sorted(stages), "indexesUsed": sorted(indexes),
            "cursorNReturned": stats["nReturned"], "totalDocsExamined": stats["totalDocsExamined"],
            "totalKeysExamined": stats["totalKeysExamined"],
            "cursorExecutionTimeMillis": stats["executionTimeMillis"],
            "blockingSort": "SORT" in stages, "executionSuccess": stats["executionSuccess"]}


def check_expected(case, rows):
    if case["id"] == "owner_list":
        expected = sorted(range(0, 1000, 20), key=lambda n: (n // 40, f"s_{n:04d}"), reverse=True)[:30]
        if [row["_id"] for row in rows] != [f"s_{n:04d}" for n in expected]:
            raise RuntimeError("Owner list differs from independently computed order")
    elif case["id"] == "submission_replay":
        if [row["_id"] for row in rows] != ["s_0000_r001"]:
            raise RuntimeError("Incorrect submission lookup")
    elif case["id"] == "response_page":
        if [row["acceptedSeq"] for row in rows] != list(range(59, 28, -1)):
            raise RuntimeError("Incorrect cursor order or boundary")
    else:
        selected = [ROWS[(seq - 1) % 6] for seq in range(1, 81)
                    if case["id"] == "stats_all" or ROWS[(seq - 1) % 6][1]]
        overview = rows[0]["overview"][0]
        expected = {"totalResponses": len(selected),
                    "anonymousResponses": sum(row[1] for row in selected),
                    "namedResponses": sum(not row[1] for row in selected)}
        if any(overview[key] != value for key, value in expected.items()):
            raise RuntimeError("Aggregation counts differ from independent sample arithmetic")
        if rows[0]["participants"] != [{"count": len({row[0] for row in selected})}]:
            raise RuntimeError("Incorrect participant count")
        score = next(row for row in rows[0]["answered"] if row["_id"] == "q_score")
        scores = [row[2]["q_score"] for row in selected if "q_score" in row[2]]
        if score["count"] != len(scores) or score["sum"] != sum(scores) or abs(score["avg"] - statistics.mean(scores)) > 1e-12:
            raise RuntimeError("Incorrect numeric aggregate, including zero")


async def measure(db, case, repeats):
    evidence = {key: value for key, value in case.items() if key != "kind"}
    evidence["modes"] = {}
    expected_digest = None
    for mode in MODES:
        explained = await db.command({"explain": command_for(case, mode), "verbosity": "executionStats"})
        rows = await execute(db, case, mode)  # One untimed warm-up per mode.
        check_expected(case, rows)
        current_digest = digest(rows, unordered=case["kind"] == "aggregate")
        if expected_digest is not None and expected_digest != current_digest:
            raise RuntimeError(f"Result mismatch for {case['id']}/{mode}")
        expected_digest = current_digest
        evidence["modes"][mode] = {"plan": plan_summary(explained), "rawExplain": explained,
                                    "resultDocuments": len(rows), "resultSha256": current_digest,
                                    "clientMilliseconds": []}
    rng = random.Random(SEED)
    for _ in range(repeats):
        modes = list(MODES)
        rng.shuffle(modes)  # Interleave modes instead of timing all scans last.
        for mode in modes:
            begin = time.perf_counter()
            rows = await execute(db, case, mode)
            elapsed = (time.perf_counter() - begin) * 1000
            if digest(rows, unordered=case["kind"] == "aggregate") != expected_digest:
                raise RuntimeError("Timed query returned inconsistent results")
            evidence["modes"][mode]["clientMilliseconds"].append(round(elapsed, 4))
    for result in evidence["modes"].values():
        result["clientMedianMilliseconds"] = round(statistics.median(result["clientMilliseconds"]), 4)
    evidence["resultsEqual"] = True
    evidence["independentExpectedPassed"] = True
    return evidence


async def run(output, repeats):
    env = dotenv_values(ROOT / ".env")
    uri = os.getenv("MONGO_URI") or env.get("MONGO_URI") or "mongodb://127.0.0.1:27117/?replicaSet=rs0"
    name = PREFIX + uuid4().hex
    run_id = uuid4().hex
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    owned = False
    report = {"experiment": "first-stage-index-plans", "stage": 1,
              "startedAtUTC": datetime.now(timezone.utc).isoformat(), "database": name,
              "datasetSeed": SEED, "repeatsPerMode": repeats, "cases": [],
              "environment": {"python": platform.python_version(), "platform": platform.platform(),
                              "pymongo": pymongo.version},
              "sourceSha256": {rel: hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() for rel in
                               ("scripts/index_experiment.py", "scripts/fixtures.py", "backend/app/database.py",
                                "backend/app/domain.py", "backend/app/statistics.py", "backend/app/main.py",
                                "backend/app/repositories.py")},
              "method": {"productionDatabaseUsed": False, "apiLoadTest": False,
                         "businessValidatorsEnabled": True, "applicationIndexesOnly": True,
                         "warmupsPerMode": 1, "timingOrder": "deterministically shuffled each round",
                         "clientTimingIncludes": "cursor creation, network, all batches and BSON decoding; excludes digest/check",
                         "scanBaseline": "hint({$natural:1}); existing indexes are not removed",
                         "cacheState": "warm; no OS/database cache reset; explain ignores plan cache"}}
    try:
        report["environment"]["mongodb"] = (await client.admin.command("buildInfo"))["version"]
        if name in await client.list_database_names():
            raise RuntimeError("Generated database already exists")
        db = client[name]
        await db["experiment_ownership"].insert_one({"_id": "owner", "runId": run_id})
        owned = True
        await init_database(db)
        definition, report["datasetCounts"] = await seed(db)
        report["collectionOptions"] = {name: item.get("options", {}) for name, item in
            [(entry["name"], entry) async for entry in await db.list_collections()]
            if name in ("users", "surveys", "survey_versions", "responses")}
        report["indexes"] = {name: await db[name].index_information() for name in report["datasetCounts"]}
        print(json.dumps({"seeded": report["datasetCounts"], "database": name}), flush=True)
        for case in scenarios(definition):
            result = await measure(db, case, repeats)
            report["cases"].append(result)
            print(json.dumps({"case": result["id"], "equal": result["resultsEqual"],
                "docsExamined": {mode: value["plan"]["totalDocsExamined"] for mode, value in result["modes"].items()}}), flush=True)
        report["passed"] = True
    except Exception as exc:
        report["passed"] = False
        report["failureType"] = type(exc).__name__
        raise
    finally:
        try:
            if owned:
                marker = await client[name]["experiment_ownership"].find_one({"_id": "owner"})
                if not name.startswith(PREFIX) or len(name) != len(PREFIX) + 32 or marker != {"_id": "owner", "runId": run_id}:
                    raise RuntimeError("Cleanup ownership verification failed")
                await client.drop_database(name)
                report["databaseAbsentAfterCleanup"] = name not in await client.list_database_names()
                if not report["databaseAbsentAfterCleanup"]:
                    raise RuntimeError("Owned database still exists after cleanup")
        finally:
            await client.close()
            report["finishedAtUTC"] = datetime.now(timezone.utc).isoformat()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(dumps(report, json_options=RELAXED_JSON_OPTIONS, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=7, choices=range(3, 16))
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a new path to preserve previous evidence")
    asyncio.run(run(args.output.resolve(), args.repeats))
