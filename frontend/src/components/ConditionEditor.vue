<script setup lang="ts">
import { computed } from 'vue'
import type { Condition, Question } from '../types'
const props=defineProps<{modelValue: Condition; question: Question; depth?: number; fieldPrefix?: string}>()
const emit=defineEmits<{'update:modelValue':[Condition]}>()
const group=computed(()=>props.modelValue.allOf ? 'allOf' : props.modelValue.anyOf ? 'anyOf' : '')
const children=computed(()=>props.modelValue.allOf || props.modelValue.anyOf || [])
const operators=computed(()=>props.question.kind==='single' ? [{value:'eqOption',label:'等于选项'}] : props.question.kind==='multi' ? [{value:'containsAny',label:'包含任一选项'},{value:'containsAll',label:'包含全部选项'},{value:'setEquals',label:'与这些选项恰好相同'}] : [{value:'lt',label:'小于'},{value:'lte',label:'小于或等于'},{value:'gt',label:'大于'},{value:'gte',label:'大于或等于'},{value:'eq',label:'等于'},{value:'between',label:'位于闭区间'}])
function leaf():Condition { return {operator:operators.value[0]?.value || 'eq',value:props.question.kind==='single' ? props.question.config.options?.[0]?.id || '' : props.question.kind==='multi' ? [props.question.config.options?.[0]?.id || ''] : 0} }
function changeMode(mode:string) {
  const retained=group.value ? children.value : [props.modelValue]
  emit('update:modelValue',mode==='leaf' ? leaf() : {[mode]:[...retained]})
}
function updateChild(index:number,value:Condition) { const list=[...children.value];list[index]=value;emit('update:modelValue',{[group.value]:list}) }
function removeChild(index:number) { const list=children.value.filter((_,i)=>i!==index);emit('update:modelValue',{[group.value]:list}) }
function addChild() { emit('update:modelValue',{[group.value]:[...children.value,leaf()]}) }
function changeOperator(operator:string) { emit('update:modelValue',{operator,value:operator==='between' ? {min:0,max:18} : leaf().value}) }
function changeValue(value:unknown) { emit('update:modelValue',{...props.modelValue,value}) }
function toggleOption(id:string) { const list=Array.isArray(props.modelValue.value) ? [...props.modelValue.value] as string[] : [];const index=list.indexOf(id);index<0 ? list.push(id) : list.splice(index,1);changeValue(list) }
function rangeValue(key:'min'|'max') { return (props.modelValue.value as Record<string,number> | undefined)?.[key] ?? 0 }
function rangeInput(key:'min'|'max', event:Event) { changeValue({...((props.modelValue.value || {}) as object),[key]:Number((event.target as HTMLInputElement).value)}) }
</script>
<template>
  <div class="condition-editor">
    <div class="inline-fields">
      <select :data-editor-field="fieldPrefix" aria-label="条件组合方式" :value="group || 'leaf'" @change="changeMode(($event.target as HTMLSelectElement).value)">
        <option value="leaf">当前题的回答</option><option v-if="(depth || 0)<3" value="allOf">全部条件满足（且）</option><option v-if="(depth || 0)<3" value="anyOf">任一条件满足（或）</option>
      </select>
      <select v-if="!group" :data-editor-field="fieldPrefix?fieldPrefix+'.operator':undefined" aria-label="比较方式" :value="modelValue.operator" @change="changeOperator(($event.target as HTMLSelectElement).value)"><option v-for="op in operators" :key="op.value" :value="op.value">{{op.label}}</option></select>
    </div>
    <template v-if="group">
      <div v-for="(child,index) in children" :key="index" class="condition-child"><ConditionEditor :model-value="child" :question="question" :depth="(depth||0)+1" :field-prefix="fieldPrefix?`${fieldPrefix}.${group}[${index}]`:undefined" @update:model-value="updateChild(index,$event)"/><button class="icon-button danger-text" :disabled="children.length<=1" aria-label="移除此条件" @click="removeChild(index)">×</button></div>
      <button class="text-button small" @click="addChild">＋ 添加子条件</button>
    </template>
    <select v-else-if="question.kind==='single'" :data-editor-field="fieldPrefix?fieldPrefix+'.value':undefined" aria-label="目标选项" :value="modelValue.value as string" @change="changeValue(($event.target as HTMLSelectElement).value)"><option v-for="option in question.config.options" :key="option.id" :value="option.id">{{option.label}}</option></select>
    <div v-else-if="question.kind==='multi'" class="check-chips"><label v-for="(option,index) in question.config.options" :key="option.id"><input :data-editor-field="index===0&&fieldPrefix?fieldPrefix+'.value':undefined" type="checkbox" :checked="Array.isArray(modelValue.value)&&modelValue.value.includes(option.id)" @change="toggleOption(option.id)"/>{{option.label}}</label></div>
    <div v-else-if="modelValue.operator==='between'" class="inline-fields"><label>最小值<input type="number" :data-editor-field="fieldPrefix?fieldPrefix+'.value.min':undefined" :value="rangeValue('min')" @input="rangeInput('min',$event)"/></label><span>至</span><label>最大值<input type="number" :data-editor-field="fieldPrefix?fieldPrefix+'.value.max':undefined" :value="rangeValue('max')" @input="rangeInput('max',$event)"/></label></div>
    <input v-else type="number" :data-editor-field="fieldPrefix?fieldPrefix+'.value':undefined" aria-label="比较数字" :value="modelValue.value as number" @input="changeValue(Number(($event.target as HTMLInputElement).value))"/>
  </div>
</template>
