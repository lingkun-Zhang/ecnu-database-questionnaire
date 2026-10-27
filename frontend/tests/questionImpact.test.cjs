const assert=require('node:assert/strict')
const {readFileSync}=require('node:fs')
const {resolve}=require('node:path')
const {test}=require('node:test')
const vm=require('node:vm'),ts=require('typescript'),vue=require('vue')
const {parse}=require('@vue/compiler-sfc')
const plain=v=>JSON.parse(JSON.stringify(v))
class ApiError extends Error{constructor(status,code,message=code){super(message);this.status=status;this.code=code}}
const defer=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}}
async function settle(){for(let i=0;i<3;i++){await vue.nextTick();await new Promise(setImmediate)}}
function component(){
 const props=vue.reactive({decisionId:'d1',questionId:'q1',sourceVersionId:'v1',sourceVersionLabel:'V1',operation:'derive'})
 const events=[],calls=[],mounted=[],unmounted=[],effect=vue.effectScope();let token='account-a'
 const network={read:async()=>{throw new Error('Unexpected GET')}}
 const src=parse(readFileSync(resolve(__dirname,'../src/components/QuestionImpactReview.vue'),'utf8')).descriptor.scriptSetup.content
 const module={exports:{}}
 const exposed='busy,failure,data,filter,canConfirm,summaryGroups,load,confirmReview,cancel,changeFilter,next,previous'
 effect.run(()=>vm.runInNewContext(ts.transpileModule(src+`\nmodule.exports={${exposed}}`,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{
  module,exports:module.exports,URLSearchParams,
  require:name=>name==='vue'?{...vue,onMounted:fn=>mounted.push(fn),onUnmounted:fn=>unmounted.push(fn)}:name==='../api'?{ApiError,getToken:()=>token,request:async path=>{calls.push(path);return network.read(path)}}:(()=>{throw new Error(name)})(),
  defineProps:()=>props,defineEmits:()=> (name,...args)=>events.push({name,args:plain(args)})
 }))
 return {state:module.exports,props,network,calls,events,setToken:v=>token=v,mount:async()=>{for(const f of mounted)await f();await settle()},dispose:()=>{unmounted.forEach(f=>f());effect.stop()}}
}
const counts=()=>({surveyCount:0,draftSurveyCount:0,draftInstanceCount:0,publishedSurveyCount:0,currentPublishedSurveyCount:0,currentPublishedInstanceCount:0,historicalSnapshotCount:0,historicalInstanceCount:0})
function fixture(h){h.network.read=async path=>{const p=new URL('http://test'+path).searchParams;return {questionId:h.props.questionId,questionVersionId:p.get('questionVersionId'),checkedAt:'2026-10-27T08:00:00Z',scope:'saved_references',summary:{own:counts(),others:counts()},items:[],offset:Number(p.get('offset')),limit:20,nextOffset:null}};return h.network.read}
test('the child reads the selected source and waits for a deliberate confirmation',async()=>{
 const h=component();fixture(h);try{await h.mount();assert.equal(h.calls.length,1);assert.match(h.calls[0],/questionVersionId=v1/);assert.equal(h.state.canConfirm.value,true);assert.equal(h.events.filter(e=>e.name==='confirm').length,0)
 h.state.confirmReview();h.state.confirmReview();assert.equal(h.events.filter(e=>e.name==='confirm').length,1);assert.equal(h.state.canConfirm.value,false)
 assert.deepEqual(h.events.find(e=>e.name==='confirm').args[0],{decisionId:'d1',questionId:'q1',sourceVersionId:'v1',checkedAt:'2026-10-27T08:00:00Z'})
 }finally{h.dispose()}
})
test('failed reads are not empty success and cannot authorize a version write',async()=>{
 const h=component();h.network.read=async()=>{throw new ApiError(503,'UNAVAILABLE')};try{await h.mount();assert.equal(h.state.data.value,null);assert.equal(h.state.canConfirm.value,false);h.state.confirmReview();assert.equal(h.events.filter(e=>e.name==='confirm').length,0);fixture(h);await h.state.load();assert.equal(h.state.canConfirm.value,true)}finally{h.dispose()}
})
test('failed later page keeps its prior label but disables confirmation until a successful read',async()=>{
 const h=component();fixture(h);try{await h.mount();h.network.read=async()=>{throw new ApiError(503,'FAILED_PAGE')};await h.state.load(20);assert.ok(h.state.data.value);assert.equal(h.state.data.value.offset,0);assert.equal(h.state.canConfirm.value,false);fixture(h);await h.state.load();assert.equal(h.state.canConfirm.value,true)}finally{h.dispose()}
})
test('changing to series and paging uses the same filter without confusing source identity',async()=>{
 const h=component();const read=fixture(h);h.network.read=async p=>({...await read(p),nextOffset:p.includes('offset=0')?20:null});try{await h.mount();await h.state.changeFilter('series');assert.equal(h.state.data.value.questionVersionId,null);await h.state.next();assert.equal(h.state.data.value.offset,20);assert.ok(!h.calls.at(-1).includes('questionVersionId'));await h.state.previous();assert.equal(h.state.data.value.offset,0);h.state.confirmReview();assert.equal(h.events.find(e=>e.name==='confirm').args[0].sourceVersionId,'v1')}finally{h.dispose()}
})
test('401 clears the private review and expires only the current account',async()=>{
 const h=component();fixture(h);try{await h.mount();h.network.read=async()=>{throw new ApiError(401,'UNAUTHENTICATED')};await h.state.load();assert.equal(h.state.data.value,null);assert.equal(h.state.canConfirm.value,false);assert.equal(h.events.filter(e=>e.name==='unauthorized').length,1)}finally{h.dispose()}
})
for(const late of ['success','401'])test(`late old-account ${late} cannot install data or expire the new account`,async()=>{
 const h=component(),d=defer();const read=fixture(h);h.network.read=()=>d.promise
 try{const running=h.state.load();h.setToken('account-b');late==='success'?d.resolve(await read('/?questionVersionId=v1&offset=0')):d.reject(new ApiError(401,'UNAUTHENTICATED'));await running;assert.equal(h.state.data.value,null);assert.equal(h.events.filter(e=>e.name==='unauthorized').length,0)}finally{h.dispose()}
})
test('changing decision or version rejects the old in-flight read and confirms only the new source',async()=>{
 const h=component(),d=defer();const read=fixture(h);let first=true;h.network.read=p=>first?(first=false,d.promise):read(p)
 try{const old=h.state.load();h.props.decisionId='d2';h.props.sourceVersionId='v2';await settle();d.resolve({questionId:'q1',questionVersionId:'v1',scope:'saved_references',checkedAt:'old'});await old;await settle();assert.equal(h.state.data.value.questionVersionId,'v2');h.state.confirmReview();assert.equal(h.events.find(e=>e.name==='confirm').args[0].decisionId,'d2')}finally{h.dispose()}
})
test('cancel sends no confirmation and busy guards stop duplicate reads and decisions',async()=>{
 const h=component(),d=defer();h.network.read=()=>d.promise;try{const running=h.state.load();await h.state.load();h.state.cancel();h.state.confirmReview();assert.equal(h.calls.length,1);assert.equal(h.events.filter(e=>e.name==='cancel'||e.name==='confirm').length,0);fixture(h);d.resolve(await h.network.read('/?questionVersionId=v1&offset=0'));await running;h.state.cancel();assert.equal(h.events.filter(e=>e.name==='cancel').length,1)}finally{h.dispose()}
})
test('unmount invalidates a late response and releases the parent busy state',async()=>{
 const h=component(),d=defer();const read=fixture(h);h.network.read=()=>d.promise;const running=h.state.load();h.dispose();d.resolve(await read('/?questionVersionId=v1&offset=0'));await running;assert.equal(h.state.data.value,null);assert.equal(h.events.at(-1).name,'busy-change');assert.equal(h.events.at(-1).args[0],false)
})
test('wrong-question or wrong-filter responses cannot authorize saving',async()=>{
 const h=component();const read=fixture(h);h.network.read=async p=>({...await read(p),questionId:'other'});try{await h.mount();assert.equal(h.state.canConfirm.value,false);assert.equal(h.state.failure.value.code,'INVALID_IMPACT_RESPONSE')}finally{h.dispose()}
})
