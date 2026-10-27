<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ApiError, getToken, request } from '../api'

interface Counts {surveyCount:number;draftSurveyCount:number;draftInstanceCount:number;publishedSurveyCount:number;currentPublishedSurveyCount:number;currentPublishedInstanceCount:number;historicalSnapshotCount:number;historicalInstanceCount:number}
interface Row {reference:string;ownership:'own'|'other';draftInstanceCount:number;currentPublishedInstanceCount:number;historicalSnapshotCount:number;historicalInstanceCount:number;surveyId?:string;surveyTitle?:string;surveyStatus?:string}
interface Impact {questionId:string;questionVersionId:string|null;checkedAt:string;scope:'saved_references';summary:{own:Counts;others:Counts};items:Row[];offset:number;limit:number;nextOffset:number|null}
interface Approval {decisionId:string;questionId:string;sourceVersionId:string;checkedAt:string}
const props=defineProps<{decisionId:string;questionId:string;sourceVersionId:string;sourceVersionLabel:string;operation:'derive'|'restore'}>()
const emit=defineEmits<{confirm:[approval:Approval];cancel:[];'busy-change':[boolean];unauthorized:[]}>()
const busy=ref(false),failure=ref<ApiError|null>(null),data=ref<Impact|null>(null),filter=ref<'source'|'series'>('source')
let generation=0,alive=true
const accepted=ref(false)
interface Session {token:string;generation:number;decisionId:string;questionId:string;sourceVersionId:string;filter:'source'|'series'}
function capture():Session{return {token:getToken(),generation,decisionId:props.decisionId,questionId:props.questionId,sourceVersionId:props.sourceVersionId,filter:filter.value}}
function current(s:Session){return alive&&s.token===getToken()&&s.generation===generation&&s.decisionId===props.decisionId&&s.questionId===props.questionId&&s.sourceVersionId===props.sourceVersionId&&s.filter===filter.value}
const confirmedScope=ref<Session|null>(null)
const canConfirm=computed(()=>!busy.value&&!failure.value&&Boolean(data.value&&confirmedScope.value&&current(confirmedScope.value))&&!accepted.value)
const summaryGroups=computed(()=>data.value?[{name:'本人问卷',counts:data.value.summary.own},{name:'他人问卷',counts:data.value.summary.others}]:[])
function date(value:string){return new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date(value))}
function setBusy(value:boolean){busy.value=value;emit('busy-change',value)}
async function load(offset=0){
  if(busy.value||!alive||accepted.value)return
  const s=capture();setBusy(true);failure.value=null
  const params=new URLSearchParams({offset:String(offset),limit:'20'})
  if(s.filter==='source')params.set('questionVersionId',s.sourceVersionId)
  try{
    const result=await request<Impact>(`/questions/${encodeURIComponent(s.questionId)}/impact?${params}`)
    if(!current(s))return
    const expected=s.filter==='source'?s.sourceVersionId:null
    if(result.questionId!==s.questionId||result.questionVersionId!==expected||result.scope!=='saved_references'||!result.checkedAt)throw new ApiError(500,'INVALID_IMPACT_RESPONSE','影响结果与当前题目或版本不一致，请重新检查。')
    data.value=result;confirmedScope.value=s
  }catch(error){
    if(!current(s))return
    failure.value=error instanceof ApiError?error:new ApiError(0,'CLIENT_ERROR','未能读取使用影响，请重新检查。')
    if(failure.value.status===401){data.value=null;confirmedScope.value=null;emit('unauthorized')}
  }finally{if(current(s))setBusy(false)}
}
function confirmReview(){
  const s=confirmedScope.value
  if(!canConfirm.value||!s||!data.value||!current(s))return
  accepted.value=true
  emit('confirm',{decisionId:s.decisionId,questionId:s.questionId,sourceVersionId:s.sourceVersionId,checkedAt:data.value.checkedAt})
}
function cancel(){if(!busy.value&&!accepted.value)emit('cancel')}
async function changeFilter(value:'source'|'series'){
  if(busy.value||accepted.value||filter.value===value)return
  filter.value=value;generation++;data.value=null;confirmedScope.value=null;failure.value=null;await load()
}
async function next(){if(data.value?.nextOffset!=null)await load(data.value.nextOffset)}
async function previous(){if(data.value&&data.value.offset>0)await load(Math.max(0,data.value.offset-data.value.limit))}
watch(()=>[props.decisionId,props.questionId,props.sourceVersionId,props.operation],()=>{generation++;accepted.value=false;filter.value='source';data.value=null;confirmedScope.value=null;failure.value=null;setBusy(false);void load()})
onMounted(()=>void load())
onUnmounted(()=>{alive=false;generation++;emit('busy-change',false)})
</script>

