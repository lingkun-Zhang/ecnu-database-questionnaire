import type { Definition, FieldError } from './types'

export interface EditorErrorTarget {
  section: 'meta' | 'questions' | 'flow' | 'json'
  questionId?: string
  ruleId?: string
  field: string
  label: string
  exact: boolean
}

type ObjectValue = Record<string, unknown>
const object = (value: unknown): value is ObjectValue => value !== null && typeof value === 'object' && !Array.isArray(value)
const stableId = (value: unknown): value is string => typeof value === 'string' && value !== 'END' && /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(value)
const indexValue = (value: string) => {
  const index = Number(value)
  return Number.isSafeInteger(index) && index >= 0 ? index : -1
}
const hasId = (value: unknown) => value !== null && value !== undefined

/** A rendering guard for JSON imports, not protocol or publication validation.
 * It deliberately returns boolean: numeric bounds, routing, uniqueness, unknown
 * fields and condition semantics are still checked by the backend. The extra
 * recursion budget protects the editor while allowing protocol-invalid trees
 * to reach that validation without silently rewriting their contents.
 */
export function canRenderDefinition(value: unknown): boolean {
  if (!object(value) || typeof value.title !== 'string' || typeof value.description !== 'string'
    || typeof value.allowAnonymous !== 'boolean'
    || (value.entryQuestionId !== null && typeof value.entryQuestionId !== 'string')
    || !Array.isArray(value.questions)) return false
  let conditionNodes = 0
  const renderCondition = (node: unknown, depth: number): boolean => {
    conditionNodes += 1
    if (conditionNodes > 10_000 || depth > 32 || !object(node)) return false
    for (const key of ['allOf', 'anyOf']) {
      if (!Object.hasOwn(node, key)) continue
      const children = node[key]
      if (!Array.isArray(children) || !children.every(child => renderCondition(child, depth + 1))) return false
    }
    return true
  }
  return value.questions.every((q: unknown) => {
    if (!object(q) || typeof q.id !== 'string' || typeof q.title !== 'string'
      || typeof q.kind !== 'string' || !['single', 'multi', 'text', 'number'].includes(q.kind)
      || typeof q.required !== 'boolean' || !object(q.config) || !Array.isArray(q.rules)) return false
    const config = q.config
    if (Object.hasOwn(config, 'options') || q.kind === 'single' || q.kind === 'multi') {
      if (!Array.isArray(config.options)
        || !config.options.every((option: unknown) => object(option) && typeof option.id === 'string' && typeof option.label === 'string')) return false
    }
    return q.rules.every((r: unknown) => object(r) && typeof r.id === 'string' && renderCondition(r.condition, 1))
  })
}

/** Map a validation error to an existing editor control without mutating either input.
 * Question and rule IDs take precedence over the indices in an older validation
 * response. Ambiguous identities and fields without a visual editor stay in JSON.
 */
