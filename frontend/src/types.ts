export type Kind = 'single' | 'multi' | 'text' | 'number'
export type Answer = string | number | string[] | null
export type Answers = Record<string, Answer>
export type RespondentType = 'all' | 'anonymous' | 'named'
export interface Option { id: string; label: string }
export interface Condition { operator?: string; value?: unknown; allOf?: Condition[]; anyOf?: Condition[] }
export interface Rule { id: string; priority: number; condition: Condition; target: string }
export interface Question { id: string; kind: Kind; title: string; required: boolean; config: { options?: Option[]; minSelected?: number | null; maxSelected?: number | null; minLength?: number | null; maxLength?: number | null; min?: number | null; max?: number | null; integerOnly?: boolean }; defaultTarget: string; rules: Rule[] }
export interface Definition { title: string; description: string; allowAnonymous: boolean; entryQuestionId: string | null; questions: Question[] }
export interface User { id: string; username: string; createdAt: string }
export interface QuestionSource { instanceId: string; questionId: string; questionVersionId: string }
export interface QuestionContent { kind: Kind; title: string; config: Question['config'] }
export interface ReusableQuestion { id: string; currentVersionId: string; lastVersionNo: number; createdAt: string; updatedAt: string }
export interface QuestionVersion { id: string; questionId: string; versionNo: number; parentVersionId: string | null; content: QuestionContent; createdAt: string; operation?: 'restore'; previousCurrentVersionId?: string }
export interface QuestionUsage { surveyId: string; surveyTitle: string; surveyStatus: string; instances: Array<{instanceId: string; questionVersionId: string}>; draftRevision?: number; surveyVersionId?: string; versionNo?: number; publishedAt?: string; isCurrent?: boolean }
export interface Survey { id: string; publicId: string; draft: Definition; draftQuestionSources?: QuestionSource[]; draftRevision: number; currentVersionId: string | null; lastVersionNo: number; status: string; deadlineAt: string | null; allowMultiple: boolean; policyLocked: boolean; settingsRevision: number; createdAt: string; updatedAt?: string }
export interface VersionSummary { id: string; surveyId?: string; versionNo: number; sourceDraftRevision: number; publishedAt: string; title?: string }
export interface Version extends VersionSummary { definition: Definition; questionSources?: QuestionSource[] }
export interface Evaluation { path: string[]; activeAnswers: Answers; trace: Array<Record<string, unknown>>; errors: FieldError[]; blockedAt: string | null; complete: boolean; provisional: boolean; warnings?: DesignWarning[] }
export interface FieldError { questionId?: string; ruleId?: string; field?: string; code?: string; message: string }
export interface DesignWarning extends FieldError { relatedRuleIds?: string[] }
export interface Receipt { receiptId: string; versionId: string; acceptedAt: string; anonymous: boolean; status: string }
export interface QuestionStats { questionId: string; kind: Kind; title: string; R: number; V: number; A: number; U: number; S: number; options?: Array<{id: string; label: string; count: number; uniqueParticipants?: number}>; sum?: number | null; avg?: number | null; uniqueParticipants?: number }
export interface Stats { versionId: string; versionNo?: number; respondentType: RespondentType; asOfSeq: number; totalResponses: number; uniqueParticipants: number; anonymousResponses: number; namedResponses?: number; questions: QuestionStats[] }
export interface ResponseItem { receiptId: string; versionId: string; acceptedAt: string; anonymous: boolean; respondentName?: string; acceptedSeq: number; answers: Array<{questionId: string; kind: Kind; value: Answer}>; path: string[] }
export const kindNames: Record<Kind, string> = { single: '单选题', multi: '多选题', text: '文本填空', number: '数字填空' }
export const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x)) as T
export const uid = (prefix: string) => `${prefix}_${crypto.randomUUID().replaceAll('-', '').slice(0, 12)}`
export function newQuestion(kind: Kind): Question {
  return { id: uid('q'), kind, title: '', required: true, config: kind === 'single' || kind === 'multi' ? { options: [{id: uid('o'), label: '选项一'}, {id: uid('o'), label: '选项二'}], ...(kind === 'multi' ? {minSelected: 1, maxSelected: null} : {}) } : kind === 'text' ? {minLength: null, maxLength: 1000} : {min: null, max: null, integerOnly: false}, defaultTarget: 'END', rules: [] }
}
export function exampleDefinition(): Definition {
  return {title: '学习与生活小调查', description: '用一份短问卷，了解不同年龄的学习与生活。约需 2 分钟。', allowAnonymous: true, entryQuestionId: 'q_age', questions: [
    {id:'q_age',kind:'number',title:'你的年龄是？',required:true,config:{min:0,max:120,integerOnly:true},defaultTarget:'q_adult',rules:[{id:'r_minor',priority:10,condition:{operator:'lt',value:18},target:'q_minor'}]},
    {id:'q_minor',kind:'text',title:'你正在参加什么学习或活动？',required:false,config:{minLength:1,maxLength:100},defaultTarget:'q_hobby',rules:[]},
    {id:'q_adult',kind:'single',title:'你目前的主要身份是？',required:true,config:{options:[{id:'student',label:'在校学生'},{id:'worker',label:'已参加工作'}]},defaultTarget:'q_hobby',rules:[]},
    {id:'q_hobby',kind:'multi',title:'闲暇时，你喜欢做什么？',required:true,config:{options:[{id:'read',label:'阅读'},{id:'sport',label:'运动'},{id:'music',label:'音乐'}],minSelected:1,maxSelected:2},defaultTarget:'q_score',rules:[]},
    {id:'q_score',kind:'number',title:'今天的状态，可以打几分？',required:false,config:{min:0,max:10,integerOnly:false},defaultTarget:'q_note',rules:[]},
    {id:'q_note',kind:'text',title:'还有什么想与我们分享？',required:false,config:{minLength:1,maxLength:100},defaultTarget:'END',rules:[]}
  ]}
}
