<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { ApiError, download, getToken, post, request } from '../api'

const props=defineProps<{questionId:string;returnToEditor?:boolean}>()
const emit=defineEmits<{'busy-change':[boolean];unauthorized:[];back:[]}>()
interface Scope {id:string;questionId:string;questionVersionId:string|null;respondentType:'all'|'anonymous'|'named';createdAt:string;expiresAt:string;publicationCount:number;surveyCount:number;instanceCount:number;hasForeignSources:boolean}
interface Overview {surveyCount:number;publicationCount:number;instanceCount:number;responseCount:number;answeredResponseCount:number;anonymousResponses:number;namedResponses:number}
interface VersionRow {questionVersionId:string;versionNo:number;kind:'single'|'multi'|'text'|'number';title:string;config:{options?:{id:string;label:string}[]};R:number;V:number;A:number;U:number;S:number;responseCount:number;answeredResponseCount:number;anonymousResponses:number;namedResponses:number;surveyCount:number;publicationCount:number;instanceCount:number;sum?:number;avg?:number|null;min?:number|null;max?:number|null;histogram?:{from:number;to:number;includeTo:boolean;count:number}[];options?:{id:string;label:string;count:number;percentage:number}[];totalSelections?:number}
interface SourceRow {id:string;surveyTitle:string;surveyVersionNo:number;asOfSeq:number;instanceCount:number;accessMode:'owner'|'aggregate';canReadAnswers:boolean}
interface AnswerRow {surveyTitle:string;surveyVersionNo:number;instanceId:string;questionVersionId:string;questionVersionNo:number;responseId:string;acceptedSeq:number;acceptedAt:string;anonymous:boolean;kind:string;value:string|number|string[]}
interface Filters {questionVersionId:string|null;respondentType:Scope['respondentType']}
interface PendingScope {key:string;body:Filters;startedAt:number;uncertain:boolean;result?:Scope}
interface RequestScope {token:string;generation:number;questionId:string}
const busy=ref(false),failure=ref<ApiError|null>(null),note=ref(''),scopeInvalid=ref(false)
const scope=ref<Scope|null>(null),overview=ref<Overview|null>(null),versionRows=ref<VersionRow[]>([]),knownVersions=ref<VersionRow[]>([])
const afterVersionNo=ref(0),versionPrevious=ref<number[]>([]),nextAfterVersionNo=ref<number|null>(null)
const sourceRows=ref<SourceRow[]>([]),sourceNext=ref<string|null>(null),answerRows=ref<AnswerRow[]>([]),answerNext=ref<string|null>(null)
const filterQuestionVersion=ref(''),filterRespondentType=ref<Scope['respondentType']>('all')
const answerVersion=ref(''),displayedAnswerVersion=ref(''),answersLoaded=ref(false),sourcesLoaded=ref(false)
const coverage=ref<{ownOnly:boolean;hasForeignAggregateSources:boolean}|null>(null),pendingScope=ref<PendingScope|null>(null)
let generation=0,alive=true
class StaleStatisticsResponse extends Error {}
const respondentNames={all:'全部答卷',anonymous:'匿名答卷',named:'实名答卷'}
const kindNames={single:'单选',multi:'多选',number:'数字',text:'文本'}
const filtersChanged=computed(()=>Boolean(scope.value)&&(filterQuestionVersion.value!==(scope.value!.questionVersionId||'')||filterRespondentType.value!==scope.value!.respondentType))
const detailFilterChanged=computed(()=>answerVersion.value!==displayedAnswerVersion.value)
const availableAnswerVersions=computed(()=>scope.value?.questionVersionId?knownVersions.value.filter(v=>v.questionVersionId===scope.value!.questionVersionId):knownVersions.value)
function capture():RequestScope{return {token:getToken(),generation,questionId:props.questionId}}
function current(s:RequestScope){return alive&&s.generation===generation&&s.token===getToken()&&s.questionId===props.questionId}
function check(s:RequestScope){if(!current(s))throw new StaleStatisticsResponse()}
function path(){return `/questions/${encodeURIComponent(props.questionId)}/statistics`}
function number(value:number|null|undefined){return value==null?'—':new Intl.NumberFormat('zh-CN',{maximumSignificantDigits:12}).format(value)}
function date(value:string){return new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date(value))}
function versionName(id:string|null){if(!id)return '全部题目版本';const item=knownVersions.value.find(v=>v.questionVersionId===id);return item?`V${item.versionNo}`:'指定题目版本'}
function clearResults(){overview.value=null;versionRows.value=[];afterVersionNo.value=0;versionPrevious.value=[];nextAfterVersionNo.value=null;sourceRows.value=[];sourceNext.value=null;sourcesLoaded.value=false;answerRows.value=[];answerNext.value=null;answersLoaded.value=false;coverage.value=null;displayedAnswerVersion.value=''}
function hasUnsavedChanges(){return Boolean(pendingScope.value)}
function canLeave(){
  if(busy.value)return false
  if(pendingScope.value?.result)return confirm('统计范围已保存，但结果尚未完整读回。确定离开？重新打开时可获取新范围。')
  if(pendingScope.value)return confirm('上次获取范围的结果尚未确认，可能已经成功。离开会放弃原标识重试入口。确定离开？')
  return true
}
function invalidate(message:string){scope.value=null;pendingScope.value=null;clearResults();scopeInvalid.value=true;note.value=message}
function showFailure(error:unknown,s:RequestScope){
  if(error instanceof StaleStatisticsResponse||!current(s))return
  failure.value=error instanceof ApiError?error:new ApiError(0,'CLIENT_ERROR',error instanceof Error?error.message:'暂时无法完成读取，请重试。')
  if(failure.value.status===410||failure.value.code==='STATISTICS_SCOPE_INVALIDATED')invalidate('原统计范围已失效。请点击“获取新范围”，重新核对当前可用来源。')
  if(failure.value.status===401){scope.value=null;pendingScope.value=null;clearResults();knownVersions.value=[];emit('unauthorized')}
}
async function run(action:(s:RequestScope)=>Promise<void>){
  if(busy.value||!alive)return
  const s=capture();busy.value=true;emit('busy-change',true);failure.value=null
  try{await action(s);check(s)}catch(error){showFailure(error,s)}
  finally{if(current(s)){busy.value=false;emit('busy-change',false)}}
}
function query(extra:Record<string,string>={}){return new URLSearchParams({scopeId:scope.value!.id,...extra}).toString()}
function rememberVersions(items:VersionRow[]){knownVersions.value=[...new Map([...knownVersions.value,...items].map(v=>[v.questionVersionId,v])).values()].sort((a,b)=>a.versionNo-b.versionNo)}
async function readSummary(s:RequestScope,after=0){
  const data=await request<{scope:Scope;overview:Overview;versions:VersionRow[];nextAfterVersionNo:number|null}>(`${path()}?${query({afterVersionNo:String(after),limit:'10'})}`);check(s)
  scope.value=data.scope;overview.value=data.overview;versionRows.value=data.versions;afterVersionNo.value=after;nextAfterVersionNo.value=data.nextAfterVersionNo;rememberVersions(data.versions)
}
async function readSources(s:RequestScope,append=false){
  const params:Record<string,string>={limit:'30'};if(append&&sourceNext.value)params.afterId=sourceNext.value
  const data=await request<{items:SourceRow[];nextAfterId:string|null}>(`${path()}/sources?${query(params)}`);check(s)
  sourceRows.value=append?[...sourceRows.value,...data.items]:data.items;sourceNext.value=data.nextAfterId;sourcesLoaded.value=true
}
async function readAnswers(s:RequestScope,append=false){
  const selected=answerVersion.value,params:Record<string,string>={limit:'30'}
  if(selected)params.questionVersionId=selected
  if(append&&answerNext.value)params.cursor=answerNext.value
  const data=await request<{items:AnswerRow[];nextCursor:string|null;coverage:{ownOnly:boolean;hasForeignAggregateSources:boolean}}>(`${path()}/answers?${query(params)}`);check(s)
  answerRows.value=append?[...answerRows.value,...data.items]:data.items;answerNext.value=data.nextCursor;displayedAnswerVersion.value=selected;answersLoaded.value=true;coverage.value=data.coverage
}
async function readSaved(s:RequestScope){
  const saved=pendingScope.value!.result!;scope.value=saved;answerVersion.value=saved.questionVersionId||''
  await readSummary(s);await readSources(s);await readAnswers(s);check(s)
  pendingScope.value=null;note.value='汇总、来源和本人明细均已读回；后续新提交需获取新范围后才会纳入。'
}
async function executeScope(){
  if(!pendingScope.value||busy.value)return
  await run(async s=>{
    const operation=pendingScope.value!
    if(!operation.result){
      if(Date.now()-operation.startedAt>=30*60*1000)throw new ApiError(0,'LOCAL_SCOPE_RETRY_EXPIRED','原请求已超过30分钟。请明确放弃原重试，再获取新范围；过期请求不会自动换标识重发。')
      try{
        const result=await post<Scope>(`${path()}/scopes`,operation.body,{'Idempotency-Key':operation.key});check(s)
        operation.result=result;operation.uncertain=false;pendingScope.value={...operation};scope.value=result;clearResults();scopeInvalid.value=false
      }catch(error){if(current(s)){operation.uncertain=!(error instanceof ApiError)||error.status===0||error.status>=500;pendingScope.value={...operation}}throw error}
    }
    try{await readSaved(s)}catch(error){if(current(s))note.value='范围已保存，但结果尚未完整读回。请重新读取结果；不会再次创建范围。';throw error}
  })
}
async function refreshScope(){
  if(busy.value||pendingScope.value)return
  pendingScope.value={key:crypto.randomUUID(),body:{questionVersionId:filterQuestionVersion.value||null,respondentType:filterRespondentType.value},startedAt:Date.now(),uncertain:true}
  // A newly requested range must never show the preceding range's results.
  scope.value=null;clearResults();note.value='';await executeScope()
}
async function retryScope(){await executeScope()}
function abandonPending(){
  if(busy.value||!pendingScope.value)return
  if(!confirm(pendingScope.value.result?'确定结束这次未完整读回的范围？之后可明确获取新范围。':'上次操作可能已经成功。确定放弃原标识重试，并重新获取当前范围？'))return
  pendingScope.value=null;scope.value=null;clearResults();failure.value=null;note.value='已结束原操作。请点击“获取新范围”重新统计。'
}
async function loadNextVersions(){
  if(!scope.value||pendingScope.value||busy.value||nextAfterVersionNo.value===null)return
  const previous=afterVersionNo.value,next=nextAfterVersionNo.value
  await run(async s=>{await readSummary(s,next);versionPrevious.value.push(previous)})
}
async function loadPreviousVersions(){
  if(!scope.value||pendingScope.value||busy.value||!versionPrevious.value.length)return
  const previous=versionPrevious.value.at(-1)!
  await run(async s=>{await readSummary(s,previous);versionPrevious.value.pop()})
}
async function loadSources(append=false){if(!scope.value||pendingScope.value||append&&!sourceNext.value)return;await run(s=>readSources(s,append))}
async function loadAnswers(append=false){
  if(!scope.value||pendingScope.value||append&&(!answerNext.value||detailFilterChanged.value))return
  await run(s=>readAnswers(s,append))
}
async function exportCSV(){
  if(!scope.value||pendingScope.value||!answersLoaded.value||detailFilterChanged.value)return
  await run(async s=>{const params:Record<string,string>={};if(displayedAnswerVersion.value)params.questionVersionId=displayedAnswerVersion.value;await download(`${path()}/export?${query(params)}`,'题目跨问卷统计明细.csv',()=>current(s));check(s)})
}
function answerText(row:AnswerRow){
  const options=knownVersions.value.find(v=>v.questionVersionId===row.questionVersionId)?.config.options
  const label=(value:string)=>options?.find(o=>o.id===value)?.label||value
  return Array.isArray(row.value)?row.value.map(label).join('；'):row.kind==='single'?label(String(row.value)):String(row.value)
}
function barWidth(count:number,all:number){return `${all?Math.max(0,Math.min(100,count/all*100)):0}%`}
function back(){if(canLeave())emit('back')}
watch(()=>props.questionId,()=>{generation++;busy.value=false;emit('busy-change',false);scope.value=null;pendingScope.value=null;knownVersions.value=[];filterQuestionVersion.value='';filterRespondentType.value='all';answerVersion.value='';scopeInvalid.value=false;failure.value=null;note.value='';clearResults()})
onUnmounted(()=>{alive=false;generation++;emit('busy-change',false);scope.value=null;pendingScope.value=null;knownVersions.value=[];clearResults()})
defineExpose({canLeave,hasUnsavedChanges})
</script>

