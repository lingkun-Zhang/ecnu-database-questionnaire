"""Question statistics over persistent, short-lived published-source ranges."""
from collections import defaultdict
from copy import deepcopy
from datetime import timedelta
import csv
import io
from uuid import uuid4

import jwt
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from .errors import ServiceError
from .question_content import canonical_question_content
from .question_sharing import grant_filter
from .repositories import Repository
from .security import decode_token, digest, token, utcnow


def scope_view(row):
    return {'id': row['_id'], **{key: row[key] for key in
        ('questionId', 'questionVersionId', 'respondentType', 'createdAt', 'expiresAt',
         'publicationCount', 'surveyCount', 'instanceCount', 'hasForeignSources')}}


def grant_view(row, username=None):
    return {'id': row['_id'], **{key: row[key] for key in
        ('surveyId', 'questionId', 'active', 'revision', 'permission', 'createdAt', 'updatedAt')},
        'recipientUsername': username}


class QuestionStatisticsServices:
    def __init__(self, db, settings, clock=utcnow, hooks=None):
        self.db, self.settings, self.clock, self.hooks = db, settings, clock, hooks or {}
        self.repo = Repository(db, settings.transaction_seconds)

    async def hook(self, name, **state):
        if self.settings.testing and name in self.hooks:
            await self.hooks[name](**state)

    async def owner_survey(self, user, sid, session=None):
        row = await self.db.surveys.find_one({'_id': sid, 'ownerId': user['_id']}, session=session)
        if not row:
            raise ServiceError('NOT_FOUND', '问卷不存在', 404)
        return row

    async def grant_candidates(self, user, sid):
        await self.owner_survey(user, sid)
        rows = await self.db.survey_versions.find({'surveyId': sid},
            {'questionSources': 1, 'definition.questions': 1}).to_list(length=None)
        qids = {item['questionId'] for row in rows for item in row.get('questionSources', [])}
        result = []
        for qid in sorted(qids):
            question = await self.db.questions.find_one({'_id': qid})
            if not question or question['ownerId'] == user['_id']:
                continue
            author = await self.db.users.find_one({'_id': question['ownerId']}, {'username': 1})
            grant = await self.db.question_stat_grants.find_one({'surveyId': sid, 'questionId': qid})
            title = next((q['title'] for row in rows for source in row.get('questionSources', [])
                if source['questionId'] == qid for q in row['definition']['questions'] if q['id'] == source['instanceId']), '已发布题目')
            result.append({'questionId': qid, 'questionTitle': title,
                'recipientUsername': author['username'] if author else None,
                'grant': grant_view(grant, author['username'] if author else None) if grant else None})
        return {'items': result}

    async def set_grant(self, user, sid, qid, active, revision):
        await self.owner_survey(user, sid)
        question = await self.db.questions.find_one({'_id': qid})
        used = await self.db.survey_versions.find_one({'surveyId': sid,
            'questionSources': {'$elemMatch': {'questionId': qid}}}, {'_id': 1})
        if not question or not used:
            raise ServiceError('NOT_FOUND', '本问卷没有发布过这道题', 404)
        if question['ownerId'] == user['_id']:
            raise ServiceError('INVALID_STATISTICS_GRANT', '自己的问卷可直接统计，无需向自己授权', 422)
        author = await self.db.users.find_one({'_id': question['ownerId']}, {'username': 1})
        query = {'surveyId': sid, 'questionId': qid, 'ownerId': user['_id']}
        request_digest = digest({'operation': 'question_statistics_grant', **query,
                                 'active': active, 'expectedRevision': revision})

        async def recover():
            row = await self.db.question_stat_grants.find_one(query)
            if row and row['requestDigest'] == request_digest:
                return grant_view(row, author['username'] if author else None), True

        previous = await recover()
        if previous:
            return previous

        async def operation(session):
            await self.owner_survey(user, sid, session)
            row = await self.db.question_stat_grants.find_one(query, session=session)
            if row and row['requestDigest'] == request_digest:
                return grant_view(row, author['username'] if author else None), True
            if row and row['revision'] != revision or not row and revision != 0:
                raise ServiceError('STATISTICS_GRANT_CONFLICT', '统计授权已变化，请重新核对')
            now = self.clock()
            if row:
                row = await self.db.question_stat_grants.find_one_and_update(
                    {'_id': row['_id'], 'revision': revision}, {'$set': {'active': active,
                    'requestDigest': request_digest, 'updatedAt': now}, '$inc': {'revision': 1}},
                    return_document=ReturnDocument.AFTER, session=session)
            else:
                if not active:
                    raise ServiceError('STATISTICS_GRANT_NOT_FOUND', '尚未授权统计', 404)
                row = {'_id': str(uuid4()), **query, 'recipientId': question['ownerId'], 'active': True,
                    'revision': 1, 'permission': 'aggregate', 'requestDigest': request_digest,
                    'createdAt': now, 'updatedAt': now, 'schemaVersion': 1}
                await self.db.question_stat_grants.insert_one(row, session=session)
            await self.hook('after_question_stats_grant_write', document=row, session=session)
            return grant_view(row, author['username'] if author else None), False

        return await self.write(operation, recover, 'STATISTICS_GRANT_WRITE_OUTCOME_UNKNOWN')

    async def create_scope(self, user, qid, version_id, respondent_type, key):
        request_digest = digest({'questionId': qid, 'questionVersionId': version_id, 'respondentType': respondent_type})

        async def recover():
            row = await self.db.question_stat_scopes.find_one({'viewerId': user['_id'], 'creationKey': key})
            if row:
                if row['requestDigest'] != request_digest:
                    raise ServiceError('IDEMPOTENCY_CONFLICT', '该标识已用于不同统计筛选')
                if row['expiresAt'] <= self.clock():
                    raise ServiceError('STATISTICS_SCOPE_EXPIRED', '统计范围已过期，请明确刷新', 410)
                return scope_view(row), True

        previous = await recover()
        if previous:
            return previous
        scope_id = str(uuid4())

        async def operation(session):
            question = await self.db.questions.find_one({'_id': qid}, session=session)
            if not question:
                raise ServiceError('NOT_FOUND', '题目不存在', 404)
            grants = await self.db.question_stat_grants.find({'questionId': qid, 'recipientId': user['_id'],
                'active': True, 'permission': 'aggregate'}, session=session).to_list(length=None)
            by_survey = {row['surveyId']: row for row in grants}
            pipeline = [{'$match': {'questionSources': {'$elemMatch': {'questionId': qid}}}},
                {'$lookup': {'from': 'surveys', 'localField': 'surveyId', 'foreignField': '_id', 'as': 'survey'}},
                {'$unwind': '$survey'}, {'$match': {'$or': [{'survey.ownerId': user['_id']},
                    {'surveyId': {'$in': list(by_survey)}}]}}, {'$sort': {'_id': 1}}]
            pubs = await (await self.db.survey_versions.aggregate(pipeline, session=session)).to_list(length=None)
            own_publication = any(row['survey']['ownerId'] == user['_id'] for row in pubs)
            live_share = await self.db.question_shares.find_one(grant_filter(question, user['_id']), {'_id': 1}, session=session)
            if question['ownerId'] != user['_id'] and not own_publication and not live_share and not grants:
                raise ServiceError('NOT_FOUND', '题目不存在或没有统计来源权限', 404)
            if version_id and not await self.db.question_versions.find_one({'_id': version_id, 'questionId': qid}, session=session):
                raise ServiceError('QUESTION_VERSION_NOT_FOUND', '题目版本不存在', 404)
            now = self.clock()
            scope = {'_id': scope_id, 'viewerId': user['_id'], 'questionId': qid,
                'questionVersionId': version_id, 'respondentType': respondent_type, 'createdAt': now,
                'expiresAt': now + timedelta(minutes=30), 'purgeAt': now + timedelta(days=7),
                'creationKey': key, 'requestDigest': request_digest, 'publicationCount': 0,
                'surveyCount': 0, 'instanceCount': 0, 'hasForeignSources': False, 'schemaVersion': 1}
            seen = set()
            children = []
            for pub in pubs:
                own = pub['survey']['ownerId'] == user['_id']
                grant = by_survey.get(pub['surveyId'])
                if not own and (not grant or grant['ownerId'] != pub['survey']['ownerId']):
                    continue
                mappings = [x for x in pub.get('questionSources', []) if x['questionId'] == qid
                            and (not version_id or x['questionVersionId'] == version_id)]
                if not mappings:
                    continue
                await self.publication_versions(scope, pub, session)
                child = {'_id': str(uuid4()), 'scopeId': scope_id, 'surveyId': pub['surveyId'],
                    'surveyVersionId': pub['_id'], 'asOfSeq': pub['survey']['submitSequence'],
                    'accessMode': 'owner' if own else 'aggregate', 'expiresAt': scope['expiresAt'], 'schemaVersion': 1}
                if not own:
                    child.update(grantId=grant['_id'], grantRevision=grant['revision'])
                children.append(child)
                seen.add(pub['surveyId'])
                scope['instanceCount'] += len(mappings)
                scope['hasForeignSources'] |= not own
            scope['publicationCount'], scope['surveyCount'] = len(children), len(seen)
            await self.hook('after_question_stats_capture', scope=scope, session=session)
            await self.db.question_stat_scopes.insert_one(scope, session=session)
            for offset in range(0, len(children), 100):
                await self.db.question_stat_sources.insert_many(children[offset:offset+100], session=session)
            await self.hook('after_question_stats_scope_write', scope=scope, session=session)
            return scope_view(scope), False

        return await self.write(operation, recover, 'STATISTICS_SCOPE_WRITE_OUTCOME_UNKNOWN')

    async def publication_versions(self, scope, pub, session):
        if not pub:
            raise ServiceError('DATA_INVARIANT', '固定发布快照缺失', 500)
        questions = {x['id']: x for x in pub['definition']['questions']}
        groups, versions, seen = defaultdict(list), {}, set()
        for source in pub.get('questionSources', []):
            if source['questionId'] != scope['questionId'] or scope['questionVersionId'] and source['questionVersionId'] != scope['questionVersionId']:
                continue
            qvid, iid = source['questionVersionId'], source['instanceId']
            row = await self.db.question_versions.find_one({'_id': qvid, 'questionId': scope['questionId']}, session=session)
            local = questions.get(iid)
            if not row or not local or iid in seen or canonical_question_content({k: local[k] for k in ('kind','title','config')}) != row['content']:
                raise ServiceError('DATA_INVARIANT', '发布题目来源与内容不一致', 500)
            seen.add(iid); groups[qvid].append(iid); versions[qvid] = row
        return dict(groups), versions

    async def validate_scope(self, user, qid, scope_id, session):
        scope = await self.db.question_stat_scopes.find_one({'_id': scope_id}, session=session)
        if not scope:
            raise ServiceError('STATISTICS_SCOPE_EXPIRED', '统计范围不存在或已过期，请刷新', 410)
        if scope['viewerId'] != user['_id'] or scope['questionId'] != qid:
            raise ServiceError('NOT_FOUND', '统计范围不存在', 404)
        if scope['expiresAt'] <= self.clock():
            raise ServiceError('STATISTICS_SCOPE_EXPIRED', '统计范围已过期，请刷新', 410)
        sources = await self.db.question_stat_sources.find({'scopeId': scope_id}, session=session).sort('_id', 1).to_list(length=None)
        if len(sources) != scope['publicationCount']:
            raise ServiceError('STATISTICS_SCOPE_INVALIDATED', '统计范围已失效，请刷新')
        for source in sources:
            survey = await self.db.surveys.find_one({'_id': source['surveyId']}, {'ownerId': 1}, session=session)
            valid = survey and survey['ownerId'] == user['_id'] if source['accessMode'] == 'owner' else False
            if source['accessMode'] == 'aggregate' and survey:
                valid = await self.db.question_stat_grants.find_one({'_id': source['grantId'],
                    'surveyId': source['surveyId'], 'questionId': qid, 'ownerId': survey['ownerId'],
                    'recipientId': user['_id'], 'revision': source['grantRevision'], 'active': True,
                    'permission': 'aggregate'}, {'_id': 1}, session=session)
            if not valid:
                raise ServiceError('STATISTICS_SCOPE_INVALIDATED', '统计授权或问卷归属已变化，请重新获取范围')
        return scope, sources

    @staticmethod
    def response_query(scope, source):
        query = {'surveyId': source['surveyId'], 'versionId': source['surveyVersionId'], 'acceptedSeq': {'$lte': source['asOfSeq']}}
        if scope['respondentType'] != 'all':
            query['anonymous'] = scope['respondentType'] == 'anonymous'
        return query

    async def summary(self, user, qid, scope_id, after=0, limit=10):
        async def operation(session):
            scope, sources = await self.validate_scope(user, qid, scope_id, session)
            accum = {}
            overview = dict(surveyCount=scope['surveyCount'], publicationCount=len(sources),
                instanceCount=scope['instanceCount'], responseCount=0, answeredResponseCount=0, anonymousResponses=0, namedResponses=0)
            contexts = []
            for source in sources:
                pub = await self.db.survey_versions.find_one({'_id': source['surveyVersionId'], 'surveyId': source['surveyId']}, session=session)
                if not pub:
                    raise ServiceError('DATA_INVARIANT', '固定发布快照缺失', 500)
                groups, versions = await self.publication_versions(scope, pub, session)
                contexts.append((source, groups))
                all_ids = [iid for ids in groups.values() for iid in ids]
                facets = {'overview': [{'$group': {'_id': None, 'responseCount': {'$sum': 1},
                    'answeredResponseCount': {'$sum': {'$cond': [{'$gt': [{'$size': {'$setIntersection': ['$answers.questionId', {'$literal': all_ids}]}}, 0]}, 1, 0]}},
                    'anonymousResponses': {'$sum': {'$cond': ['$anonymous', 1, 0]}},
                    'namedResponses': {'$sum': {'$cond': ['$anonymous', 0, 1]}}}}]}
                for index, (vid, ids) in enumerate(groups.items()):
                    literal = {'$literal': ids}
                    reached = {'$setIntersection': [literal, '$path']}
                    answered = {'$setIntersection': [literal, '$answers.questionId']}
                    facets[f'c{index}'] = [{'$group': {'_id': None,
                        'R': {'$sum': len(ids)}, 'V': {'$sum': {'$size': reached}}, 'A': {'$sum': {'$size': answered}},
                        'U': {'$sum': {'$size': {'$setDifference': [reached, '$answers.questionId']}}},
                        'S': {'$sum': {'$size': {'$setDifference': [literal, '$path']}}},
                        'responseCount': {'$sum': 1}, 'answeredResponseCount': {'$sum': {'$cond': [{'$gt': [{'$size': answered}, 0]}, 1, 0]}},
                        'anonymousResponses': {'$sum': {'$cond': ['$anonymous', 1, 0]}},
                        'namedResponses': {'$sum': {'$cond': ['$anonymous', 0, 1]}}}}]
                    answer_rows = [{'$unwind': '$answers'}, {'$match': {'answers.questionId': {'$in': ids}}}]
                    kind = versions[vid]['content']['kind']
                    if kind == 'number':
                        facets[f'v{index}'] = answer_rows + [{'$match': {'answers.kind': 'number'}}, {'$group': {'_id': None,
                            'count': {'$sum': 1}, 'sum': {'$sum': '$answers.value'}, 'min': {'$min': '$answers.value'}, 'max': {'$max': '$answers.value'}}}]
                    elif kind in ('single', 'multi'):
                        facets[f'v{index}'] = answer_rows + [{'$project': {'values': {'$cond': [
                            {'$eq': ['$answers.kind', 'multi']}, '$answers.value', ['$answers.value']]}}},
                            {'$unwind': '$values'}, {'$group': {'_id': '$values', 'count': {'$sum': 1}}}]
                    if vid not in accum:
                        content = versions[vid]['content']
                        accum[vid] = {'questionVersionId': vid, 'versionNo': versions[vid]['versionNo'], **deepcopy(content),
                            **{name: 0 for name in ('R','V','A','U','S','responseCount','answeredResponseCount','anonymousResponses','namedResponses','publicationCount','instanceCount')},
                            '_surveys': set(), '_options': defaultdict(int), '_numericCount': 0, 'sum': 0, 'min': None, 'max': None}
                    accum[vid]['_surveys'].add(source['surveyId']); accum[vid]['publicationCount'] += 1
                    accum[vid]['instanceCount'] += len(ids)
                rows = await (await self.db.responses.aggregate([{'$match': self.response_query(scope, source)}, {'$facet': facets}], session=session, maxTimeMS=30000)).to_list(length=1)
                data = rows[0]
                for key, value in next(iter(data['overview']), {}).items():
                    if key != '_id': overview[key] += value
                for index, vid in enumerate(groups):
                    row = accum[vid]
                    for key, value in next(iter(data[f'c{index}']), {}).items():
                        if key != '_id': row[key] += value
                    values = data.get(f'v{index}', [])
                    if row['kind'] == 'number' and values:
                        value = values[0]; row['sum'] += value['sum']; row['_numericCount'] += value['count']
                        row['min'] = value['min'] if row['min'] is None else min(row['min'], value['min'])
                        row['max'] = value['max'] if row['max'] is None else max(row['max'], value['max'])
                    elif row['kind'] in ('single', 'multi'):
                        for value in values: row['_options'][value['_id']] += value['count']
            ordered = sorted(accum.values(), key=lambda row: row['versionNo'])
            page = [row for row in ordered if row['versionNo'] > after][:limit+1]
            result = []
            for original in page[:limit]:
                row = dict(original); row['surveyCount'] = len(row.pop('_surveys'))
                options = row.pop('_options'); numeric_count = row.pop('_numericCount')
                if not 0 <= row['A'] <= row['V'] <= row['R'] or row['R'] != row['A'] + row['U'] + row['S']:
                    raise ServiceError('DATA_INVARIANT', '作答机会、路径与答案不一致', 500)
                if row['kind'] == 'number':
                    if numeric_count != row['A']: raise ServiceError('DATA_INVARIANT', '数字答案与有效作答数不一致', 500)
                    row['avg'] = row['sum']/row['A'] if row['A'] else None
                    row['histogram'] = await self.histogram(scope, sources, contexts, row, session)
                else:
                    for key in ('sum', 'min', 'max'): row.pop(key)
                    if row['kind'] in ('single', 'multi'):
                        ids = {option['id'] for option in row['config']['options']}
                        if set(options)-ids or any(value > row['A'] for value in options.values()):
                            raise ServiceError('DATA_INVARIANT', '选项答案不属于固定版本', 500)
                        row['options'] = [{**option, 'count': options[option['id']],
                            'percentage': options[option['id']]*100/row['A'] if row['A'] else 0} for option in row['config']['options']]
                        row['totalSelections'] = sum(options.values())
                        if row['kind']=='single' and row['totalSelections'] != row['A']:
                            raise ServiceError('DATA_INVARIANT', '单选分布不一致', 500)
                result.append(row)
            return {'scope': scope_view(scope), 'overview': overview, 'versions': result,
                    'nextAfterVersionNo': page[limit-1]['versionNo'] if len(page)>limit else None}
        return await self.repo.transaction(operation)

    async def histogram(self, scope, sources, contexts, row, session):
        if not row['A']: return []
        low, high = row['min'], row['max']
        edges = [low]
        if high != low:
            for i in range(1,10):
                value = low + (high-low)*i/10
                if edges[-1] < value < high: edges.append(value)
            edges.append(high)
        counts = defaultdict(int)
        bucket = 0 if len(edges)<=2 else {'$switch': {'branches': [
            {'case': {'$lt': ['$answers.value', edge]}, 'then': i} for i,edge in enumerate(edges[1:-1])],
            'default': len(edges)-2}}
        for source, groups in contexts:
            ids = groups.get(row['questionVersionId'])
            if not ids: continue
            pipeline = [{'$match': self.response_query(scope, source)}, {'$unwind': '$answers'},
                {'$match': {'answers.questionId': {'$in': ids}, 'answers.kind': 'number'}},
                {'$group': {'_id': bucket, 'count': {'$sum': 1}}}]
            values = await (await self.db.responses.aggregate(pipeline, session=session, maxTimeMS=30000)).to_list(length=10)
            for value in values: counts[int(value['_id'])] += value['count']
        if sum(counts.values()) != row['A']: raise ServiceError('DATA_INVARIANT', '数字分布与有效作答数不一致', 500)
        if high==low: return [{'from':low,'to':high,'includeTo':True,'count':counts[0]}]
        return [{'from':edges[i],'to':edges[i+1],'includeTo':i==len(edges)-2,'count':counts[i]} for i in range(len(edges)-1)]

    async def source_page(self, user, qid, scope_id, after=None, limit=30):
        async def operation(session):
            scope, sources = await self.validate_scope(user,qid,scope_id,session)
            rows = [source for source in sources if not after or source['_id']>after][:limit+1]
            items=[]
            for source in rows[:limit]:
                pub=await self.db.survey_versions.find_one({'_id':source['surveyVersionId']},session=session)
                groups,_=await self.publication_versions(scope,pub,session)
                item={'id':source['_id'],'surveyTitle':pub['definition']['title'] if source['accessMode']=='owner' else '已获授权的其他问卷',
                    'surveyVersionNo':pub['versionNo'],'asOfSeq':source['asOfSeq'],'instanceCount':sum(map(len,groups.values())),
                    'accessMode':source['accessMode'],'canReadAnswers':source['accessMode']=='owner'}
                if source['accessMode']=='owner': item.update(surveyId=source['surveyId'],surveyVersionId=source['surveyVersionId'])
                items.append(item)
            return {'scope':scope_view(scope),'items':items,'nextAfterId':rows[limit-1]['_id'] if len(rows)>limit else None}
        return await self.repo.transaction(operation)

    def decode_cursor(self,user,scope_id,version_id,cursor):
        if not cursor: return None
        try:
            data=decode_token(cursor,self.settings.auth_secret,'question-stat-cursor')
            if (data['sub']!=user['_id'] or data['scopeId']!=scope_id or data.get('questionVersionId')!=version_id
                or type(data['acceptedSeq']) is not int or data['acceptedSeq']<1 or not isinstance(data['sourceId'],str)
                or not isinstance(data['instanceId'],str)): raise ValueError()
            return data
        except (jwt.PyJWTError,ValueError,KeyError,TypeError) as exc:
            raise ServiceError('INVALID_STATISTICS_CURSOR','分页位置与当前范围或筛选不一致',422) from exc

    async def raw_rows(self,scope,sources,session,version_id=None,cursor=None,limit=31):
        items=[]
        if cursor and not any(source['_id']==cursor['sourceId'] and source['accessMode']=='owner' for source in sources):
            raise ServiceError('INVALID_STATISTICS_CURSOR','分页来源不属于当前范围',422)
        for source in sources:
            if source['accessMode']!='owner' or cursor and source['_id']<cursor['sourceId']: continue
            pub=await self.db.survey_versions.find_one({'_id':source['surveyVersionId']},session=session)
            groups,versions=await self.publication_versions(scope,pub,session)
            mapping={iid:vid for vid,ids in groups.items() if not version_id or vid==version_id for iid in ids}
            pipeline=[{'$match':self.response_query(scope,source)},{'$unwind':'$answers'},
                {'$match':{'answers.questionId':{'$in':list(mapping)}}}]
            if cursor and source['_id']==cursor['sourceId']:
                pipeline.append({'$match':{'$or':[{'acceptedSeq':{'$gt':cursor['acceptedSeq']}},
                    {'acceptedSeq':cursor['acceptedSeq'],'answers.questionId':{'$gt':cursor['instanceId']}}]}})
            pipeline += [{'$sort':{'acceptedSeq':1,'answers.questionId':1}},{'$limit':limit-len(items)},
                {'$project':{'_id':1,'acceptedSeq':1,'acceptedAt':1,'anonymous':1,'answers':1}}]
            rows=await (await self.db.responses.aggregate(pipeline,session=session,maxTimeMS=30000)).to_list(length=limit-len(items))
            for row in rows:
                answer=row['answers'];vid=mapping[answer['questionId']]
                items.append({'surveyId':source['surveyId'],'surveyTitle':pub['definition']['title'],
                    'surveyVersionId':pub['_id'],'surveyVersionNo':pub['versionNo'],'instanceId':answer['questionId'],
                    'questionVersionId':vid,'questionVersionNo':versions[vid]['versionNo'],'responseId':row['_id'],
                    'acceptedSeq':row['acceptedSeq'],'acceptedAt':row['acceptedAt'],'anonymous':row['anonymous'],
                    'kind':answer['kind'],'value':answer['value'],'_sourceId':source['_id']})
            if len(items)>=limit: break
        return items

    async def answers(self,user,qid,scope_id,version_id=None,cursor=None,limit=30):
        location=self.decode_cursor(user,scope_id,version_id,cursor)
        async def operation(session):
            scope,sources=await self.validate_scope(user,qid,scope_id,session)
            if scope['questionVersionId'] and version_id and version_id!=scope['questionVersionId']:
                raise ServiceError('INVALID_STATISTICS_CURSOR','版本筛选超出当前范围',422)
            if version_id and not await self.db.question_versions.find_one({'_id':version_id,'questionId':qid},{'_id':1},session=session):
                raise ServiceError('QUESTION_VERSION_NOT_FOUND','题目版本不存在',404)
            rows=await self.raw_rows(scope,sources,session,version_id,location,limit+1)
            next_cursor=None
            if len(rows)>limit:
                last=rows[limit-1]
                next_cursor=token(self.settings.auth_secret,'question-stat-cursor',user['_id'],3600,
                    scopeId=scope_id,questionVersionId=version_id,sourceId=last['_sourceId'],
                    acceptedSeq=last['acceptedSeq'],instanceId=last['instanceId'])[0]
            return {'scope':scope_view(scope),'items':[{k:v for k,v in row.items() if k!='_sourceId'} for row in rows[:limit]],
                'nextCursor':next_cursor,'coverage':{'ownOnly':True,'hasForeignAggregateSources':scope['hasForeignSources']}}
        return await self.repo.transaction(operation)

    async def export(self,user,qid,scope_id,version_id=None):
        async def operation(session):
            scope,sources=await self.validate_scope(user,qid,scope_id,session)
            if scope['questionVersionId'] and version_id and version_id!=scope['questionVersionId']:
                raise ServiceError('INVALID_STATISTICS_CURSOR','版本筛选超出当前范围',422)
            if version_id and not await self.db.question_versions.find_one({'_id':version_id,'questionId':qid},{'_id':1},session=session):
                raise ServiceError('QUESTION_VERSION_NOT_FOUND','题目版本不存在',404)
            buffer=io.StringIO();writer=csv.writer(buffer)
            writer.writerow(['问卷','问卷版本','题目版本','卷内题目标识','回执','提交序号','提交时间UTC','匿名','答案'])
            size=3+len(buffer.getvalue().encode('utf-8'));count=0;cursor=None;pieces=[buffer.getvalue()]
            while True:
                rows=await self.raw_rows(scope,sources,session,version_id,cursor,100)
                for row in rows:
                    count+=1
                    value=row['value'] if not isinstance(row['value'],list) else '；'.join(map(str,row['value']))
                    def cell(raw):
                        text=str(raw)
                        return "'"+text if text.lstrip().startswith(('=','+','-','@')) else text
                    buffer.seek(0);buffer.truncate(0)
                    writer.writerow([cell(row['surveyTitle']),row['surveyVersionNo'],row['questionVersionNo'],row['instanceId'],
                        row['responseId'],row['acceptedSeq'],row['acceptedAt'].isoformat(),'是' if row['anonymous'] else '否',cell(value)])
                    fragment=buffer.getvalue();size+=len(fragment.encode('utf-8'))
                    if count>20000 or size>8*1024*1024:
                        raise ServiceError('EXPORT_TOO_LARGE','导出最多20000条作答且不超过8MiB，请按版本缩小范围',422)
                    pieces.append(fragment)
                if len(rows)<100: break
                last=rows[-1];cursor={'sourceId':last['_sourceId'],'acceptedSeq':last['acceptedSeq'],'instanceId':last['instanceId']}
            return '\ufeff'+''.join(pieces)
        return await self.repo.transaction(operation)

    async def write(self,operation,recover,code):
        try: return await self.repo.transaction(operation)
        except (ServiceError,PyMongoError) as error:
            try: recovered=await recover()
            except PyMongoError as exc: raise ServiceError(code,'暂时无法确认保存结果，请保留原请求重试',503) from exc
            if recovered: return recovered
            if isinstance(error,PyMongoError): raise ServiceError(code,'暂时无法确认保存结果，请保留原请求重试',503) from error
            raise
