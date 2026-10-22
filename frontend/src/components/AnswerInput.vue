<script setup lang="ts">
import type { Answer, Question } from '../types'
const props = defineProps<{question: Question; modelValue: Answer | undefined; disabled?: boolean}>()
const emit = defineEmits<{ 'update:modelValue': [value: Answer] }>()
function toggle(id: string) { const values = Array.isArray(props.modelValue) ? [...props.modelValue] : []; const index=values.indexOf(id); index<0 ? values.push(id) : values.splice(index,1);emit('update:modelValue',values.length ? values : null) }
function numberInput(event: Event) { const raw=(event.target as HTMLInputElement).value; emit('update:modelValue',raw.trim()==='' ? null : Number(raw)) }
</script>
<template>
  <div v-if="question.kind==='single' || question.kind==='multi'" class="answer-options">
    <label v-for="option in question.config.options || []" :key="option.id" class="answer-option" :class="{selected: question.kind==='single' ? modelValue===option.id : Array.isArray(modelValue)&&modelValue.includes(option.id)}">
      <input v-if="question.kind==='single'" type="radio" :name="question.id" :value="option.id" :checked="modelValue===option.id" :disabled="disabled" @change="emit('update:modelValue',option.id)" />
      <input v-else type="checkbox" :checked="Array.isArray(modelValue)&&modelValue.includes(option.id)" :disabled="disabled" @change="toggle(option.id)" />
      <span>{{ option.label }}</span>
    </label>
    <button v-if="!question.required && modelValue" class="text-button small" :disabled="disabled" @click="emit('update:modelValue',null)">清除此题回答</button>
  </div>
  <textarea v-else-if="question.kind==='text'" :value="typeof modelValue==='string' ? modelValue : ''" :disabled="disabled" rows="4" placeholder="在这里写下你的回答…" @input="emit('update:modelValue',($event.target as HTMLTextAreaElement).value || null)" />
  <input v-else type="number" :value="typeof modelValue==='number' ? modelValue : ''" :min="question.config.min ?? undefined" :max="question.config.max ?? undefined" :step="question.config.integerOnly ? 1 : 'any'" :disabled="disabled" placeholder="请输入数字" @input="numberInput" />
  <p class="hint" v-if="question.kind==='multi'">{{ question.config.minSelected===question.config.maxSelected && question.config.minSelected!=null ? `请选择恰好 ${question.config.minSelected} 项` : `至少 ${question.config.minSelected ?? 0} 项${question.config.maxSelected!=null ? `，最多 ${question.config.maxSelected} 项` : ''}` }}</p>
  <p class="hint" v-if="question.kind==='text'">{{ question.config.minLength!=null ? `最少 ${question.config.minLength} 字 · ` : '' }}{{ question.config.maxLength!=null ? `最多 ${question.config.maxLength} 字` : '按需填写' }}</p>
  <p class="hint" v-if="question.kind==='number'">{{ question.config.integerOnly ? '仅限整数' : '支持小数' }}{{ question.config.min!=null ? ` · 最小 ${question.config.min}` : '' }}{{ question.config.max!=null ? ` · 最大 ${question.config.max}` : '' }}</p>
</template>