<template>
  <section class="question-statistics">
    <div class="qstats-heading"><div><p class="eyebrow">题目 · 跨问卷统计</p><h2>把同一道题的作答放在一起看</h2></div><button type="button" class="button secondary" :disabled="busy" @click="back">{{returnToEditor?'返回问卷编辑':'返回题目管理'}}</button></div>
    <p class="muted qstats-intro">统计只认已发布问卷保存的题目来源。自己的问卷自动纳入；其他人的问卷须由其所有者另行授权，且只参与汇总。不同题目版本分别计算。</p>
    <div v-if="failure" role="alert" class="feedback error"><strong>{{failure.message}}</strong><small v-if="failure.requestId">请求编号：{{failure.requestId}}</small></div>
    <p v-if="note" role="status" class="feedback neutral">{{note}}</p>
    <form class="panel qstats-controls" @submit.prevent="refreshScope">
      <label>题目版本<select v-model="filterQuestionVersion" :disabled="busy||Boolean(pendingScope)"><option value="">全部版本，分别汇总</option><option v-for="row in knownVersions" :key="row.questionVersionId" :value="row.questionVersionId">V{{row.versionNo}} · {{row.title}}</option></select></label>
      <label>答卷类型<select v-model="filterRespondentType" :disabled="busy||Boolean(pendingScope)"><option value="all">全部答卷</option><option value="anonymous">匿名答卷</option><option value="named">实名答卷</option></select></label>
      <button type="submit" class="button primary" :disabled="busy||Boolean(pendingScope)">{{busy?'正在处理…':scope?'刷新并获取新范围':'获取新范围'}}</button>
      <small>范围有效30分钟。版本选项随汇总分页载入；筛选修改后须重新获取范围。</small>
      <p v-if="filtersChanged" class="feedback neutral">筛选已修改。下方仍是已固定范围的结果，点击刷新后应用新筛选。</p>
    </form>
    <div v-if="pendingScope" class="panel qstats-pending">
      <p>{{pendingScope.result?'范围已保存，结果尚未完整读回。':pendingScope.uncertain?'范围请求的结果尚未确认。重试将保留原筛选和原标识。':'范围请求未完成。可重试原请求，或结束后重新选择。'}}</p>
      <div class="button-row"><button type="button" class="button primary" :disabled="busy" @click="retryScope">{{pendingScope.result?'重新读取结果':'重试原请求'}}</button><button type="button" class="button secondary" :disabled="busy" @click="abandonPending">结束原操作</button></div>
    </div>
    <div v-if="scope" class="panel qstats-range">
      <strong>当前范围：{{versionName(scope.questionVersionId)}} · {{respondentNames[scope.respondentType]}}</strong>
      <span>{{date(scope.createdAt)}}获取，{{date(scope.expiresAt)}}到期</span>
      <small>每份问卷单独固定提交序号。获取范围后的新答卷不会进入本次汇总、明细或导出。</small>
    </div>
    <template v-if="scope&&overview">
      <div class="qstats-metrics">
        <div class="panel"><strong>{{overview.surveyCount}}</strong><span>来源问卷</span><small>{{overview.publicationCount}}个发布版本 · {{overview.instanceCount}}处题目实例</small></div>
        <div class="panel"><strong>{{overview.responseCount}}</strong><span>来源答卷</span><small>含未走到该题的答卷</small></div>
        <div class="panel"><strong>{{overview.answeredResponseCount}}</strong><span>至少作答一次的答卷</span><small>同一答卷多实例只计一次</small></div>
        <div class="panel"><strong>{{overview.anonymousResponses}} / {{overview.namedResponses}}</strong><span>匿名 / 实名答卷</span><small>跨问卷不推断唯一人数</small></div>
      </div>
      <div class="qstats-section-heading"><h3>按题目版本汇总</h3><div class="button-row"><button type="button" class="button secondary" :disabled="busy||Boolean(pendingScope)||!versionPrevious.length" @click="loadPreviousVersions">上一页</button><button type="button" class="button secondary" :disabled="busy||Boolean(pendingScope)||nextAfterVersionNo===null" @click="loadNextVersions">下一页</button></div></div>
      <p class="muted">R为作答机会，V为路径经过，A为有效作答，U为经过但未答，S为跳过。同一题在一份问卷出现多次时，分别计数；R = A + U + S，V = A + U。</p>
      <p v-if="!versionRows.length" class="panel muted">该范围内没有符合筛选的已发布题目来源。</p>
      <article v-for="row in versionRows" :key="row.questionVersionId" class="panel qstats-version">
        <div class="qstats-section-heading"><h3>V{{row.versionNo}} · {{row.title}}</h3><span class="status-pill">{{kindNames[row.kind]}}</span></div>
        <p class="muted">{{row.surveyCount}}份问卷 · {{row.publicationCount}}个发布版本 · {{row.instanceCount}}处实例 · {{row.responseCount}}份来源答卷</p>
        <div class="qstats-counts"><span>作答机会 R<strong>{{row.R}}</strong></span><span>路径经过 V<strong>{{row.V}}</strong></span><span>有效作答 A<strong>{{row.A}}</strong></span><span>经过未答 U<strong>{{row.U}}</strong></span><span>跳过 S<strong>{{row.S}}</strong></span></div>
        <template v-if="row.kind==='number'">
          <p class="qstats-number">合计 <strong>{{number(row.sum)}}</strong>　平均 <strong>{{number(row.avg)}}</strong>　最小 <strong>{{number(row.min)}}</strong>　最大 <strong>{{number(row.max)}}</strong></p>
          <h4>总体数值分布</h4><p v-if="!row.A" class="muted">没有有效数字答案。</p>
          <div v-for="(bin,index) in row.histogram" :key="index" class="qstats-distribution"><span :title="`${bin.from} ~ ${bin.to}`">{{bin.from===bin.to?number(bin.from):`[${number(bin.from)}, ${number(bin.to)}${bin.includeTo?']':')'}`}}</span><div class="qstats-bar"><i :style="{width:barWidth(bin.count,row.A)}"></i></div><strong>{{bin.count}}</strong></div>
          <small class="muted">按本版本全部有效数字答案的范围划分，最多10组。末组含上界；分布计数之和等于A。</small>
        </template>
        <template v-else-if="row.kind==='single'||row.kind==='multi'">
          <div v-for="option in row.options" :key="option.id" class="qstats-distribution"><span>{{option.label}}</span><div class="qstats-bar"><i :style="{width:barWidth(option.count,row.A)}"></i></div><strong>{{option.count}} <small>({{number(option.percentage)}}%)</small></strong></div>
          <small class="muted">百分比以本版本有效作答A为分母。{{row.kind==='multi'?`共选择${row.totalSelections}次，多选百分比之和可超过100%。`:'未被选择的选项也会列出。'}}</small>
        </template>
        <p v-else class="muted">文本按有效作答次数汇总。本人问卷的文本可在下方明细查看；其他问卷的文本内容不提供。</p>
      </article>
    </template>
    <section v-if="scope&&sourcesLoaded" class="panel qstats-sources"><h3>已固定的发布来源</h3><p class="muted">“提交截至”是该问卷固定时的提交序号。其他问卷只显示授权汇总来源。</p><p v-if="!sourceRows.length" class="muted">没有来源。</p><div v-for="row in sourceRows" :key="row.id" class="qstats-source"><strong>{{row.surveyTitle}}</strong><span>问卷V{{row.surveyVersionNo}} · {{row.instanceCount}}处实例 · 提交截至{{row.asOfSeq}}</span><small>{{row.accessMode==='owner'?'本人问卷，可查看本题明细':'仅授权汇总，不提供答卷明细或身份'}}</small></div><button v-if="sourceNext" type="button" class="button secondary" :disabled="busy||Boolean(pendingScope)" @click="loadSources(true)">加载更多来源</button></section>
    <section v-if="scope" class="panel qstats-answers"><h3>本人问卷的作答明细</h3><p class="muted">一行表示该题某个实例的一次有效作答，不包含其他题目的答案或答题者身份。{{scope.hasForeignSources?'本次汇总含其他人的授权问卷，下列明细和CSV仅覆盖本人问卷，因此不等于全部汇总。':''}}</p>
      <div class="qstats-controls"><label>明细版本<select v-model="answerVersion" :disabled="busy||Boolean(pendingScope)||Boolean(scope.questionVersionId)"><option value="">全部范围内版本</option><option v-for="row in availableAnswerVersions" :key="row.questionVersionId" :value="row.questionVersionId">V{{row.versionNo}} · {{row.title}}</option></select></label><button type="button" class="button secondary" :disabled="busy||Boolean(pendingScope)" @click="loadAnswers()">读取明细</button><button type="button" class="button secondary" :disabled="busy||Boolean(pendingScope)||!answersLoaded||detailFilterChanged" @click="exportCSV">导出本人明细 CSV</button></div>
      <p v-if="detailFilterChanged" class="feedback neutral">明细筛选已修改，请读取后应用；下表仍为{{versionName(displayedAnswerVersion)}}。</p>
      <p v-if="answersLoaded" class="muted">当前明细：{{versionName(displayedAnswerVersion)}} · {{respondentNames[scope.respondentType]}}。CSV最多20000条作答、8MiB，超出时须按版本缩小范围。</p>
      <div v-if="answerRows.length" class="qstats-table"><table><thead><tr><th>来源问卷</th><th>题目版本 / 实例</th><th>提交</th><th>答案</th></tr></thead><tbody><tr v-for="row in answerRows" :key="`${row.responseId}:${row.instanceId}`"><td>{{row.surveyTitle}}<small>问卷V{{row.surveyVersionNo}}</small></td><td>V{{row.questionVersionNo}}<small>{{row.instanceId}}</small></td><td>{{row.anonymous?'匿名':'实名'}} · 序号{{row.acceptedSeq}}<small>{{date(row.acceptedAt)}}</small></td><td class="qstats-answer-value">{{answerText(row)}}</td></tr></tbody></table></div>
      <p v-else-if="answersLoaded" class="muted">此筛选下没有可读取的本人作答明细。</p><button v-if="answerNext" type="button" class="button secondary" :disabled="busy||Boolean(pendingScope)||detailFilterChanged" @click="loadAnswers(true)">加载更多明细</button>
    </section>
    <p v-if="!scope&&!pendingScope&&!scopeInvalid" class="panel muted">点击“获取新范围”开始。题目共享只允许使用题目，不会自动授权读取其他人的作答。</p>
  </section>
