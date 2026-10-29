"""Read-only fingerprints for all business collections in the selected project.

Collection names follow that project's VALIDATORS, so stage one covers four
and stage two covers twelve. One snapshot fixes the read across collections.
Only counts and BSON-preserving hashes leave the database; never raw secrets.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

from bson.json_util import CANONICAL_JSON_OPTIONS, dumps
from pymongo import AsyncMongoClient
from pymongo.read_concern import ReadConcern


async def business_fingerprints(db, collection_names):
    async def observe(session):
        result = {}
        for name in sorted(collection_names):
            values = await db[name].find({}, session=session).sort("_id", 1).to_list(length=None)
            encoded = dumps(values, json_options=CANONICAL_JSON_OPTIONS, sort_keys=True,
                            ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            result[name] = {"count": len(values), "sha256": hashlib.sha256(encoded).hexdigest()}
        return result

    async with db.client.start_session() as session:
        return await session.with_transaction(observe, read_concern=ReadConcern("snapshot"),
                                               max_commit_time_ms=5000)


async def snapshot(project):
    sys.path.insert(0, str(project / "backend"))
    from app.config import Settings
    from app.database import VALIDATORS

    settings = Settings.from_env()
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=3000)
    try:
        return {"envSha256": hashlib.sha256((project / ".env").read_bytes()).hexdigest(),
                "collections": await business_fingerprints(client[settings.database], VALIDATORS)}
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(snapshot(args.project.resolve()))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"snapshotSaved": True, "counts": {name: value["count"]
          for name, value in result["collections"].items()}}, ensure_ascii=False))