export function resolveEditorError(definition: Definition, error: FieldError): EditorErrorTarget {
  const originalField = typeof error.field === 'string' && error.field ? error.field : 'definition'
  const json = (reason: string): EditorErrorTarget => ({
    section: 'json', field: originalField, label: `JSON 高级编辑：${reason}（${originalField}）`, exact: false,
  })
  const field = originalField.startsWith('definition.') ? originalField.slice('definition.'.length) : originalField
  if (!object(definition)) return json('当前问卷结构无法定位')

  const meta: Record<string, string> = { title: '问卷标题', description: '问卷说明', allowAnonymous: '允许匿名填写' }
  if (Object.hasOwn(meta, field)) {
    if (hasId(error.questionId) || hasId(error.ruleId)) return json('字段与题目或规则标识不一致')
    return { section: 'meta', field, label: meta[field]!, exact: true }
  }
  if (field === 'entryQuestionId') {
    if (hasId(error.questionId) || hasId(error.ruleId)) return json('入口字段与题目或规则标识不一致')
    return { section: 'flow', field, label: '入口题', exact: true }
  }
  const questionPath = /^questions\[(0|[1-9]\d*)\](?:\.(.*))?$/.exec(field)
  if (!questionPath || !Array.isArray(definition.questions)) return json('该字段没有对应的可视化编辑控件')
  const questions: unknown[] = definition.questions
  const requestedQuestionId: unknown = error.questionId
  const requestedRuleId: unknown = error.ruleId
  let questionIndex = indexValue(questionPath[1]!)
  const tail = questionPath[2] || ''
  const rulePath = /^rules\[(0|[1-9]\d*)\](?:\.(.*))?$/.exec(tail)
  if (hasId(requestedQuestionId)) {
    if (!stableId(requestedQuestionId)) return json('题目标识无效，无法可靠定位')
    const matches = questions.flatMap((q, index) => object(q) && q.id === requestedQuestionId ? [index] : [])
    if (matches.length !== 1) return json(matches.length ? '题目标识重复，无法可靠定位' : '原题目已删除或标识已更改')
    questionIndex = matches[0]!
  }

  // Rule IDs are unique across the questionnaire, including when no question ID
  // was supplied. A rule moved to another question must not redirect an error
  // that also explicitly names its former owner.
  let ruleIndex = rulePath ? indexValue(rulePath[1]!) : -1
  if (hasId(requestedRuleId)) {
    if (!rulePath || !stableId(requestedRuleId)) return json('规则字段或标识无效，无法可靠定位')
    const matches: Array<{ questionIndex: number; ruleIndex: number }> = []
    questions.forEach((q, qi) => {
      if (object(q) && Array.isArray(q.rules)) q.rules.forEach((r: unknown, ri: number) => {
        if (object(r) && r.id === requestedRuleId) matches.push({ questionIndex: qi, ruleIndex: ri })
      })
    })
    if (matches.length !== 1) return json(matches.length ? '规则标识重复，无法可靠定位' : '原规则已删除或标识已更改')
    const match = matches[0]!
    if (hasId(requestedQuestionId) && match.questionIndex !== questionIndex) return json('规则已不属于原题目')
    questionIndex = match.questionIndex
    ruleIndex = match.ruleIndex
  }
  const question = questions[questionIndex]
  if (!object(question) || !stableId(question.id)) return json('原题目或有效题目标识不存在')
  if (questions.filter(q => object(q) && q.id === question.id).length !== 1) return json('题目标识重复，无法可靠定位')
  const base = `questions[${questionIndex}]`
  const questionLabel = `第 ${questionIndex + 1} 题${typeof question.title === 'string' && question.title ? `「${question.title}」` : ''}`
  const target = (suffix: string, label: string, exact = true, ruleId?: string): EditorErrorTarget => ({
    section: 'questions', questionId: question.id as string, ...(ruleId ? { ruleId } : {}),
    field: `${base}.${suffix}`, label: `${questionLabel} · ${label}`, exact,
  })
  const simple: Record<string, string> = { title: '题目名称', kind: '题型', required: '必须回答', defaultTarget: '默认去向' }
  if (Object.hasOwn(simple, tail)) return target(tail, simple[tail]!)
  if (tail === 'id') return json('稳定题目标识须在 JSON 中修改')

  if (tail === 'config' || tail.startsWith('config.')) {
    const config = question.config
    if (!object(config)) return json('题目配置不是可编辑的对象')
    const configNames: Record<string, Record<string, string>> = {
      number: { min: '最小值（含）', max: '最大值（含）', integerOnly: '只接受整数' },
      text: { minLength: '最少字数', maxLength: '最多字数' },
      multi: { minSelected: '至少选择', maxSelected: '最多选择' }, single: {},
    }
    const names = typeof question.kind === 'string' ? configNames[question.kind] : undefined
    if (!names) return json('未知题型的配置须先在 JSON 中修正')
    if (tail === 'config') {
      const firstBound = question.kind === 'number' ? 'min' : question.kind === 'text' ? 'minLength' : question.kind === 'multi' ? 'minSelected' : ''
      return firstBound ? target(`config.${firstBound}`, `${names[firstBound]}（请同时检查上下限）`, false) : json('请检查完整选项配置')
    }
    const key = tail.slice('config.'.length)
    if (Object.hasOwn(names, key)) return target(tail, names[key]!)
    const optionPath = /^options\[(0|[1-9]\d*)\]\.(label|id)$/.exec(key)
    if (!optionPath || !['single', 'multi'].includes(String(question.kind)) || !Array.isArray(config.options)) return json('该配置没有对应的可视化编辑控件')
    const optionIndex = indexValue(optionPath[1]!)
    const option = config.options[optionIndex]
    if (!object(option)) return json('原选项不存在或结构无效')
    if (optionPath[2] === 'id') return json('稳定选项标识须在 JSON 中修改')
    if (!stableId(option.id) || config.options.filter(o => object(o) && o.id === option.id).length !== 1) return json('选项标识无效或重复，无法可靠定位')
    return target(`config.options[${optionIndex}].label`, `选项 ${optionIndex + 1} 文案`)
  }

  if (!rulePath || !Array.isArray(question.rules)) return json('该字段没有对应的可视化编辑控件')
  const rule = question.rules[ruleIndex]
  if (!object(rule) || !stableId(rule.id)) return json('原规则或有效规则标识不存在')
  let ruleMatches = 0
  questions.forEach(q => {
    if (object(q) && Array.isArray(q.rules)) ruleMatches += q.rules.filter(r => object(r) && r.id === rule.id).length
  })
  if (ruleMatches !== 1) return json('规则标识重复，无法可靠定位')
  const ruleBase = `rules[${ruleIndex}]`
  const ruleField = rulePath[2] || ''
  const ruleTarget = (suffix: string, label: string, exact = true) => target(`${ruleBase}.${suffix}`, `条件规则 ${ruleIndex + 1} · ${label}`, exact, rule.id as string)
  if (ruleField === 'priority') return ruleTarget('priority', '优先级')
  if (ruleField === 'target') return ruleTarget('target', '条件满足时跳到')
  if (ruleField === 'id') return json('稳定规则标识须在 JSON 中修改')
  if (ruleField !== 'condition' && !ruleField.startsWith('condition.')) return json('该规则字段没有对应的可视化编辑控件')
  if (!['single', 'multi', 'number'].includes(String(question.kind))) return json('当前题型不支持条件规则')

  let condition: unknown = rule.condition
  let rest = ruleField.slice('condition'.length)
  let conditionField = 'condition'
  while (true) {
    if (!object(condition)) return json('条件节点不是可编辑的对象')
    const node = condition
    const groups = ['allOf', 'anyOf'].filter(key => Object.hasOwn(node, key))
    if (groups.length) {
      const group = groups[0]!
      if (groups.length !== 1 || Object.keys(condition).length !== 1 || !Array.isArray(condition[group])) return json('组合条件结构无效，须先在 JSON 中修正')
      if (rest === '' || rest === `.${group}`) return ruleTarget(conditionField, '条件组合方式（请检查整组条件）', false)
      const child = /^\.(allOf|anyOf)\[(0|[1-9]\d*)\](.*)$/.exec(rest)
      if (!child || child[1] !== group) return json('条件路径与当前组合结构不一致')
      const childIndex = indexValue(child[2]!)
      condition = condition[group][childIndex]
      conditionField += `.${group}[${childIndex}]`
      rest = child[3]!
      continue
    }
    if (Object.keys(condition).some(key => !['operator', 'value'].includes(key))) return json('条件包含未定义的结构')
    if (rest === '') return ruleTarget(conditionField, '条件组合方式（请检查此条件）', false)
    if (rest === '.operator') return ruleTarget(`${conditionField}.operator`, '比较方式')
    if (rest === '.value') {
      if (question.kind === 'number' && condition.operator === 'between') {
        if (!object(condition.value) || Object.keys(condition.value).some(key => key !== 'min' && key !== 'max')) return json('条件区间结构无效，须先在 JSON 中修正')
        return ruleTarget(`${conditionField}.value.min`, '比较区间（请同时检查最小值和最大值）', false)
      }
      return ruleTarget(`${conditionField}.value`, question.kind === 'single' ? '目标选项' : question.kind === 'multi' ? '比较选项集合' : '比较数字')
    }
    if ((rest === '.value.min' || rest === '.value.max') && question.kind === 'number' && condition.operator === 'between') {
      if (!object(condition.value) || Object.keys(condition.value).some(key => key !== 'min' && key !== 'max')) return json('条件区间结构无效，须先在 JSON 中修正')
      return ruleTarget(`${conditionField}${rest}`, rest.endsWith('.min') ? '比较区间最小值' : '比较区间最大值')
    }
    return json('该条件路径没有对应的可视化编辑控件')
  }
}
