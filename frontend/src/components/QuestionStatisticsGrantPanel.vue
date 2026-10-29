<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { ApiError, getToken, request } from '../api'

interface Grant { id:string; active:boolean; revision:number; recipientUsername:string|null }
interface Candidate { questionId:string; questionTitle:string; recipientUsername:string|null; grant:Grant|null }
interface Decision { questionId:string; title:string; recipient:string; body:{active:boolean;expectedRevision:number}; phase:'confirm'|'uncertain'|'saved'|'conflict' }
const props=defineProps<{surveyId:string}>()
const emit=defineEmits<{ 'busy-change':[value:boolean]; unauthorized:[] }>()
const rows=ref<Candidate[]>([]), busy=ref(false), loaded=ref(false), failure=ref(''), note=ref(''), pending=ref<Decision|null>(null)
let generation=0, alive=true
function scope(){return {id:props.surveyId,token:getToken(),generation}}
function current(s:ReturnType<typeof scope>){return alive&&s.id===props.surveyId&&s.token===getToken()&&s.generation===generation}
function setBusy(value:boolean){busy.value=value;emit('busy-change',value)}
function error(value:unknown,s:ReturnType<typeof scope>){
  if(!current(s))return
  const e=value instanceof ApiError?value:new ApiError(0,'CLIENT_ERROR','操作未完成，请重试。')
  failure.value=e.message
  if(e.status===401){rows.value=[];pending.value=null;emit('unauthorized')}
}
async function read(s:ReturnType<typeof scope>){
  const result=await request<{items:Candidate[]}>(`/surveys/${encodeURIComponent(s.id)}/question-statistics-grants`)
  if(!current(s))return false
  rows.value=result.items;loaded.value=true;return true
}
async function load(){
  if(busy.value||pending.value)return
  const s=scope();setBusy(true);failure.value=''
  try{await read(s)}catch(e){error(e,s)}finally{if(current(s))setBusy(false)}
}
function decide(row:Candidate,active:boolean){
  if(busy.value||pending.value)return
  pending.value={questionId:row.questionId,title:row.questionTitle,recipient:row.recipientUsername||'题目作者',body:{active,expectedRevision:row.grant?.revision||0},phase:'confirm'}
  failure.value='';note.value=''
}
function cancel(){if(!busy.value&&pending.value?.phase==='confirm')pending.value=null}
async function submit(){
  const operation=pending.value
  if(busy.value||!operation||!['confirm','uncertain'].includes(operation.phase))return
  const s=scope();setBusy(true);failure.value='';operation.phase='uncertain'
  try{
    await request(`/surveys/${encodeURIComponent(s.id)}/question-statistics-grants/${encodeURIComponent(operation.questionId)}`,{method:'PUT',body:JSON.stringify(operation.body)})
    if(!current(s)||pending.value!==operation)return
    operation.phase='saved'
    if(await read(s)){pending.value=null;note.value=operation.body.active?'已授权题目作者查看这道题在本问卷中的汇总。':'已撤销统计授权，使用旧范围的后续查询将失效。'}
  }catch(e){
    if(!current(s))return
    if(operation.phase!=='saved'&&e instanceof ApiError&&e.status>=400&&e.status<500){
      if(e.status===409)operation.phase='conflict'
      else pending.value=null
    }
    error(e,s)
  }finally{if(current(s))setBusy(false)}
}
async function refreshSaved(){
  if(busy.value||pending.value?.phase!=='saved')return
  const s=scope();setBusy(true);failure.value=''
  try{if(await read(s)){pending.value=null;note.value='授权结果已重新读取。'}}catch(e){error(e,s)}finally{if(current(s))setBusy(false)}
}
async function abandon(){
  if(busy.value||!pending.value||!confirm('结束这次授权操作并读取当前结果？未确认的请求仍可能已经生效，请以重新读取的状态为准。'))return
  pending.value=null;note.value='';await load()
}
function hasUnsavedChanges(){return busy.value||pending.value!==null}
function canLeave(){return !busy.value&&(!pending.value||confirm('统计授权还有未确认的操作。确定离开？重新进入后请读取当前授权状态。'))}
watch(()=>props.surveyId,()=>{generation++;rows.value=[];loaded.value=false;pending.value=null;failure.value='';note.value='';setBusy(false);void load()})
onMounted(load)
onUnmounted(()=>{alive=false;generation++;emit('busy-change',false)})
defineExpose({canLeave,hasUnsavedChanges})
</script>

<template>
  <section class="panel statistics-grants">
    <div class="section-heading"><div><h3>向题目作者提供统计汇总</h3><p class="hint">这里列出本问卷发布过的他人题目。逐题授权后，作者可查看该题的汇总，不会获得逐条答案、填写者身份或其他题目的内容。题目共享与统计授权分别设置。</p></div><button class="button secondary small" :disabled="busy||!!pending" @click="load">重新读取</button></div>
    <p v-if="failure" role="alert" class="error-message">{{failure}}</p><p v-if="note" role="status">{{note}}</p>
    <p v-if="loaded&&!rows.length" class="hint">还没有发布过他人的题目。自己的题目可直接统计，无需授权。</p>
    <article v-for="row in rows" :key="row.questionId" class="grant-row"><div><strong>{{row.questionTitle}}</strong><small>题目作者：{{row.recipientUsername||'账号不可用'}} · {{row.grant?.active?'已授权汇总':'未授权汇总'}}<template v-if="row.grant"> · 修订 {{row.grant.revision}}</template></small></div><button class="button secondary small" :disabled="busy||!!pending" @click="decide(row,!row.grant?.active)">{{row.grant?.active?'撤销统计授权':'授权统计汇总'}}</button></article>
    <div v-if="pending" class="grant-confirm" role="status">
      <p v-if="pending.phase==='confirm'">{{pending.body.active?'允许':'撤销'}}「{{pending.recipient}}」汇总「{{pending.title}}」在本问卷全部发布版本中的数据？{{pending.body.active?'范围包括已有和后续接收的答卷。':'撤销后，旧统计范围的后续读取会失效；已经看到或保存的汇总无法收回。'}}</p>
      <p v-else-if="pending.phase==='uncertain'">授权请求的结果尚未确认。重试会使用原来的操作和修订号。</p>
      <p v-else-if="pending.phase==='saved'">操作已保存，当前状态读取失败。请重新读取结果。</p>
      <p v-else>授权状态已经变化。结束本次操作并读取当前状态后，再决定是否修改。</p>
      <button v-if="pending.phase==='confirm'" class="button" :disabled="busy" @click="submit">确认{{pending.body.active?'授权':'撤销'}}</button>
      <button v-if="pending.phase==='confirm'" class="button secondary" :disabled="busy" @click="cancel">取消</button>
      <button v-if="pending.phase==='uncertain'" class="button" :disabled="busy" @click="submit">使用原操作重试</button>
      <button v-if="pending.phase==='saved'" class="button" :disabled="busy" @click="refreshSaved">重新读取结果</button>
      <button v-if="pending.phase!=='confirm'" class="button secondary" :disabled="busy" @click="abandon">结束本次操作并读取状态</button>
    </div>
  </section>
</template>

<style scoped>
.statistics-grants{margin-top:24px}.grant-row{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 0;border-top:1px solid var(--border,#e5e7eb)}.grant-row small{display:block;margin-top:6px;color:var(--muted,#64748b)}.grant-confirm{padding:16px;background:#f5f7fa;border-radius:10px;margin-top:14px}.grant-confirm button{margin:4px 8px 4px 0}.error-message{color:#b42318}
</style>