<template>
  <section class="panel impact-review" aria-labelledby="impact-review-title">
    <div class="section-heading"><div><div class="eyebrow">保存前检查</div><h3 id="impact-review-title">{{operation==='restore'?'恢复为新版本前，核对已有引用':'创建新版本前，核对已有引用'}}</h3></div><button class="button secondary small" :disabled="busy" @click="load()">重新检查</button></div>
    <p class="hint">这次操作基于{{sourceVersionLabel}}。创建新版本只保存新的题目内容；已经保存的草稿和发布问卷仍固定原版，不会自动换版。{{operation==='restore'?'若暂不恢复，可以取消检查；当前版本和已有引用都会保留。':'若暂不创建，可以取消检查并继续编辑，也可以取消编辑。'}}</p>
    <div class="button-row impact-filters" role="group" aria-label="影响检查范围"><button class="button secondary small" :aria-pressed="filter==='source'" :disabled="busy" @click="changeFilter('source')">只看{{sourceVersionLabel}}</button><button class="button secondary small" :aria-pressed="filter==='series'" :disabled="busy" @click="changeFilter('series')">查看整个题目系列</button></div>
    <div v-if="failure" class="feedback error" role="alert"><strong>{{failure.message}}</strong><p>读取失败不表示没有引用。请重新检查后再决定是否创建新版本。</p></div>
    <p v-if="busy" role="status" class="hint">正在读取已保存的引用…</p>
    <template v-if="data">
      <p class="hint">本页检查时间：{{date(data.checkedAt)}}。当前范围：{{data.questionVersionId?sourceVersionLabel:'整个题目系列'}}。只统计服务器已保存的引用，不包含本地未保存编辑；查询不会锁住之后的变化。</p>
      <div class="impact-summary"><article v-for="group in summaryGroups" :key="group.name"><h4>{{group.name}}</h4><strong>{{group.counts.surveyCount}} 份不同问卷</strong><p>当前草稿 {{group.counts.draftSurveyCount}} 份 / {{group.counts.draftInstanceCount}} 处引用</p><p>当前发布 {{group.counts.currentPublishedSurveyCount}} 份 / {{group.counts.currentPublishedInstanceCount}} 处引用</p><p>历史发布 {{group.counts.historicalSnapshotCount}} 个快照 / {{group.counts.historicalInstanceCount}} 处引用</p><small>曾发布过的问卷：{{group.counts.publishedSurveyCount}} 份。各类可能重叠，不能直接相加。</small></article></div>
      <h4>引用条目</h4><p class="hint">本人问卷显示标题；他人问卷只显示稳定的脱敏引用代号。这里不提供他人问卷内容、身份、填写链接或答卷。关闭问卷的当前发布版仍是当前版。</p>
      <p v-if="!data.items.length" class="hint">这一页没有匹配的已保存引用。</p>
      <article v-for="row in data.items" :key="row.reference" class="impact-row"><strong>{{row.ownership==='own'?row.surveyTitle:row.reference}}</strong><span>{{row.ownership==='own'?'本人问卷':'他人问卷'}} · 草稿{{row.draftInstanceCount}}处 · 当前发布{{row.currentPublishedInstanceCount}}处 · 历史发布{{row.historicalSnapshotCount}}个快照 / {{row.historicalInstanceCount}}处</span></article>
      <div class="button-row"><button class="button secondary small" :disabled="busy||data.offset===0" @click="previous">上一页</button><span class="hint">第{{Math.floor(data.offset/data.limit)+1}}页</span><button class="button secondary small" :disabled="busy||data.nextOffset===null" @click="next">下一页</button></div>
      <p class="hint">这是实时分页，每次读取可能看到新的保存结果；完整重新核对请从第一页检查。</p>
    </template>
    <div class="button-row impact-decision"><button class="button primary" :disabled="!canConfirm" @click="confirmReview">{{operation==='restore'?'确认恢复为新版本':'确认创建新版本'}}</button><button class="button secondary" :disabled="busy" @click="cancel">{{operation==='restore'?'取消恢复':'取消检查，保留编辑'}}</button></div>
  </section>
</template>

<style scoped>
.impact-review{border:1px solid #b6d2c8;margin-bottom:20px}.impact-filters{margin:14px 0}.impact-filters [aria-pressed="true"]{border-color:var(--primary);background:#edf5f1}.impact-summary{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:16px 0}.impact-summary article{background:#f7f9f8;border-radius:8px;padding:16px}.impact-summary h4{margin:0 0 12px}.impact-summary strong{font-size:19px}.impact-summary p{margin:9px 0;font-size:13px}.impact-summary small{color:var(--muted);line-height:1.6}.impact-row{display:flex;flex-direction:column;gap:7px;padding:14px 0;border-top:1px solid #e7ece9;overflow-wrap:anywhere}.impact-row span{font-size:13px;color:var(--muted)}.impact-decision{margin-top:18px}@media(max-width:700px){.impact-summary{grid-template-columns:1fr}}
</style>
