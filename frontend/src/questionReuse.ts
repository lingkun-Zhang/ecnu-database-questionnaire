import { clone, uid } from './types'
import type { Definition, Question, QuestionSource, QuestionVersion } from './types'

export function sourcesInDefinition(definition: Definition, sources: QuestionSource[]): QuestionSource[] {
  const ids = new Set(definition.questions.map(question => question.id))
  return clone(sources.filter(source => ids.has(source.instanceId)))
}

export function applyQuestionVersion(definition: Definition, sources: QuestionSource[], version: QuestionVersion, instanceId?: string) {
  const next = clone(definition), mapping = sourcesInDefinition(next, sources)
  if (instanceId !== undefined) {
    const index = next.questions.findIndex(question => question.id === instanceId)
    const source = mapping.find(item => item.instanceId === instanceId)
    if (index < 0 || !source) throw new Error('这道引用题已被移除，请重新选择。')
    if (source.questionId !== version.questionId) throw new Error('更换版本时请选择同一道题目的版本。')
    // Route IDs, requiredness and rules belong to this questionnaire instance.
    // Keep them so a changed kind/option can be diagnosed instead of erased.
    next.questions[index] = { ...next.questions[index]!, ...clone(version.content) }
    source.questionVersionId = version.id
  } else {
    if (next.questions.length >= 100) throw new Error('每份问卷最多100题，请先移除不需要的题目。')
    const ids = new Set(next.questions.map(question => question.id))
    do { instanceId = uid('q') } while (ids.has(instanceId))
    const question: Question = { id: instanceId, ...clone(version.content), required: true, defaultTarget: 'END', rules: [] }
    if (!next.questions.length) next.entryQuestionId = instanceId
    const last = next.questions.at(-1)
    if (last?.defaultTarget === 'END') last.defaultTarget = instanceId
    next.questions.push(question)
    mapping.push({ instanceId, questionId: version.questionId, questionVersionId: version.id })
  }
  return { definition: next, sources: mapping, instanceId }
}

export function detachQuestionSource(definition: Definition, sources: QuestionSource[], instanceId: string): QuestionSource[] {
  return sourcesInDefinition(definition, sources).filter(source => source.instanceId !== instanceId)
}
