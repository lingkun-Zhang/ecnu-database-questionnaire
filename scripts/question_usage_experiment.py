"""Compare the real question-usage queries in an owned disposable MongoDB DB.

Example (explicit test server; no .env is read):
    python scripts/question_usage_experiment.py --mongo-uri <test-uri> --output <result.json>

This seeds synthetic documents through the actual collection validators. It is
neither an API write demonstration nor a production throughput benchmark.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import platform
import random
import sys
from uuid import uuid4

from bson.json_util import dumps, RELAXED_JSON_OPTIONS
import pymongo
from pymongo import AsyncMongoClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.database import init_database  # noqa: E402
from app.domain import validate_definition  # noqa: E402
from app.question_usage import (  # noqa: E402
    DRAFT_USAGE_PROJECTION, draft_usage_filter, published_usage_pipeline,
)

PREFIX = "survey_usage_lab_"
SEED = 20261025
OWNER = "owner_000"
QUESTION = "question_target"
VERSION = "question_target_v2"
LIMIT = 30
MARKER = "usage_experiment_ownership"
REFERENCES = [
    "https://www.mongodb.com/docs/manual/core/indexes/index-types/index-multikey/multikey-index-bounds/",
    "https://www.mongodb.com/docs/manual/core/indexes/index-types/index-multikey/",
    "https://www.mongodb.com/docs/manual/tutorial/sort-results-with-indexes/",
]


def signature(value):
    encoded = dumps(value, json_options=RELAXED_JSON_OPTIONS,
                    sort_keys=True, ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def scrub(value, database):
    """Retain plan detail, excluding host/port and cluster-identifying metadata."""
    if isinstance(value, dict):
        return {key: scrub(item, database) for key, item in value.items()
                if key not in {"serverInfo", "$clusterTime", "operationTime", "host", "hostname", "port"}}
    if isinstance(value, list):
        return [scrub(item, database) for item in value]
    if isinstance(value, str):
        return value.replace(database, "<experiment-database>")
    return value


def content():
    # Distinct stable question IDs deliberately share exactly the same wording.
    return {"kind": "number", "title": "合成评分", "config": {"min": 0, "max": 10, "integerOnly": True}}


def definition(title, sources):
    questions = [{"id": item["instanceId"], **content(), "required": False,
                  "defaultTarget": "END", "rules": []} for item in sources]
    result = {"title": title, "description": "隔离查询计划实验", "allowAnonymous": True,
              "entryQuestionId": questions[0]["id"], "questions": questions}
    if validate_definition(result):
        raise RuntimeError("Synthetic definition failed the application domain engine")
    return result


def sources_for(number, edition):
    target = number % 5 == 0
    qid = QUESTION if target else f"question_other_{number % 20:02d}"
    qversion = 2 if target and edition != 1 and number % 100 == 0 else 1
    sources = [{"instanceId": "score", "questionId": qid, "questionVersionId": f"{qid}_v{qversion}"}]
    if target and number % 20 == 0:
        sources.append({**sources[0], "instanceId": "score_again"})
    if number % 100 == 20:
        # Intentionally structurally valid, semantically false source pair:
        # a dot-path query could combine target/v1 with other/target-v2.
        # This is direct synthetic seeding, never described as an allowed API write.
        sources.append({"instanceId": "pair_trap", "questionId": "question_other_01",
                        "questionVersionId": VERSION})
    return sources


async def seed(db):
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    await db.users.insert_many([
        {"_id": f"owner_{number:03d}", "username": f"owner_{number:03d}",
         "usernameKey": f"owner_{number:03d}", "passwordHash": "synthetic-no-login",
         "createdAt": base, "schemaVersion": 1} for number in range(4)
    ])
    roots, question_versions = [], []
    for number, qid in enumerate([QUESTION] + [f"question_other_{n:02d}" for n in range(20)]):
        roots.append({"_id": qid, "ownerId": f"owner_{number % 4:03d}", "creationKey": f"create_{qid}",
                      "currentVersionId": f"{qid}_v2", "lastVersionNo": 2,
                      "createdAt": base, "updatedAt": base, "schemaVersion": 1})
        for version in (1, 2):
            question_versions.append({"_id": f"{qid}_v{version}", "questionId": qid,
                "versionNo": version, "parentVersionId": None if version == 1 else f"{qid}_v1",
                "content": content(), "requestKey": f"create_{qid}" if version == 1 else f"derive_{qid}",
                "requestDigest": "0" * 64, "createdAt": base, "schemaVersion": 1})
    await db.questions.insert_many(roots)
    await db.question_versions.insert_many(question_versions)

    surveys, versions = [], []
    for number in range(1000):
        sid = f"s_{number:04d}"
        sources = sources_for(number, "draft")
        survey = {"_id": sid, "ownerId": f"owner_{number % 4:03d}", "publicId": f"public_{sid}",
            "draft": definition(f"合成草稿 {number:04d}", sources), "draftRevision": 3,
            "draftQuestionSources": sources, "currentVersionId": f"v_{sid}_2", "lastVersionNo": 2,
            "status": "closed" if number % 7 == 0 else "open", "deadlineAt": None,
            "allowMultiple": True, "policyLocked": False, "settingsRevision": 0, "submitSequence": 0,
            "createdAt": base, "updatedAt": base + timedelta(days=1), "schemaVersion": 1}
        surveys.append(survey)
        for edition in (1, 2):
            frozen = sources_for(number, edition)
            versions.append({"_id": f"v_{sid}_{edition}", "surveyId": sid, "versionNo": edition,
                "sourceDraftRevision": edition,
                "definition": definition(f"合成发布 {number:04d} V{edition}", frozen),
                "questionSources": frozen, "publishKey": f"publish_{edition}",
                "publishRequestDigest": "0" * 64, "publishedAt": base + timedelta(seconds=edition),
                "schemaVersion": 1})
    rng = random.Random(SEED)
    for collection, documents in (("surveys", surveys), ("survey_versions", versions)):
        shuffled = documents[:]
        rng.shuffle(shuffled)
        for offset in range(0, len(shuffled), 500):
            await db[collection].insert_many(shuffled[offset:offset + 500])
    expected_counts = {"users": 4, "questions": 21, "question_versions": 42,
                       "surveys": 1000, "survey_versions": 2000, "responses": 0}
    counts = {name: await db[name].count_documents({}) for name in expected_counts}
    if counts != expected_counts:
        raise RuntimeError("Unexpected fixture counts")
    return surveys, versions, counts


def cases():
    results = []
    for scope in ("draft", "published"):
        for exact in (False, True):
            version = VERSION if exact else None
            after = "s_0149" if scope == "draft" else "v_s_0149_2"
            collection = "surveys" if scope == "draft" else "survey_versions"
            index_prefix = "ix_surveys" if scope == "draft" else "ix_versions"
            index = index_prefix + ("_question_version_usage" if exact else "_question_usage")
            if scope == "draft":
                command = {"find": collection, "filter": draft_usage_filter(OWNER, QUESTION, version, after),
                           "projection": deepcopy(DRAFT_USAGE_PROJECTION), "sort": {"_id": 1}, "limit": LIMIT + 1}
            else:
                command = {"aggregate": collection,
                           "pipeline": published_usage_pipeline(OWNER, QUESTION, version, after, LIMIT), "cursor": {}}
            results.append({"id": scope + ("_version" if exact else "_series"), "scope": scope,
                            "collection": collection, "questionVersionId": version,
                            "afterId": after, "index": index,
                            "seriesIndex": index_prefix + "_question_usage", "command": command})
    return results


def expected_rows(case, surveys, versions):
    """Independent projection/filter over original seed documents, not builders."""
    parents = {survey["_id"]: survey for survey in surveys}
    candidates = surveys if case["scope"] == "draft" else versions
    selected = []
    for row in sorted(candidates, key=lambda item: item["_id"]):
        if row["_id"] <= case["afterId"]:
            continue
        parent = row if case["scope"] == "draft" else parents[row["surveyId"]]
        if parent["ownerId"] != OWNER:
            continue
        sources = row["draftQuestionSources"] if case["scope"] == "draft" else row["questionSources"]
        if not any(source["questionId"] == QUESTION and
                   (case["questionVersionId"] is None or source["questionVersionId"] == case["questionVersionId"])
                   for source in sources):
            continue
        if case["scope"] == "draft":
            projected = {"_id": row["_id"], "draft": {"title": row["draft"]["title"]},
                         "draftRevision": row["draftRevision"], "status": row["status"],
                         "draftQuestionSources": deepcopy(sources)}
        else:
            projected = {"_id": row["_id"], "surveyId": row["surveyId"], "versionNo": row["versionNo"],
                         "definition": {"title": row["definition"]["title"]}, "publishedAt": row["publishedAt"],
                         "questionSources": deepcopy(sources), "surveyStatus": parent["status"],
                         "isCurrent": row["_id"] == parent["currentVersionId"]}
        selected.append(projected)
        if len(selected) == LIMIT + 1:
            break
    return selected


def command_for(case, mode):
    command = deepcopy(case["command"])
    if mode == "index":
        command["hint"] = case["index"]
    elif mode == "series_index":
        command["hint"] = case["seriesIndex"]
    elif mode == "collection_scan":
        command["hint"] = {"$natural": 1}
    command["maxTimeMS"] = 10000
    return command


async def execute(db, case, command):
    collection = db[case["collection"]]
    if case["scope"] == "draft":
        cursor = collection.find(command["filter"], command["projection"], max_time_ms=10000).sort("_id", 1)
        if "hint" in command:
            cursor = cursor.hint(command["hint"])
        return await cursor.limit(command["limit"]).to_list(length=None)
    kwargs = {"maxTimeMS": 10000}
    if "hint" in command:
        kwargs["hint"] = command["hint"]
    cursor = await collection.aggregate(command["pipeline"], **kwargs)
    return await cursor.to_list(length=None)


def plan_summary(explained):
    cursor = explained if "executionStats" in explained else next(
        stage["$cursor"] for stage in explained["stages"] if "$cursor" in stage)
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

    visit(cursor["queryPlanner"]["winningPlan"])
    stats = cursor["executionStats"]
    joins = [{key: stage[key] for key in ("totalDocsExamined", "totalKeysExamined", "collectionScans", "indexesUsed", "nReturned")
              if key in stage} for stage in explained.get("stages", []) if "$lookup" in stage]
    return {"winningStages": sorted(stages), "indexesUsed": sorted(indexes),
            "cursorNReturned": stats["nReturned"], "cursorDocsExamined": stats["totalDocsExamined"],
            "cursorKeysExamined": stats["totalKeysExamined"], "cursorExecutionTimeMillis": stats["executionTimeMillis"],
            "blockingSort": "SORT" in stages, "executionSuccess": stats["executionSuccess"], "lookupStages": joins}


async def measure(db, case, surveys, versions):
    expected = expected_rows(case, surveys, versions)
    evidence = {key: deepcopy(value) for key, value in case.items() if key != "command"}
    evidence["command"] = deepcopy(case["command"])
    evidence["expectedContainerIds"] = [row["_id"] for row in expected]
    evidence["expectedResultDocuments"] = len(expected)
    evidence["modes"] = {}
    modes = ["auto", "index", "collection_scan"]
    if case["questionVersionId"] is not None:
        modes.append("series_index")
    for mode in modes:
        command = command_for(case, mode)
        explained = await db.command({"explain": command, "verbosity": "executionStats"})
        rows = await execute(db, case, command)
        if rows != expected:
            raise RuntimeError(f"Query differs from independently projected synthetic data: {case['id']}/{mode}")
        evidence["modes"][mode] = {"plan": plan_summary(explained), "rawExplain": explained,
            "resultDocuments": len(rows), "resultSha256": signature(rows), "independentExpectedPassed": True}
    if len({item["resultSha256"] for item in evidence["modes"].values()}) != 1:
        raise RuntimeError("Query modes returned different projections")
    evidence["resultsEqual"] = True
    return evidence


async def run(uri, output):
    database = PREFIX + uuid4().hex
    run_id = uuid4().hex
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    owned = False
    report = {"experiment": "question-usage-query-plans", "startedAtUTC": datetime.now(timezone.utc).isoformat(), "datasetSeed": SEED,
        "environment": {"python": platform.python_version(), "pymongo": pymongo.version},
        "sourceSha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                         ("scripts/question_usage_experiment.py", "backend/app/question_usage.py", "backend/app/database.py")},
        "method": {"businessDatabaseUsed": False, "realConfigurationRead": False,
            "syntheticDirectInserts": True, "apiWriteDemonstration": False, "throughputBenchmark": False,
            "applicationValidatorsAndIndexes": True, "sort": "container _id ascending, afterId exclusive",
            "pageFetchLimit": LIMIT + 1, "lookupOwnershipBeforeLimit": True,
            "intentionallyInvalidSemanticPairs": "pair_trap sources and cross-owner sources are structural fixtures only; API acceptance is not claimed",
            "comparison": "default plan, named index, forced natural scan; exact-version cases also force series index",
            "timingLimit": "executionStats milliseconds are diagnostic only; no repeated client timing or speedup claim",
            "projectionLimit": "raw container projections compared; endpoint authorization is covered by API tests",
            "explainRedactions": ["serverInfo", "$clusterTime", "operationTime", "host", "hostname", "port", "random database namespace"]},
        "references": REFERENCES, "cases": []}
    try:
        report["environment"]["mongodb"] = (await client.admin.command("buildInfo"))["version"]
        if database in await client.list_database_names():
            raise RuntimeError("Generated experiment database already exists")
        db = client[database]
        await db[MARKER].insert_one({"_id": "owner", "runId": run_id})
        owned = True
        await init_database(db)
        surveys, versions, report["datasetCounts"] = await seed(db)
        report["indexes"] = {name: await db[name].index_information() for name in ("surveys", "survey_versions")}
        for case in cases():
            result = await measure(db, case, surveys, versions)
            report["cases"].append(result)
            print(json.dumps({"case": result["id"], "equal": result["resultsEqual"],
                "cursorDocsExamined": {mode: item["plan"]["cursorDocsExamined"] for mode, item in result["modes"].items()}}), flush=True)
        report["passed"] = True
    except Exception as exc:
        report["passed"] = False
        report["failureType"] = type(exc).__name__
        raise
    finally:
        try:
            if owned:
                marker = await client[database][MARKER].find_one({"_id": "owner"})
                if not database.startswith(PREFIX) or len(database) != len(PREFIX) + 32 or marker != {"_id": "owner", "runId": run_id}:
                    raise RuntimeError("Experiment ownership could not be verified; database retained")
                await client.drop_database(database)
                absent = database not in await client.list_database_names()
                report["cleanup"] = {"ownershipVerified": True, "databaseDropped": absent}
                if not absent:
                    raise RuntimeError("Experiment database still exists after cleanup")
        except Exception as exc:
            report["passed"] = False
            report["cleanupFailureType"] = type(exc).__name__
            raise
        finally:
            await client.close()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(dumps(scrub(report, database), json_options=RELAXED_JSON_OPTIONS,
                                    ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo-uri", required=True, help="Explicit isolated test-server URI; no .env fallback")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.mongo_uri, args.output))


if __name__ == "__main__":
    main()
