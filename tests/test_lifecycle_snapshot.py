"""Snapshot coverage against the real disposable API/MongoDB fixture."""
import importlib.util
from pathlib import Path
from uuid import uuid4

from app.database import VALIDATORS
from conftest import account

path = Path(__file__).parent / "support/lifecycle_snapshot.py"
spec = importlib.util.spec_from_file_location("lifecycle_fingerprint", path)
snapshot_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot_tool)


async def test_snapshot_detects_new_question_and_bank_data_without_changing_old_collections(context):
    owner, _ = await account(context)
    before = await snapshot_tool.business_fingerprints(context.db, VALIDATORS)
    version = await context.client.post("/api/questions", headers={**owner, "Idempotency-Key": str(uuid4())},
        json={"content": {"kind": "number", "title": "Fingerprint score", "config": {"min": 0, "max": 7}}})
    assert version.status_code == 201
    question = version.json()
    bank = await context.client.post("/api/question-banks", headers={**owner, "Idempotency-Key": str(uuid4())},
        json={"name": "Fingerprint bank", "description": "Synthetic data"})
    assert bank.status_code == 201
    pin = await context.client.put(f'/api/question-banks/{bank.json()["id"]}/questions/{question["questionId"]}',
        headers=owner, json={"questionVersionId": question["id"], "expectedRevision": 0})
    assert pin.status_code == 200
    after = await snapshot_tool.business_fingerprints(context.db, VALIDATORS)
    assert set(after) == set(VALIDATORS) and len(after) == 12
    for name in ("questions", "question_versions", "question_banks", "question_bank_items"):
        assert before[name]["count"] == 0 and after[name]["count"] == 1
        assert before[name]["sha256"] != after[name]["sha256"]
    for name in ("users", "surveys", "survey_versions", "responses"):
        assert after[name] == before[name]
    assert await snapshot_tool.business_fingerprints(context.db, VALIDATORS) == after


async def test_first_stage_collection_selection_remains_available(context):
    old = ("users", "surveys", "survey_versions", "responses")
    result = await snapshot_tool.business_fingerprints(context.db, old)
    assert set(result) == set(old)
    assert all(row["count"] == 0 for row in result.values())
