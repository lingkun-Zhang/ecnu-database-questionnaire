<script setup lang="ts">
import { computed, nextTick, onMounted, onUpdated, ref, watch } from 'vue'
import ConditionEditor from './ConditionEditor.vue'
import { clone, kindNames, newQuestion, uid } from '../types'
import type { Definition, FieldError, Kind, Question, Rule } from '../types'
import { canRenderDefinition, resolveEditorError } from '../editorErrors'
const props=defineProps<{modelValue: Definition; errors?: FieldError[]; warnings?: FieldError[]}>()
const emit=defineEmits<{'update:modelValue':[Definition]}>()
const selected=ref('');const section=ref<'questions'|'flow'|'json'>('questions');const jsonText=ref('');const jsonError=ref('')
const editorRoot=ref<HTMLElement|null>(null)
const focusedError=ref<{message:string;label:string;field:string;warning:boolean}|null>(null)
let focusCounter=0
const current=computed(()=>props.modelValue.questions.find(q=>q.id===selected.value) || props.modelValue.questions[0])
watch(()=>props.modelValue,()=>{if(!props.modelValue.questions.some(q=>q.id===selected.value))selected.value=props.modelValue.questions[0]?.id || ''},{immediate:true})
function add(kind:Kind) { const def=clone(props.modelValue);const q=newQuestion(kind);q.title=`新的${kindNames[kind]}`;if(!def.questions.length)def.entryQuestionId=q.id;const last=def.questions.at(-1);if(last?.defaultTarget==='END')last.defaultTarget=q.id;def.questions.push(q);emit('update:modelValue',def);selected.value=q.id }
function remove(q:Question) { if(!confirm(`删除“${q.title || '未命名题目'}”？相关跳转将需要重新检查。`))return;const def=clone(props.modelValue);def.questions=def.questions.filter(x=>x.id!==q.id);if(def.entryQuestionId===q.id)def.entryQuestionId=def.questions[0]?.id||'';emit('update:modelValue',def) }
function move(q:Question,delta:number) { const def=clone(props.modelValue);const index=def.questions.findIndex(x=>x.id===q.id);const target=index+delta;if(target<0||target>=def.questions.length)return;[def.questions[index],def.questions[target]]=[def.questions[target]!,def.questions[index]!];emit('update:modelValue',def) }
function resetFlow(){if(!confirm('按当前题目顺序重新设置默认去向？已配置的条件跳转仍会保留。'))return;const def=clone(props.modelValue);def.questions.forEach((q,i)=>q.defaultTarget=def.questions[i+1]?.id||'END');def.entryQuestionId=def.questions[0]?.id||'';emit('update:modelValue',def)}
function changeKind(q:Question,event:Event) { const kind=(event.target as HTMLSelectElement).value as Kind;q.kind=kind;q.config=newQuestion(kind).config;q.rules=[] }
function setLimit(q:Question,key: keyof Question['config'],event:Event) { const raw=(event.target as HTMLInputElement).value;(q.config as Record<string,unknown>)[key]=raw==='' ? null : Number(raw) }
function exact(q:Question,event:Event) { const raw=(event.target as HTMLInputElement).value;const value=raw==='' ? null : Number(raw);q.config.minSelected=value;q.config.maxSelected=value }
function addRule(q:Question) { const rule:Rule={id:uid('r'),priority:q.rules.length ? Math.max(...q.rules.map(r=>r.priority))+10 : 10,condition:{operator:q.kind==='single'?'eqOption':q.kind==='multi'?'containsAny':'lt',value:q.kind==='single' ? q.config.options?.[0]?.id||'' : q.kind==='multi' ? [q.config.options?.[0]?.id||''] : 18},target:'END'};q.rules.push(rule) }
function openJSON(){jsonText.value=JSON.stringify(props.modelValue,null,2);jsonError.value='';section.value='json'}
function applyJSON(){try{const value=JSON.parse(jsonText.value);if(!canRenderDefinition(value))throw new Error('题目配置、规则或条件结构不完整。请在 JSON 中修正；当前草稿保持不变。');emit('update:modelValue',value as Definition);jsonError.value='';section.value='questions'}catch(error){jsonError.value=error instanceof Error?error.message:'JSON 格式有误'}}
function title(id:string){return id==='END' ? '结束问卷' : props.modelValue.questions.find(q=>q.id===id)?.title||`无效目标 ${id}`}
function questionField(field:string){return `questions[${props.modelValue.questions.indexOf(current.value!)}].${field}`}
function ruleField(index:number,field:string){return questionField(`rules[${index}].${field}`)}
function relatedFields(error:FieldError){
  const target=resolveEditorError(props.modelValue,error),field=target.section==='json'?'definition':target.field,fields=[field]
  if(!target.exact){
    for(const [low,high] of [['.config.min','.config.max'],['.config.minLength','.config.maxLength'],['.config.minSelected','.config.maxSelected'],['.value.min','.value.max']]){
      if(field.endsWith(low!))fields.push(field.slice(0,-low!.length)+high!)
    }
  }
  return fields
}
function applyFieldErrors(){
  const targets=(props.errors||[]).map((issue,index)=>({fields:relatedFields(issue),id:`definition-error-${index}`}))
  const warnings=(props.warnings||[]).map((issue,index)=>({fields:relatedFields(issue),id:`design-warning-${index}`}))
  for(const element of editorRoot.value?.querySelectorAll<HTMLElement>('[data-editor-field]')||[]){
    const ids=targets.filter(target=>target.fields.includes(element.dataset.editorField||'')).map(target=>target.id)
    const warningIds=warnings.filter(target=>target.fields.includes(element.dataset.editorField||'')).map(target=>target.id)
    element.classList.toggle('editor-field-invalid',ids.length>0)
    element.classList.toggle('editor-field-warning',warningIds.length>0)
    if(ids.length)element.setAttribute('aria-invalid','true')
    else element.removeAttribute('aria-invalid')
    const descriptions=[...ids,...warningIds]
    if(descriptions.length)element.setAttribute('aria-describedby',descriptions.join(' '))
    else element.removeAttribute('aria-describedby')
  }
}
function currentIssue(issue:FieldError){return (props.errors||[]).includes(issue)||(props.warnings||[]).includes(issue)}
async function focusError(issue:FieldError){
  const sequence=++focusCounter,target=resolveEditorError(props.modelValue,issue)
  if(!currentIssue(issue))return false
  if(target.section==='json'){if(section.value!=='json')openJSON()}
  else{section.value=target.section==='flow'?'flow':'questions';if(target.questionId)selected.value=target.questionId}
  await nextTick()
  if(sequence!==focusCounter||!currentIssue(issue))return false
  const field=target.section==='json'?'definition':target.field
  let element=Array.from(editorRoot.value?.querySelectorAll<HTMLElement>('[data-editor-field]')||[]).find(node=>node.dataset.editorField===field)
  if(!element){
    if(section.value!=='json')openJSON()
    await nextTick()
    if(sequence!==focusCounter||!currentIssue(issue))return false
    element=editorRoot.value?.querySelector<HTMLElement>('[data-editor-field="definition"]')||undefined
  }
  if(!element)return false
  focusedError.value={message:issue.message,label:target.label,field:issue.field||'',warning:(props.warnings||[]).includes(issue)}
  applyFieldErrors()
  const control=element.matches('input,select,textarea,button')?element:element.querySelector<HTMLElement>('input,select,textarea,button')||element
  control.scrollIntoView({block:'center'});control.focus({preventScroll:true})
  return true
}
watch(()=>props.modelValue,()=>{focusCounter++;focusedError.value=null},{deep:true,flush:'sync'})
watch(()=>props.errors,()=>{focusCounter++;focusedError.value=null},{deep:true})
watch(()=>props.warnings,()=>{focusCounter++;focusedError.value=null},{deep:true})
onMounted(applyFieldErrors);onUpdated(applyFieldErrors)
defineExpose({focusError})
</script>
<template>
  <div ref="editorRoot" class="definition-editor">
    <div class="panel definition-meta">
      <div class="eyebrow">问卷信息</div>
      <label>问卷标题<input v-model="modelValue.title" data-editor-field="title" maxlength="200" placeholder="请输入问卷标题"/></label>
      <label>问卷说明<textarea v-model="modelValue.description" data-editor-field="description" rows="2" placeholder="填写问卷说明（选填）"/></label>
      <label class="switch-line"><input v-model="modelValue.allowAnonymous" data-editor-field="allowAnonymous" type="checkbox"/><span><strong>允许匿名填写</strong><small>填写者仍需登录；匿名时，创建者无法查看其账号。</small></span></label>
    </div>
    <div class="editor-toolbar"><div class="segmented"><button :class="{active:section==='questions'}" @click="section='questions'">题目与规则</button><button :class="{active:section==='flow'}" @click="section='flow'">流程概览</button><button :class="{active:section==='json'}" @click="openJSON">JSON 高级编辑</button></div><span class="muted small">{{modelValue.questions.length}} / 100 题</span></div>
    <p v-if="focusedError" :class="['editor-focus-message',{'editor-warning-message':focusedError.warning}]" role="status">{{focusedError.warning?'设计提示':'需要修改'}}：{{focusedError.label}}。{{focusedError.message}}</p>
    <div v-if="section==='json'" class="panel"><p class="hint">适合批量配置。应用后仍须经过后端发布检查；请保留题目、选项和规则的稳定 ID。</p><textarea v-model="jsonText" data-editor-field="definition" class="code-editor" rows="24" spellcheck="false" aria-label="问卷 JSON 定义"/><p v-if="jsonError" class="error-text">{{jsonError}}</p><button class="button primary" @click="applyJSON">应用到草稿</button></div>
    <div v-else-if="section==='flow'" class="panel"><div class="section-heading"><div><h3>跳转流程</h3><p class="hint">检查入口题、默认去向和条件跳转。发布时会检查无效目标和循环。</p></div><button class="button secondary small" @click="resetFlow">按排序重置默认流程</button></div><label>入口题<select v-model="modelValue.entryQuestionId" data-editor-field="entryQuestionId"><option value="">请选择入口</option><option v-for="q in modelValue.questions" :key="q.id" :value="q.id">{{q.title}}</option></select></label><div class="flow-list"><div v-for="(q,index) in modelValue.questions" :key="q.id" class="flow-row"><span class="flow-number">{{index+1}}</span><div><strong>{{q.title}}</strong><small>{{kindNames[q.kind]}} · {{q.id}}</small><div class="flow-route">默认 → {{title(q.defaultTarget)}}</div><div v-for="rule in [...q.rules].sort((a,b)=>a.priority-b.priority)" :key="rule.id" class="flow-rule">优先级 {{rule.priority}} · {{JSON.stringify(rule.condition)}} → {{title(rule.target)}}</div></div></div></div><div class="flow-end">✓ 结束问卷</div></div>
    <div v-else class="question-workspace">
      <aside class="question-index panel"><div class="eyebrow">题目导航</div><button v-for="(q,index) in modelValue.questions" :key="q.id" :class="['question-index-item',{active:current?.id===q.id}]" @click="selected=q.id"><span>{{String(index+1).padStart(2,'0')}}</span><div>{{q.title || '未命名题目'}}<small>{{kindNames[q.kind]}} {{q.rules.length ? `· ${q.rules.length} 条规则` : ''}}</small></div></button><div class="add-question"><span class="small muted">添加题目</span><button v-for="kind in (['single','multi','text','number'] as Kind[])" :key="kind" :data-editor-field="kind==='single'?'questions':undefined" :disabled="modelValue.questions.length>=100" @click="add(kind)">＋ {{kindNames[kind]}}</button></div></aside>
      <div v-if="current" class="question-detail panel" :key="current.id">
        <div class="section-heading"><span class="eyebrow">题目 {{modelValue.questions.indexOf(current)+1}}</span><div class="button-row"><button class="icon-button" title="上移显示顺序" aria-label="上移题目" @click="move(current,-1)">↑</button><button class="icon-button" title="下移显示顺序" aria-label="下移题目" @click="move(current,1)">↓</button><button class="icon-button danger-text" title="删除题目" aria-label="删除题目" @click="remove(current)">×</button></div></div>
        <label>题目名称<input v-model="current.title" :data-editor-field="questionField('title')" maxlength="500" placeholder="请输入题目内容"/></label>
        <div class="inline-fields"><label>题型<select :value="current.kind" :data-editor-field="questionField('kind')" @change="changeKind(current,$event)"><option v-for="kind in (['single','multi','text','number'] as Kind[])" :key="kind" :value="kind">{{kindNames[kind]}}</option></select></label><label class="checkbox-line"><input v-model="current.required" :data-editor-field="questionField('required')" type="checkbox"/> 必须回答</label></div>
        <p class="stable-id">稳定标识 <code>{{current.id}}</code></p>
        <template v-if="current.kind==='single'||current.kind==='multi'">
          <div class="field-heading">选项 <small>修改文案不改变选项标识</small></div>
          <div v-for="(option,index) in current.config.options" :key="option.id" class="option-editor"><span>{{String.fromCharCode(65+index)}}</span><input v-model="option.label" :data-editor-field="questionField(`config.options[${index}].label`)" :aria-label="`选项 ${index+1}`"/><button class="icon-button danger-text" aria-label="删除选项" @click="current.config.options?.splice(index,1)">×</button></div>
          <button class="text-button" :data-editor-field="questionField('config.options')" @click="current.config.options?.push({id:uid('o'),label:'新选项'})">＋ 添加选项</button>
          <div v-if="current.kind==='multi'" class="limit-grid"><label>至少选择<input type="number" min="0" :value="current.config.minSelected" :data-editor-field="questionField('config.minSelected')" @input="setLimit(current,'minSelected',$event)"/></label><label>最多选择<input type="number" min="0" :value="current.config.maxSelected" :data-editor-field="questionField('config.maxSelected')" placeholder="不限制" @input="setLimit(current,'maxSelected',$event)"/></label><label>快捷设置恰好 N 项<input type="number" min="0" :value="current.config.minSelected===current.config.maxSelected?current.config.minSelected:''" placeholder="同时设置上下限" @input="exact(current,$event)"/></label></div>
        </template>
        <div v-if="current.kind==='text'" class="limit-grid"><label>最少字数<input type="number" min="0" :value="current.config.minLength" :data-editor-field="questionField('config.minLength')" placeholder="不限制" @input="setLimit(current,'minLength',$event)"/></label><label>最多字数<input type="number" min="0" :value="current.config.maxLength" :data-editor-field="questionField('config.maxLength')" placeholder="不限制" @input="setLimit(current,'maxLength',$event)"/></label></div>
        <template v-if="current.kind==='number'"><div class="limit-grid"><label>最小值（含）<input type="number" :value="current.config.min" :data-editor-field="questionField('config.min')" placeholder="不限制" @input="setLimit(current,'min',$event)"/></label><label>最大值（含）<input type="number" :value="current.config.max" :data-editor-field="questionField('config.max')" placeholder="不限制" @input="setLimit(current,'max',$event)"/></label></div><label class="checkbox-line"><input v-model="current.config.integerOnly" :data-editor-field="questionField('config.integerOnly')" type="checkbox"/> 只接受整数</label></template>
        <div class="rule-section"><div class="section-heading"><div><h3>跳转规则</h3><p class="hint">先匹配优先级较小的规则，未命中时走默认去向。</p></div></div><label>默认去向<select v-model="current.defaultTarget" :data-editor-field="questionField('defaultTarget')"><option value="END">结束问卷</option><option v-for="q in modelValue.questions.filter(x=>x.id!==current?.id)" :key="q.id" :value="q.id">{{q.title}} · {{q.id}}</option></select></label>
          <div v-for="(rule,index) in current.rules" :key="rule.id" class="rule-card" :data-editor-rule="rule.id"><div class="section-heading"><strong>条件规则 {{index+1}}</strong><button class="icon-button danger-text" aria-label="删除规则" @click="current.rules.splice(index,1)">×</button></div><label>优先级<input v-model.number="rule.priority" :data-editor-field="ruleField(index,'priority')" type="number" step="1"/></label><ConditionEditor v-model="rule.condition" :question="current" :field-prefix="ruleField(index,'condition')"/><label>条件满足时跳到<select v-model="rule.target" :data-editor-field="ruleField(index,'target')"><option value="END">结束问卷</option><option v-for="q in modelValue.questions.filter(x=>x.id!==current?.id)" :key="q.id" :value="q.id">{{q.title}} · {{q.id}}</option></select></label><small class="stable-id">{{rule.id}}</small></div>
          <button v-if="current.kind!=='text'" class="button secondary full-width" :data-editor-field="questionField('rules')" :disabled="current.rules.length>=20" @click="addRule(current)">＋ 添加条件跳转</button><p v-else class="hint">文本题采用默认去向。单选、多选和数字题可添加条件规则。</p>
        </div>
      </div>
      <div v-else class="panel empty-state"><div class="empty-symbol">＋</div><h3>暂无题目</h3><p>从题目导航中选择题型添加题目。</p></div>
    </div>
  </div>
</template>
