"""Read-only fingerprints for a start/stop acceptance run; never prints secrets."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

from bson.json_util import CANONICAL_JSON_OPTIONS, dumps
from pymongo import AsyncMongoClient


async def snapshot(project):
    sys.path.insert(0, str(project / "backend"))
    from app.config import Settings

    settings = Settings.from_env()
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=3000)
    try:
        result = {"envSha256": hashlib.sha256((project / ".env").read_bytes()).hexdigest(), "collections": {}}
        for name in ("users", "surveys", "survey_versions", "responses"):
            values = await client[settings.database][name].find({}).sort("_id", 1).to_list(length=None)
            encoded = dumps(values, json_options=CANONICAL_JSON_OPTIONS, sort_keys=True,
                            ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            result["collections"][name] = {"count": len(values), "sha256": hashlib.sha256(encoded).hexdigest()}
        return result
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