</template>

<style scoped>
.question-statistics{display:flex;flex-direction:column;gap:20px;max-width:1200px;margin:auto}.qstats-heading,.qstats-section-heading{display:flex;justify-content:space-between;align-items:center;gap:20px}.qstats-heading h2{font-size:26px;margin:6px 0}.qstats-intro{max-width:950px;line-height:1.9;margin:0}.qstats-controls{display:flex;flex-wrap:wrap;align-items:flex-end;gap:14px}.qstats-controls label{min-width:180px;flex:1;margin:0}.qstats-controls>small,.qstats-controls>p{flex-basis:100%;margin:0}.qstats-range{display:flex;flex-direction:column;gap:8px;border-left:4px solid var(--primary,#3e6c69)}.qstats-range span,.qstats-range small{color:var(--muted,#64748b)}.qstats-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.qstats-metrics>.panel{display:flex;flex-direction:column;gap:8px}.qstats-metrics strong{font-size:28px}.qstats-metrics small{color:var(--muted,#64748b);line-height:1.6}.qstats-version h3,.qstats-section-heading h3{margin:0;overflow-wrap:anywhere}.qstats-counts{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:20px 0}.qstats-counts span{background:#f5f7f9;border-radius:9px;padding:14px;font-size:12px;color:var(--muted,#64748b)}.qstats-counts strong{display:block;font-size:23px;margin-top:8px;color:var(--text,#203239)}.qstats-number{line-height:2}.qstats-distribution{display:grid;grid-template-columns:minmax(130px,1.4fr) minmax(80px,3fr) minmax(50px,auto);gap:15px;align-items:center;margin:14px 0;font-size:13px}.qstats-distribution>span{overflow-wrap:anywhere}.qstats-distribution>strong{font-weight:600}.qstats-distribution small{font-weight:400;color:var(--muted,#64748b)}.qstats-bar{height:12px;border-radius:6px;background:#edf2f1;overflow:hidden}.qstats-bar i{height:100%;display:block;background:#4d8580;border-radius:6px}.qstats-source{display:flex;flex-direction:column;gap:7px;padding:15px 0;border-top:1px solid #e9eef1;overflow-wrap:anywhere}.qstats-source span,.qstats-source small{font-size:12px;color:var(--muted,#64748b)}.qstats-table{overflow:auto;margin:16px 0}.qstats-table table{width:100%;border-collapse:collapse;min-width:600px;text-align:left;font-size:13px}.qstats-table th,.qstats-table td{padding:13px;border-bottom:1px solid #e8edf0;vertical-align:top}.qstats-table td small{display:block;color:var(--muted,#64748b);margin-top:6px;overflow-wrap:anywhere}.qstats-answer-value{max-width:420px;white-space:pre-wrap;overflow-wrap:anywhere}.qstats-pending p{margin-top:0}.error small{display:block;margin-top:8px}.qstats-section-heading .button-row{flex-shrink:0}button:disabled{cursor:default}@media(max-width:900px){.qstats-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.qstats-heading{align-items:flex-start}.qstats-counts{grid-template-columns:repeat(3,minmax(0,1fr))}.qstats-heading h2{font-size:22px}.qstats-distribution{grid-template-columns:minmax(100px,1.5fr) minmax(40px,2fr) minmax(30px,auto);gap:9px}}@media(max-width:520px){.qstats-heading{flex-direction:column}.qstats-counts{grid-template-columns:repeat(2,minmax(0,1fr))}.qstats-metrics{grid-template-columns:1fr}.qstats-distribution{grid-template-columns:1fr 1fr auto}}
</style>
