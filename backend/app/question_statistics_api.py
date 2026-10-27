from typing import Literal

from fastapi import Depends, Header, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field


class ScopeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    questionVersionId: str | None = Field(None, min_length=1, max_length=160)
    respondentType: Literal['all','anonymous','named'] = 'all'


class StatisticsGrantRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    active: bool
    expectedRevision: int = Field(ge=0, le=9007199254740991)


def install_statistics_routes(api, current_user, service):
    @api.post('/questions/{question_id}/statistics/scopes', status_code=201, responses={200:{'description':'Scope replay'}})
    async def create_scope(question_id: str, body: ScopeRequest, user=Depends(current_user), svc=Depends(service),
                           idempotency_key: str = Header(min_length=1, max_length=128)):
        result,replay = await svc.create_scope(user,question_id,body.questionVersionId,body.respondentType,idempotency_key)
        return JSONResponse(content=jsonable_encoder(result), status_code=200 if replay else 201,
                            headers={'Idempotent-Replay':str(replay).lower()})

    @api.get('/questions/{question_id}/statistics')
    async def statistics(question_id: str, scopeId: str=Query(min_length=1,max_length=160),
                         afterVersionNo: int=Query(0,ge=0,le=9007199254740991),limit: int=Query(10,ge=1,le=20),
                         user=Depends(current_user),svc=Depends(service)):
        return await svc.summary(user,question_id,scopeId,afterVersionNo,limit)

    @api.get('/questions/{question_id}/statistics/sources')
    async def sources(question_id: str,scopeId: str=Query(min_length=1,max_length=160),
                       afterId: str|None=Query(None,min_length=1,max_length=160),limit: int=Query(30,ge=1,le=100),
                       user=Depends(current_user),svc=Depends(service)):
        return await svc.source_page(user,question_id,scopeId,afterId,limit)

    @api.get('/questions/{question_id}/statistics/answers')
    async def answers(question_id: str,scopeId: str=Query(min_length=1,max_length=160),
                       questionVersionId: str|None=Query(None,min_length=1,max_length=160),
                       cursor: str|None=Query(None,min_length=1,max_length=3000),limit: int=Query(30,ge=1,le=100),
                       user=Depends(current_user),svc=Depends(service)):
        return await svc.answers(user,question_id,scopeId,questionVersionId,cursor,limit)

    @api.get('/questions/{question_id}/statistics/export')
    async def export(question_id: str,scopeId: str=Query(min_length=1,max_length=160),
                      questionVersionId: str|None=Query(None,min_length=1,max_length=160),
                      user=Depends(current_user),svc=Depends(service)):
        content=await svc.export(user,question_id,scopeId,questionVersionId)
        return Response(content=content,media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition':'attachment; filename="question-statistics.csv"'})

    @api.get('/surveys/{survey_id}/question-statistics-grants')
    async def grant_candidates(survey_id: str,user=Depends(current_user),svc=Depends(service)):
        return await svc.grant_candidates(user,survey_id)

    @api.put('/surveys/{survey_id}/question-statistics-grants/{question_id}')
    async def set_grant(survey_id: str,question_id: str,body: StatisticsGrantRequest,
                        user=Depends(current_user),svc=Depends(service)):
        result,replay=await svc.set_grant(user,survey_id,question_id,body.active,body.expectedRevision)
        return JSONResponse(content=jsonable_encoder(result),
                            headers={'Idempotent-Replay':str(replay).lower()})
