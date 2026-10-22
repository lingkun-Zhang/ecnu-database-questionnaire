const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { resolve } = require('node:path')
const { test } = require('node:test')
const vm = require('node:vm')
const ts = require('typescript')

// Execute the actual, dependency-free helper. No copied resolver or simulated
// implementation participates in these expectations.
const compiled = ts.transpileModule(readFileSync(resolve(__dirname, '../src/editorErrors.ts'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const compiledModule = { exports: {} }
vm.runInNewContext(compiled, { module: compiledModule, exports: compiledModule.exports })
const { resolveEditorError, canRenderDefinition } = compiledModule.exports
const plain = value => JSON.parse(JSON.stringify(value))
const error = (field, ids = {}) => ({ code: 'invalid_type', message: '请检查此处', field, ...ids })
const question = (id, kind = 'number') => ({
  id, kind, title: `题目 ${id}`, required: true,
  config: kind === 'number' ? { min: 0, max: 100, integerOnly: true }
    : kind === 'text' ? { minLength: 1, maxLength: 100 }
      : { options: [{ id: 'yes', label: '是' }, { id: 'no', label: '否' }], ...(kind === 'multi' ? { minSelected: 1, maxSelected: 2 } : {}) },
  defaultTarget: 'END', rules: [],
})
const rule = (id, condition = { operator: 'lt', value: 18 }) => ({ id, priority: 10, condition, target: 'END' })
const definition = (...questions) => ({ title: '测试问卷', description: '', allowAnonymous: true, entryQuestionId: questions[0]?.id || '', questions })
const expectJSON = (def, issue, reason) => {
  const actual = resolveEditorError(def, issue)
  assert.equal(actual.section, 'json', reason)
  assert.equal(actual.field, issue.field || 'definition', 'JSON fallback retains the original diagnostic path')
  assert.equal(actual.exact, false)
  assert.match(actual.label, /JSON/)
  return actual
}

test('a saved validation error follows question and rule IDs after independent reordering', () => {
  const age = question('q_age'), note = question('q_note', 'text')
  age.rules = [rule('r_young'), rule('r_adult')]
  const oldIssue = error('questions[0].rules[0].priority', { questionId: 'q_age', ruleId: 'r_young' })
  const def = definition(note, { ...age, rules: [...age.rules].reverse() })
  const actual = plain(resolveEditorError(def, oldIssue))
  assert.deepEqual(actual, {
    section: 'questions', questionId: 'q_age', ruleId: 'r_young', field: 'questions[1].rules[1].priority',
    label: '第 2 题「题目 q_age」 · 条件规则 2 · 优先级', exact: true,
  })
  const questionIssue = resolveEditorError(def, error('questions[0].title', { questionId: 'q_age' }))
  assert.equal(questionIssue.field, 'questions[1].title')
  assert.equal(questionIssue.questionId, 'q_age')
})

test('a globally unique rule ID can locate its current owner, while an explicit former owner cannot', () => {
  const age = question('q_age'), score = question('q_score')
  score.rules.push(rule('r_moved'))
  const def = definition(age, score)
  const actual = resolveEditorError(def, error('questions[0].rules[4].target', { ruleId: 'r_moved' }))
  assert.equal(actual.field, 'questions[1].rules[0].target')
  assert.equal(actual.questionId, 'q_score')
  assert.equal(actual.ruleId, 'r_moved')
  expectJSON(def, error('questions[0].rules[4].target', { questionId: 'q_age', ruleId: 'r_moved' }), 'do not redirect an error to a rule now owned by another question')
})

test('nested any/all condition paths preserve the exact edited leaf and range boundary', () => {
  const age = question('q_age')
  age.rules.push(rule('r_nested', { allOf: [
    { operator: 'gte', value: 0 },
    { anyOf: [{ operator: 'lt', value: 18 }, { operator: 'between', value: { min: 20, max: 60 } }] },
  ] }))
  const def = definition(question('q_intro', 'text'), age)
  const prefix = 'questions[0].rules[7].condition.allOf[1].anyOf[1]'
  const ids = { questionId: 'q_age', ruleId: 'r_nested' }
  for (const suffix of ['operator', 'value.min', 'value.max']) {
    const actual = resolveEditorError(def, error(`${prefix}.${suffix}`, ids))
    assert.equal(actual.field, `questions[1].rules[0].condition.allOf[1].anyOf[1].${suffix}`)
    assert.equal(actual.exact, true)
    assert.equal(actual.questionId, 'q_age'); assert.equal(actual.ruleId, 'r_nested')
  }
  const range = resolveEditorError(def, error(`${prefix}.value`, ids))
  assert.equal(range.field, 'questions[1].rules[0].condition.allOf[1].anyOf[1].value.min')
  assert.equal(range.exact, false)
  assert.match(range.label, /最小值.*最大值/)
  const group = resolveEditorError(def, error('questions[0].rules[7].condition.allOf[1]', ids))
  assert.equal(group.field, 'questions[1].rules[0].condition.allOf[1]')
  assert.equal(group.exact, false)
})

test('index-only backend errors locate legal existing fields, including option wording and required properties', () => {
  const multi = question('q_multi', 'multi'), age = question('q_age')
  age.rules.push(rule('r_age'))
  const def = definition(multi, age)
  const expected = [
    ['questions[0].config.options[1].label', 'q_multi'],
    ['questions[0].config.maxSelected', 'q_multi'],
    ['questions[0].required', 'q_multi'],
    ['questions[1].kind', 'q_age'],
    ['questions[1].defaultTarget', 'q_age'],
    ['questions[1].config.integerOnly', 'q_age'],
    ['questions[1].rules[0].condition.operator', 'q_age'],
    ['questions[1].rules[0].condition.value', 'q_age'],
  ]
  for (const [field, questionId] of expected) {
    const actual = resolveEditorError(def, error(field))
    assert.equal(actual.field, field)
    assert.equal(actual.questionId, questionId)
    assert.equal(actual.exact, true)
  }
  const withNullIds = resolveEditorError(def, error('questions[0].title', { questionId: null, ruleId: null }))
  assert.equal(withNullIds.section, 'questions')
})

test('definition metadata and entry errors open their actual editor sections', () => {
  const def = definition(question('q_age'))
  for (const field of ['title', 'description', 'allowAnonymous']) {
    const actual = resolveEditorError(def, error(`definition.${field}`))
    assert.equal(actual.section, 'meta')
    assert.equal(actual.field, field)
    assert.equal(actual.exact, true)
  }
  const entry = resolveEditorError(def, error('entryQuestionId'))
  assert.equal(entry.section, 'flow'); assert.equal(entry.field, 'entryQuestionId')
  expectJSON(def, error('title', { questionId: 'q_age' }), 'contradictory identity metadata is not silently ignored')
})

test('whole constraint errors direct users to the first bound and explicitly cover both bounds', () => {
  for (const [kind, first] of [['number', 'min'], ['text', 'minLength'], ['multi', 'minSelected']]) {
    const def = definition(question('q_bounds', kind))
    const actual = resolveEditorError(def, { ...error('questions[0].config', { questionId: 'q_bounds' }), code: 'bound_order' })
    assert.equal(actual.field, `questions[0].config.${first}`)
    assert.equal(actual.exact, false)
    assert.match(actual.label, /上下限/)
  }
})

test('deleted or duplicate stable IDs cannot fall back to a different item at the old index', () => {
  const age = question('q_age'); age.rules.push(rule('r_age'))
  expectJSON(definition(age), error('questions[0].title', { questionId: 'q_deleted' }))
  expectJSON(definition(age), error('questions[0].rules[0].priority', { questionId: 'q_age', ruleId: 'r_deleted' }))
  expectJSON(definition(age, question('q_age')), error('questions[0].title', { questionId: 'q_age' }))
  expectJSON(definition(age, question('q_age')), error('questions[0].title'), 'duplicate question keys are unsafe even with an index')
  const other = question('q_other'); other.rules.push(rule('r_age'))
  expectJSON(definition(age, other), error('questions[0].rules[0].priority', { questionId: 'q_age', ruleId: 'r_age' }))
  expectJSON(definition(age, other), error('questions[0].rules[0].priority'), 'rule uniqueness is questionnaire-wide')
  const choice = question('q_choice', 'single'); choice.config.options[1].id = 'yes'
  expectJSON(definition(choice), error('questions[0].config.options[1].label'))
})

test('stable ID edits and collection structure errors retain their original JSON paths', () => {
  const choice = question('q_choice', 'single'); choice.rules.push(rule('r_choice', { operator: 'eqOption', value: 'yes' }))
  const def = definition(choice)
  for (const field of ['questions[0].id', 'questions[0].config.options[1].id', 'questions[0].rules[0].id',
    'definition.questions', 'questions[0].config.options', 'questions[0].rules', 'definition']) {
    expectJSON(def, error(field))
  }
  const fallback = resolveEditorError(def, { message: '没有字段信息' })
  assert.equal(fallback.section, 'json'); assert.equal(fallback.field, 'definition')
})

test('unknown fields, malformed paths, and absent nested nodes never select a nearby control', () => {
  const age = question('q_age'); age.rules.push(rule('r_age', { allOf: [{ operator: 'eq', value: 1 }] }))
  const def = definition(age)
  for (const field of [
    'questions[-1].title', 'questions[01].title', 'questions[9007199254740993].title', 'questions[1].title',
    'questions[0].config.__proto__', 'questions[0].config.options[0].label', 'questions[0].config.max.evil',
    'questions[0].rules[0].condition.allOf[1].operator', 'questions[0].rules[0].condition.anyOf[0].operator',
    'questions[0].rules[0].condition.allOf[0].value.min', 'questions[0].rules[0].condition.allOf[0].extra',
    'questions[0].rules[0].condition.allOf[0].value;globalThis.hacked=true',
  ]) expectJSON(def, error(field))
})

test('unrenderable condition or config structures are handled without throwing', () => {
  const age = question('q_age'); age.rules.push(rule('r_age'))
  for (const malformed of [null, 2, [], { allOf: [], anyOf: [] }, { allOf: {} }, { operator: 'lt', value: 1, unknown: true }]) {
    age.rules[0].condition = malformed
    expectJSON(definition(age), error('questions[0].rules[0].condition'))
  }
  age.rules[0].condition = { operator: 'between', value: 'not a range' }
  expectJSON(definition(age), error('questions[0].rules[0].condition.value'))
  age.config = null
  expectJSON(definition(age), error('questions[0].config'))
})

test('single and multiple choice comparisons resolve to their value controls', () => {
  for (const kind of ['single', 'multi']) {
    const q = question('q_choice', kind)
    q.rules.push(rule('r_choice', { operator: kind === 'single' ? 'eqOption' : 'containsAll', value: kind === 'single' ? 'yes' : ['yes'] }))
    const actual = resolveEditorError(definition(q), error('questions[0].rules[0].condition.value'))
    assert.equal(actual.field, 'questions[0].rules[0].condition.value')
    assert.equal(actual.exact, true)
    assert.match(actual.label, /选项/)
  }
})

test('resolving an error does not rewrite the draft or backend diagnostic', () => {
  const q = question('q_age'); q.rules.push(rule('r_age', { operator: 'between', value: { min: 5, max: 2 } }))
  const def = definition(question('q_note', 'text'), q)
  const issue = error('questions[0].rules[9].condition.value', { questionId: 'q_age', ruleId: 'r_age' })
  const before = JSON.stringify({ def, issue })
  const freeze = value => {
    if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value) }
    return value
  }
  freeze(def); freeze(issue)
  const actual = resolveEditorError(def, issue)
  assert.equal(actual.field, 'questions[1].rules[0].condition.value.min')
  assert.equal(JSON.stringify({ def, issue }), before)
})

test('the JSON rendering guard rejects null containers and malformed records before component rendering', () => {
  const q = question('q_age'); q.rules.push(rule('r_age'))
  const valid = definition(q)
  assert.equal(canRenderDefinition(valid), true)
  for (const mutate of [
    def => { def.description = null },
    def => { def.allowAnonymous = 'yes' },
    def => { def.entryQuestionId = {} },
    def => { def.questions = null },
    def => { def.questions[0] = null },
    def => { def.questions[0].id = 1 },
    def => { def.questions[0].kind = 'unknown' },
    def => { def.questions[0].required = null },
    def => { def.questions[0].config = null },
    def => { def.questions[0].rules = null },
    def => { def.questions[0].rules[0] = null },
    def => { def.questions[0].rules[0].condition = null },
    def => { def.questions[0].rules[0].condition = { allOf: null } },
    def => { def.questions[0].rules[0].condition = { anyOf: [null] } },
  ]) {
    const malformed = plain(valid); mutate(malformed)
    assert.equal(canRenderDefinition(malformed), false)
  }
  const choice = question('q_choice', 'single')
  for (const options of [null, [{ id: 'yes', label: null }], [null], [{ id: 1, label: '是' }]]) {
    choice.config.options = options
    assert.equal(canRenderDefinition(definition(choice)), false)
  }
})

test('the rendering guard preserves semantic failures for backend diagnostics without changing the import', () => {
  const age = question('q_duplicate')
  age.title = ''; age.config.min = 100; age.config.max = 1; age.config.integerOnly = 'invalid'
  age.config.unknown = true
  age.rules.push({ id: '', priority: 'not an integer', target: 'missing_question', condition: { allOf: [], anyOf: [], extra: true } })
  const choice = question('q_duplicate', 'multi')
  choice.config.options[0].label = ''; choice.config.options[1].id = choice.config.options[0].id
  choice.config.minSelected = 5; choice.config.maxSelected = -1
  const def = definition(age, choice)
  def.title = ''; def.entryQuestionId = 'missing_entry'; def.extra = { retained: true }
  const before = JSON.stringify(def)
  const freeze = value => {
    if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value) }
  }
  freeze(def)
  assert.equal(canRenderDefinition(def), true, 'the guard must not replace backend validation')
  assert.equal(JSON.stringify(def), before)
})

test('the rendering guard bounds deep, cyclic and oversized condition trees', () => {
  const q = question('q_age'); q.rules.push(rule('r_age'))
  let deep = { operator: 'eq', value: 1 }
  for (let index = 0; index < 40; index += 1) deep = { allOf: [deep] }
  q.rules[0].condition = deep
  assert.equal(canRenderDefinition(definition(q)), false)
  const cycle = { allOf: [] }; cycle.allOf.push(cycle)
  q.rules[0].condition = cycle
  assert.equal(canRenderDefinition(definition(q)), false)
  q.rules[0].condition = { anyOf: Array.from({ length: 10_001 }, () => ({ operator: 'eq', value: 1 })) }
  assert.equal(canRenderDefinition(definition(q)), false)
  // More than the protocol's 4 levels still reaches backend validation when
  // the tree is safe to display; the guard is not a duplicate domain validator.
  let tooDeepForPublication = { operator: 'eq', value: 1 }
  for (let index = 0; index < 5; index += 1) tooDeepForPublication = { allOf: [tooDeepForPublication] }
  q.rules[0].condition = tooDeepForPublication
  assert.equal(canRenderDefinition(definition(q)), true)
})
