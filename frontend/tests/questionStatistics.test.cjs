const assert = require('node:assert/strict')
const {readFileSync}=require('node:fs')
const {resolve}=require('node:path')
const {test:nodeTest}=require('node:test')
const {webcrypto}=require('node:crypto')
const vm=require('node:vm')
const ts=require('typescript')
const {parse}=require('@vue/compiler-sfc')
const vue=require('vue')
const test=(name,action)=>nodeTest(name,{timeout:5000},action)
const plain=value=>JSON.parse(JSON.stringify(value))
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}}
class ApiError extends Error{constructor(status,code,message=code){super(message);this.status=status;this.code=code;this.fieldErrors=[]}}
const url=call=>new URL('http://test'+call.path)
const writes=h=>h.calls.filter(c=>c.method==='POST')
const reads=h=>h.calls.filter(c=>c.method==='GET')
async function settle(){for(let i=0;i<3;i++){await vue.nextTick();await new Promise(setImmediate)}}
function component(questionId='q-a'){
  const filename=resolve(__dirname,'../src/components/QuestionStatistics.vue')
  const source=parse(readFileSync(filename,'utf8')).descriptor.scriptSetup.content
  const exposed='busy,failure,note,scopeInvalid,scope,overview,versionRows,knownVersions,afterVersionNo,versionPrevious,nextAfterVersionNo,sourceRows,sourceNext,answerRows,answerNext,filterQuestionVersion,filterRespondentType,answerVersion,displayedAnswerVersion,answersLoaded,sourcesLoaded,coverage,pendingScope,filtersChanged,detailFilterChanged,availableAnswerVersions,refreshScope,retryScope,abandonPending,loadNextVersions,loadPreviousVersions,loadSources,loadAnswers,exportCSV,answerText,canLeave,hasUnsavedChanges,back'
  const calls=[],events=[],confirmations=[],unmounted=[],props=vue.reactive({questionId}),effect=vue.effectScope()
  let token='account-A',confirmResult=true,disposed=false
  const network={request:async call=>{throw new Error(`Unexpected request: ${call.method} ${call.path}`)}}
  const api={ApiError,getToken:()=>token,
    request:async(path,options={})=>{const call={path,method:options.method||'GET',body:options.body?JSON.parse(options.body):undefined,headers:Object.fromEntries(new Headers(options.headers))};calls.push(call);return network.request(call)},
    post:async(path,body,headers)=>{const call={path,method:'POST',body:plain(body),headers:Object.fromEntries(new Headers(headers))};calls.push(call);return network.request(call)},
    download:async(path,filename,isCurrent)=>{const call={path,filename,isCurrent,method:'DOWNLOAD'};calls.push(call);return network.request(call)},
  }
  const module={exports:{}}
  const compiled=ts.transpileModule(source+`\nmodule.exports={${exposed}}`,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
  effect.run(()=>vm.runInNewContext(compiled,{module,exports:module.exports,require:name=>{
    if(name==='vue')return {...vue,onUnmounted:fn=>unmounted.push(fn)}
    if(name==='../api')return api
    throw new Error(`Unexpected import: ${name}`)
  },defineProps:()=>props,defineEmits:()=> (name,...args)=>events.push({name,args:plain(args)}),defineExpose:()=>{},URLSearchParams,Headers,crypto:webcrypto,confirm:text=>{confirmations.push(text);return confirmResult}}))
  return {state:module.exports,network,calls,events,confirmations,props,setToken:value=>{token=value},setConfirm:value=>{confirmResult=value},dispose:()=>{if(!disposed){disposed=true;unmounted.forEach(fn=>fn());effect.stop()}}}
}
function row(number=1){return {questionVersionId:`q-a-v${number}`,versionNo:number,kind:'number',title:`数字V${number}`,config:{min:0,max:100},R:8,V:6,A:5,U:1,S:2,responseCount:5,answeredResponseCount:4,anonymousResponses:4,namedResponses:1,surveyCount:2,publicationCount:2,instanceCount:3,sum:100,avg:20,min:0,max:40,histogram:[{from:0,to:20,includeTo:false,count:2},{from:20,to:40,includeTo:true,count:3}]}}
function range(body={},id='range-A'){return {id,questionId:'q-a',questionVersionId:null,respondentType:'all',createdAt:'2026-10-27T01:00:00Z',expiresAt:'2026-10-27T01:30:00Z',publicationCount:2,surveyCount:2,instanceCount:3,hasForeignSources:true,...body}}
function fixture(h){
  const state={range:range(),versions:[row()],sources:[{id:'source-a',surveyTitle:'自己的问卷',surveyVersionNo:2,asOfSeq:3,instanceCount:2,accessMode:'owner',canReadAnswers:true},{id:'source-z',surveyTitle:'已获授权的其他问卷',surveyVersionNo:1,asOfSeq:2,instanceCount:1,accessMode:'aggregate',canReadAnswers:false}],answers:[{surveyTitle:'自己的问卷',surveyVersionNo:2,instanceId:'score',questionVersionId:'q-a-v1',questionVersionNo:1,responseId:'receipt-a',acceptedSeq:1,acceptedAt:'2026-10-27T00:00:00Z',anonymous:true,kind:'number',value:0}],nextVersion:null,sourceNext:null,answerNext:null}
  const respond=async call=>{
    const parsed=url(call),path=parsed.pathname
    if(call.method==='POST'){state.range=range(call.body,state.range.id);return plain(state.range)}
    if(call.method==='DOWNLOAD')return undefined
    assert.equal(parsed.searchParams.get('scopeId'),state.range.id)
    if(path.endsWith('/sources'))return {scope:plain(state.range),items:plain(state.sources),nextAfterId:state.sourceNext}
    if(path.endsWith('/answers'))return {scope:plain(state.range),items:plain(state.answers),nextCursor:state.answerNext,coverage:{ownOnly:true,hasForeignAggregateSources:true}}
    return {scope:plain(state.range),overview:{surveyCount:2,publicationCount:2,instanceCount:3,responseCount:5,answeredResponseCount:4,anonymousResponses:4,namedResponses:1},versions:plain(state.versions),nextAfterVersionNo:state.nextVersion}
  }
  h.network.request=respond
  return {state,respond}
}

// Execute the real setup with Vue reactivity and controlled HTTP; no DOM is
// mounted here. Browser acceptance remains a separate check.
test('opening the component does not create a statistical range without a click',()=>{
  const h=component();try{assert.equal(h.calls.length,0);assert.equal(h.state.scope.value,null);assert.equal(h.state.hasUnsavedChanges(),false)}finally{h.dispose()}
})
test('one explicit range supplies summary, sources, answers and fixed filter labels',async()=>{
  const h=component('q/a?b');fixture(h)
  try{
    h.state.filterRespondentType.value='anonymous';await h.state.refreshScope()
    assert.equal(writes(h).length,1);assert.equal(writes(h)[0].path,'/questions/q%2Fa%3Fb/statistics/scopes')
    assert.match(writes(h)[0].headers['idempotency-key'],/^[0-9a-f-]{36}$/)
    assert.deepEqual(writes(h)[0].body,{questionVersionId:null,respondentType:'anonymous'})
    assert.equal(reads(h).length,3);assert.ok(reads(h).every(c=>url(c).searchParams.get('scopeId')==='range-A'))
    assert.deepEqual(plain(h.state.versionRows.value[0]),row());assert.equal(h.state.answerRows.value[0].value,0)
    assert.equal(h.state.scope.value.respondentType,'anonymous');assert.equal(h.state.pendingScope.value,null)
    h.state.filterRespondentType.value='named';assert.equal(h.state.filtersChanged.value,true);assert.equal(h.state.scope.value.respondentType,'anonymous')
  }finally{h.dispose()}
})
test('uncertain creation retries the same key and frozen body despite locally changed filters',async()=>{
  const h=component(),server=fixture(h);let first=true
  h.network.request=async c=>{if(c.method==='POST'&&first){first=false;throw new ApiError(0,'NETWORK_ERROR')}return server.respond(c)}
  try{
    await h.state.refreshScope();const original=plain(writes(h)[0]);assert.equal(h.state.pendingScope.value.uncertain,true)
    h.state.filterRespondentType.value='named';await h.state.refreshScope();assert.equal(writes(h).length,1)
    await h.state.retryScope();assert.deepEqual(plain(writes(h)[1]),original);assert.equal(h.state.scope.value.respondentType,'all')
    assert.equal(h.state.filtersChanged.value,true);assert.equal(h.state.hasUnsavedChanges(),false)
  }finally{h.dispose()}
})
test('a committed range followed by summary failure retries GET only',async()=>{
  const h=component(),server=fixture(h);let fail=true
  h.network.request=async c=>{if(c.method==='GET'&&!c.path.includes('/sources')&&!c.path.includes('/answers')&&fail){fail=false;throw new ApiError(503,'TEMPORARY')}return server.respond(c)}
  try{
    await h.state.refreshScope();assert.equal(h.state.pendingScope.value.result.id,'range-A');assert.equal(h.state.overview.value,null)
    await h.state.retryScope();assert.equal(writes(h).length,1);assert.equal(reads(h).length,4);assert.equal(h.state.pendingScope.value,null)
  }finally{h.dispose()}
})
test('a later source or answer read failure retains the saved range for GET recovery',async()=>{
  const h=component(),server=fixture(h);let fail=true
  h.network.request=async c=>{if(c.path.includes('/answers?')&&fail){fail=false;throw new ApiError(0,'NETWORK_ERROR')}return server.respond(c)}
  try{await h.state.refreshScope();assert.ok(h.state.overview.value);assert.equal(h.state.answersLoaded.value,false);assert.ok(h.state.pendingScope.value.result);await h.state.retryScope();assert.equal(writes(h).length,1);assert.equal(h.state.answersLoaded.value,true)}finally{h.dispose()}
})
for(const [status,code]of [[410,'STATISTICS_SCOPE_EXPIRED'],[409,'STATISTICS_SCOPE_INVALIDATED']])test(`${code} clears all old range data without expiring the login`,async()=>{
  const h=component();fixture(h)
  try{
    await h.state.refreshScope();h.network.request=async()=>{throw new ApiError(status,code)}
    await h.state.loadSources();assert.equal(h.state.scope.value,null);assert.equal(h.state.overview.value,null)
    assert.deepEqual(plain(h.state.answerRows.value),[]);assert.deepEqual(plain(h.state.versionRows.value),[]);assert.deepEqual(plain(h.state.sourceRows.value),[])
    assert.equal(h.state.scopeInvalid.value,true);assert.equal(h.state.pendingScope.value,null);assert.equal(h.events.some(e=>e.name==='unauthorized'),false)
  }finally{h.dispose()}
})
test('401 clears visible data and tells the parent to expire the session',async()=>{
  const h=component();fixture(h)
  try{await h.state.refreshScope();h.network.request=async()=>{throw new ApiError(401,'UNAUTHORIZED')};await h.state.loadAnswers();assert.equal(h.state.scope.value,null);assert.deepEqual(plain(h.state.knownVersions.value),[]);assert.equal(h.events.filter(e=>e.name==='unauthorized').length,1)}finally{h.dispose()}
})
test('a range POST finishing after the account token changed cannot show its result',async()=>{
  const h=component(),wait=deferred();fixture(h);h.network.request=()=>wait.promise
  try{const action=h.state.refreshScope();h.setToken('account-B');wait.resolve(range());await action;assert.equal(h.state.scope.value,null);assert.equal(reads(h).length,0);assert.deepEqual(plain(h.state.answerRows.value),[]);assert.equal(h.events.some(e=>e.name==='unauthorized'),false)}finally{h.dispose()}
})
test('a question change clears old rows and ignores late range results',async()=>{
  const h=component(),wait=deferred();fixture(h);h.network.request=()=>wait.promise
  try{const action=h.state.refreshScope();h.props.questionId='q-b';await settle();wait.resolve(range());await action;assert.equal(h.state.pendingScope.value,null);assert.equal(h.state.scope.value,null);assert.equal(h.state.busy.value,false);assert.equal(reads(h).length,0)}finally{h.dispose()}
})
test('an unmounted component ignores a late raw-answer response',async()=>{
  const h=component(),server=fixture(h),wait=deferred();await h.state.refreshScope()
  h.network.request=c=>c.path.includes('/answers?')?wait.promise:server.respond(c)
  const action=h.state.loadAnswers();h.dispose();wait.resolve({items:[{value:'late private answer'}],nextCursor:null,coverage:{ownOnly:true}});await action
  assert.deepEqual(plain(h.state.answerRows.value),[]);assert.equal(h.state.scope.value,null)
})
test('version keyset paging replaces the page, preserves known versions, and goes back',async()=>{
  const h=component(),server=fixture(h);server.state.nextVersion=1
  try{
    await h.state.refreshScope();server.state.versions=[row(2)];server.state.nextVersion=null;await h.state.loadNextVersions()
    assert.equal(url(reads(h).at(-1)).searchParams.get('afterVersionNo'),'1');assert.deepEqual(plain(h.state.versionPrevious.value),[0]);assert.equal(h.state.versionRows.value[0].versionNo,2);assert.equal(h.state.knownVersions.value.length,2)
    server.state.versions=[row(1)];server.state.nextVersion=1;await h.state.loadPreviousVersions();assert.equal(url(reads(h).at(-1)).searchParams.get('afterVersionNo'),'0');assert.equal(h.state.versionRows.value[0].versionNo,1);assert.deepEqual(plain(h.state.versionPrevious.value),[]);assert.equal(writes(h).length,1)
  }finally{h.dispose()}
})
test('source paging sends the opaque cursor and appends authorized aggregate rows',async()=>{
  const h=component(),server=fixture(h);server.state.sourceNext='opaque%/source?cursor'
  try{await h.state.refreshScope();server.state.sources=[{...server.state.sources[1],id:'source-next'}];server.state.sourceNext=null;await h.state.loadSources(true);assert.equal(url(reads(h).at(-1)).searchParams.get('afterId'),'opaque%/source?cursor');assert.equal(h.state.sourceRows.value.length,3);assert.equal(h.state.sourceRows.value.at(-1).canReadAnswers,false);assert.equal(writes(h).length,1)}finally{h.dispose()}
})
test('raw pagination cannot reuse the old cursor after a version selection change',async()=>{
  const h=component(),server=fixture(h);server.state.answerNext='signed%/cursor?'
  try{
    await h.state.refreshScope();h.state.answerVersion.value='q-a-v2';assert.equal(h.state.detailFilterChanged.value,true)
    const count=h.calls.length;await h.state.loadAnswers(true);await h.state.exportCSV();assert.equal(h.calls.length,count)
    await h.state.loadAnswers();let last=url(reads(h).at(-1));assert.equal(last.searchParams.get('questionVersionId'),'q-a-v2');assert.equal(last.searchParams.has('cursor'),false);assert.equal(h.state.displayedAnswerVersion.value,'q-a-v2')
    await h.state.loadAnswers(true);last=url(reads(h).at(-1));assert.equal(last.searchParams.get('cursor'),'signed%/cursor?');assert.equal(last.searchParams.get('questionVersionId'),'q-a-v2');assert.equal(writes(h).length,1)
  }finally{h.dispose()}
})
test('CSV uses the displayed detail filter, same fixed scope, and a live account guard',async()=>{
  const h=component();fixture(h)
  try{h.state.filterQuestionVersion.value='q-a-v1';await h.state.refreshScope();await h.state.exportCSV();const call=h.calls.at(-1);assert.equal(call.method,'DOWNLOAD');assert.equal(url(call).searchParams.get('scopeId'),'range-A');assert.equal(url(call).searchParams.get('questionVersionId'),'q-a-v1');assert.equal(call.isCurrent(),true);h.setToken('account-B');assert.equal(call.isCurrent(),false)}finally{h.dispose()}
})
test('CSV grant invalidation removes prior summary and detail data',async()=>{
  const h=component(),server=fixture(h)
  try{await h.state.refreshScope();h.network.request=c=>{if(c.method==='DOWNLOAD')throw new ApiError(409,'STATISTICS_SCOPE_INVALIDATED');return server.respond(c)};await h.state.exportCSV();assert.equal(h.state.scope.value,null);assert.deepEqual(plain(h.state.answerRows.value),[]);assert.equal(h.state.scopeInvalid.value,true)}finally{h.dispose()}
})
test('a new refresh freezes changed filters under a fresh key and clears the preceding range before I/O',async()=>{
  const h=component(),server=fixture(h),wait=deferred()
  try{await h.state.refreshScope();const first=writes(h)[0].headers['idempotency-key'];h.state.filterRespondentType.value='named';server.state.range.id='range-B';h.network.request=c=>{if(c.method==='POST'){server.state.range=range(c.body,'range-B');return wait.promise}return server.respond(c)};const action=h.state.refreshScope();assert.equal(h.state.scope.value,null);assert.equal(h.state.overview.value,null);assert.deepEqual(plain(h.state.answerRows.value),[]);assert.notEqual(writes(h)[1].headers['idempotency-key'],first);assert.equal(writes(h)[1].body.respondentType,'named');wait.resolve(range({respondentType:'named'},'range-B'));await action;assert.equal(h.state.scope.value.id,'range-B');assert.equal(h.state.scope.value.respondentType,'named')}finally{h.dispose()}
})
test('an uncertain request older than 30 minutes cannot silently create another range',async()=>{
  const h=component();fixture(h);h.network.request=async()=>{throw new ApiError(503,'UNKNOWN')}
  try{await h.state.refreshScope();h.state.pendingScope.value.startedAt=Date.now()-31*60*1000;await h.state.retryScope();assert.equal(writes(h).length,1);assert.equal(h.state.failure.value.code,'LOCAL_SCOPE_RETRY_EXPIRED');assert.ok(h.state.pendingScope.value)}finally{h.dispose()}
})
test('busy work prevents leaving; uncertain requests require explicit confirmation before losing retry',async()=>{
  const h=component(),wait=deferred();fixture(h);h.network.request=()=>wait.promise
  try{const action=h.state.refreshScope();assert.equal(h.state.canLeave(),false);assert.equal(h.confirmations.length,0);wait.reject(new ApiError(0,'NETWORK_ERROR'));await action;assert.equal(h.state.hasUnsavedChanges(),true);h.setConfirm(false);assert.equal(h.state.canLeave(),false);h.state.back();assert.equal(h.events.some(e=>e.name==='back'),false);h.state.abandonPending();assert.ok(h.state.pendingScope.value);h.setConfirm(true);h.state.abandonPending();assert.equal(h.state.pendingScope.value,null);assert.equal(h.state.hasUnsavedChanges(),false);assert.equal(h.state.canLeave(),true)}finally{h.dispose()}
})
test('answer formatting preserves zero and text and maps labels using that immutable version',async()=>{
  const h=component();fixture(h)
  try{await h.state.refreshScope();h.state.knownVersions.value.push({...row(2),kind:'multi',config:{options:[{id:'same',label:'V2标签'},{id:'other',label:'另一项'}]}});assert.equal(h.state.answerText({questionVersionId:'q-a-v1',kind:'number',value:0}),'0');assert.equal(h.state.answerText({questionVersionId:'q-a-v2',kind:'multi',value:['same','other']}),'V2标签；另一项');assert.equal(h.state.answerText({questionVersionId:'q-a-v1',kind:'text',value:'=原始文本\n下一行'}),'=原始文本\n下一行')}finally{h.dispose()}
})
