"""MongoDB access boundary; services own the transaction and business decisions."""
from pymongo import ReturnDocument, ReadPreference
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern
import pymongo


class Repository:
    def __init__(self, db, transaction_seconds=20):
        self.db = db
        self.transaction_seconds = transaction_seconds

    async def transaction(self, callback):
        # CSOT bounds network work while with_transaction retains driver commit semantics.
        with pymongo.timeout(self.transaction_seconds):
            async with self.db.client.start_session() as session:
                return await session.with_transaction(
                    callback, read_concern=ReadConcern("snapshot"),
                    write_concern=WriteConcern("majority"), read_preference=ReadPreference.PRIMARY,
                    max_commit_time_ms=5000,
                )

    async def owner_survey(self, survey_id, owner_id, session=None):
        return await self.db.surveys.find_one({"_id": survey_id, "ownerId": owner_id}, session=session)

    async def public_survey(self, public_id):
        return await self.db.surveys.find_one({"publicId": public_id})

    async def version(self, survey_id, version_id, session=None):
        return await self.db.survey_versions.find_one(
            {"_id": version_id, "surveyId": survey_id}, session=session)

    async def prior_submission(self, survey_id, participant, key):
        return await self.db.responses.find_one(
            {"surveyId": survey_id, "participantKey": participant, "submissionKey": key})

    async def prior_publish(self, survey_id, key):
        return await self.db.survey_versions.find_one({"surveyId": survey_id, "publishKey": key})

    async def save_draft(self, survey_id, owner_id, revision, definition, now):
        return await self.db.surveys.find_one_and_update(
            {"_id": survey_id, "ownerId": owner_id, "draftRevision": revision},
            {"$set": {"draft": definition, "updatedAt": now}, "$inc": {"draftRevision": 1}},
            return_document=ReturnDocument.AFTER,
        )

    async def submission_gate(self, survey_id, now, session):
        return await self.db.surveys.find_one_and_update(
            {"_id": survey_id, "status": "open"},
            {"$inc": {"submitSequence": 1}, "$set": {"policyLocked": True, "updatedAt": now}},
            return_document=ReturnDocument.AFTER, session=session,
        )

    async def insert_response(self, document, session):
        return await self.db.responses.insert_one(document, session=session)

    async def insert_version(self, document, session):
        return await self.db.survey_versions.insert_one(document, session=session)

    async def switch_version(self, survey, owner_id, revision, expected_current, version_id, number, now, session):
        return await self.db.surveys.update_one(
            {"_id": survey["_id"], "ownerId": owner_id, "draftRevision": revision,
             "currentVersionId": expected_current},
            {"$set": {"currentVersionId": version_id, "lastVersionNo": number,
                      "status": "open" if survey["status"] == "draft" else survey["status"],
                      "updatedAt": now}}, session=session,
        )
