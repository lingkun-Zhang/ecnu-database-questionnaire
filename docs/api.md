# 第一阶段 API 说明

最新读取优化的兼容参数见文末第15节；[优化说明](phase2-optimization.md)记录输出变化和实测边界。

本文保留第一阶段21个API操作、20条路径的接口说明。第二阶段新增6个题目与版本操作，输入、输出及错误语义见[独立题目与版本](phase2-question-versions.md)。机器可读[OpenAPI快照](api-openapi.json)随当前应用更新，运行中的交互文档为http://127.0.0.1:8000/docs；以下第一阶段历史批次中的接口数量不包含新增操作。

草稿保存支持可选 `questionSources`，所有者草稿和发布版本可返回固定来源；省略、空数组、内容匹配和复制历史的兼容规则见[问卷固定使用题目版本](phase2-question-reuse.md)。绑定没有新增单独端点。

`GET /api/questions/{questionId}/usages`按题目系列或指定版本查询本人草稿、发布快照，参数与输出口径见[题目使用情况查询](phase2-question-usage.md)。

`POST /api/questions/{questionId}/restore`由服务端读取指定版本生成新恢复版；请求、元数据和重放规则见[恢复题目旧版本](phase2-question-restore.md)。恢复功能完成时共29个API操作、26条路径；共享与题库接入时为39个API操作、32条路径；跨问卷统计新增7个操作时为46个操作、39条路径；影响查询接入后当前47个操作、40条路径，以本次生成的OpenAPI为准。

下文JSON是契约示例，尖括号内为调用时替换的值，不冒充实际HTTP运行记录。真实执行结果见[test-report.md](test-report.md)及evidence/。OpenAPI快照由实际应用生成，但部分响应没有声明response_model，动态201/200和业务错误应结合本文阅读。

## 1 认证与共同约定

基础地址为http://127.0.0.1:8000/api。除health、register、login外，所有API均要求`Authorization: Bearer <accessToken>`，包括通过公开问卷标识读取定义。公开链接用于分发，不表示允许未登录填写。

写请求使用application/json。实体ID由服务端生成；survey_id是管理问卷ID，public_id是填写链接标识，versionId是发布快照ID，三者不能互换。JSON字段区分大小写，严格请求模型拒绝未知字段和不合类型的值。日期为带时区ISO 8601字符串，服务端转换为UTC。

填写求值与首次提交还要求`X-Fill-Token: <fillToken>`，由start签发并绑定账号、问卷、版本和有效期。提交另需1—128字符的`Idempotency-Key`。发布的幂等键是请求体publishKey，与提交请求头不同。

默认登录有效期43200秒、填写凭据7200秒，可由ACCESS_SECONDS、FILL_SECONDS配置。没有服务端logout或refresh-token接口；前端退出清除本地会话。

## 2 接口总表

下表路径省略/api。所有“所有者”入口先核对问卷归属；非所有者和不存在问卷均以404处理。

| 方法 | 路径 | 权限 | 请求与成功结果 |
|---|---|---|---|
| GET | /health | 无 | 数据库ping；200，status/database/mongoVersion/replicaSetRequired |
| POST | /auth/register | 无 | username/password；201，登录结果 |
| POST | /auth/login | 无 | username/password；200，登录结果 |
| GET | /auth/me | 登录 | 200，id/username/createdAt |
| POST | /surveys | 登录 | title，默认“未命名问卷”；201，新问卷 |
| GET | /surveys | 登录 | limit默认30、1—100；offset默认0、0—100000；200，items/offset/limit，仅本人 |
| GET | /surveys/{survey_id} | 所有者 | 200，问卷详情与当前草稿 |
| PUT | /surveys/{survey_id}/draft | 所有者 | expectedDraftRevision/definition；200，修订后的问卷 |
| POST | /surveys/{survey_id}/preview | 所有者 | definition/answers/mode；200，路径求值结果及非阻断warnings |
| POST | /surveys/{survey_id}/publish | 所有者 | expectedDraftRevision/expectedCurrentVersionId/publishKey；首次201，重放200，发布版本 |
| PATCH | /surveys/{survey_id}/settings | 所有者 | expectedSettingsRevision及deadlineAt/allowMultiple中的至少一项；200，问卷 |
| POST | /surveys/{survey_id}/close | 所有者 | expectedSettingsRevision；200，问卷 |
| GET | /surveys/{survey_id}/versions | 所有者 | limit默认100、1—100；offset默认0、非负；200，items，版本号降序 |
| GET | /surveys/{survey_id}/versions/{version_id} | 所有者 | 200，完整不可变版本定义 |
| GET | /survey/{public_id} | 登录 | 200，公开定义、当前版本、收集状态 |
| POST | /survey/{public_id}/start | 登录 | 无请求体；200，固定版本、定义及填写凭据 |
| POST | /survey/{public_id}/evaluate | 登录＋填写凭据 | versionId/answers；200，interactive求值结果 |
| POST | /survey/{public_id}/submit | 登录；新写入还需填写凭据 | versionId/anonymous/answers和幂等请求头；首次201，重放200，回执 |
| GET | /surveys/{survey_id}/stats | 所有者 | versionId必需，asOfSeq/questionId/respondentType可选；200，统计 |
| GET | /surveys/{survey_id}/responses | 所有者 | versionId必需，asOfSeq/questionId/cursor/limit/respondentType可选；200，答卷页 |
| GET | /surveys/{survey_id}/export | 所有者 | versionId必需，asOfSeq/respondentType可选；200，CSV附件 |

## 3 账号与常用响应

注册和登录请求为：

```json
{"username":"example_user","password":"ExamplePassword2026!"}
```

密码为8—128码点，不自动去空格。用户名先NFKC规范化并去首尾空白，规范化后3—32码点，只允许字母、数字和下划线；查找键再casefold，大小写等价用户名不能重复注册。

登录结果包含accessToken、expiresAt、tokenType="bearer"及user。user仅含id、username、createdAt，没有密码哈希。

同一规范化用户名并发注册时只创建一个账号；其余返回409/USERNAME_EXISTS，不覆盖成功账号的密码、展示名或注册时间。展示名保留胜者输入经过NFKC和去首尾空白后的结果；casefold仅用于内部查找键。例如Straße_1与STRASSE_1具有相同查找键。等价用户名可使用胜者密码登录，失败注册请求各自不同的密码返回401/INVALID_CREDENTIALS。两组8请求及4个不同规范名并发的真实结果见[15-并发注册.md](开发记录/15-并发注册.md)，接口和OpenAPI未变化。

问卷响应包含id、publicId、draft、draftRevision、currentVersionId、lastVersionNo、status、deadlineAt、allowMultiple、policyLocked、settingsRevision、submitSequence、createdAt、updatedAt。创建时草稿为空、draftRevision/settingsRevision/submitSequence均0、allowMultiple=false。

发布版本响应包含id、surveyId、versionNo、sourceDraftRevision、definition、publishedAt。公开定义接口返回publicId、status、deadlineAt、allowMultiple、versionId、versionNo、definition，不包含所有者内部信息。公开接口会将已截止的open计算为expired；管理问卷的存储状态仍为open。

## 4 保存与发布示例

创建问卷后，以其当前修订号保存。以下是最小合法数字问卷，其他题型与规则见[protocol.md](protocol.md)：

```json
{
  "expectedDraftRevision":0,
  "definition":{
    "title":"年龄调查",
    "description":"用于演示数字约束",
    "allowAnonymous":true,
    "entryQuestionId":"q_age",
    "questions":[{
      "id":"q_age","kind":"number","title":"年龄","required":true,
      "config":{"min":0,"max":120,"integerOnly":true},
      "defaultTarget":"END","rules":[]
    }]
  }
}
```

成功后draftRevision为1。首次发布请求：

```json
{"expectedDraftRevision":1,"expectedCurrentVersionId":null,"publishKey":"publish-example-1"}
```

后续发布必须提交当前发布ID；旧草稿修订或旧当前版本预期不符时409/PUBLISH_CONFLICT。同publishKey和相同预期参数重放返回原版本，Idempotent-Replay=true；同键改变参数则409/IDEMPOTENCY_CONFLICT。

草稿修订正确也不能忽略expectedCurrentVersionId：当前已为V2时，传旧V1、过期null、不存在ID或其他问卷的版本ID，新发布均409/PUBLISH_CONFLICT且不写入。失败尝试没有占用publishKey，更正当前版本预期后可沿用该key正常发布；已有成功记录的key则绑定原预期参数，更改参数409/IDEMPOTENCY_CONFLICT。两个不同key竞争同一发布基线时只新增一个版本，失败方刷新后可继续；后续版本发布不影响旧成功key按原参数恢复原快照。实际测试和完整文档核对见[16-并发发布.md](开发记录/16-并发发布.md)，本轮接口及OpenAPI不变。

草稿可以保存未完成题目，但预览、发布要通过完整定义检查。预览请求可使用当前未保存的definition；不等于正式发布或持久化答卷。

第十一轮所有者预览在原求值结果上增加 `warnings` 数组。每项固定包含 `code,message,questionId,ruleId,field,relatedRuleIds`；无相关规则时 `relatedRuleIds=[]`，题目级提示的 `ruleId=null`。例：

```json
{"code":"shadowed_rule","message":"条件的全部有效答案已被较高优先级规则覆盖，此规则不会被命中。","questionId":"q_gate","ruleId":"r_shadow","field":"questions[0].rules[2].condition","relatedRuleIds":["r_first"]}
```

五种code为 `unreachable_question`（结构不可达）、`duplicate_rule_condition`（规范化后重复）、`shadowed_rule`（完全遮蔽）、`overlapping_rules`（部分重叠）、`unsatisfiable_rule`（有效答案范围无解）。规则按优先级分析，field仍采用原定义数组索引；`relatedRuleIds`只引用实际先匹配且有效的前规则。适用范围见protocol.md。`warnings=[]`仅表示在支持范围内未发现提示。

提示不进入 `errors`，不改变HTTP状态、路由或发布成功条件。非法定义仍422/INVALID_DEFINITION及fieldErrors；鉴权仍先于分析。公开元信息、start、填写evaluate、submit及发布回执不新增warnings；草稿、版本和答卷不存储它。接口数量仍为21操作/20路径。现有OpenAPI未声明预览响应模型，因此该JSON快照的路径/模型无需改变，不能把它当作包含输出字段的完整契约；本节和真实专项证据补充说明响应。详见[11-规则设计检查.md](开发记录/11-规则设计检查.md)。

## 5 填写与可靠提交

start返回fillToken、versionId、expiresAt、definition、allowMultiple、deadlineAt、publicId、versionNo。客户端应保留其快照；开始V1后再发布V2，不把旧填写改用V2约束。

evaluate请求为`{"versionId":"<versionId>","answers":{"q_age":18}}`，结果包含path、activeAnswers、trace、errors、blockedAt、complete、provisional。填写错误通常随200求值结果中的errors返回；HTTP层请求结构或凭据错误另有状态码。提交服务会重新执行final校验，不能信任客户端路径或忽略错误直接入库。

首次提交同时发送Authorization、X-Fill-Token、Idempotency-Key，请求体为：

```json
{"versionId":"<versionId>","anonymous":true,"answers":{"q_age":18}}
```

成功201回执形状如下；这是响应结构示例：

```json
{
  "receiptId":"<receiptId>","versionId":"<versionId>",
  "acceptedAt":"2026-10-20T08:00:00+00:00",
  "anonymous":true,"status":"accepted"
}
```

相同账号、问卷、提交键及有效内容重放返回200、相同回执、Idempotent-Replay=true。比较内容包含固定版本、匿名选择、规范化后的有效答案；多选排列、等价整数和路径外旧值按协议处理。同键修改有效内容409。没有成功记录的新写入，必须检查凭据、约束、实时关闭/截止和多填政策。

第十三轮实测进一步核对：即使V1/V2的定义完全一样，versionId不同也不能复用原成功标识，返回409/IDEMPOTENCY_CONFLICT；同标识改有效答案或将它变成缺必答、非法数值等无效内容，也不会覆盖原答卷。请求结构校验仍在服务恢复逻辑之前，不能据此认为任意非法JSON都返回409。

等价恢复只针对有效提交语义：原本空白未答的选填可用空串/null/省略表示，已回答的选填改成未答则冲突；多选换顺序可恢复，换集合或重复选项不行；路径外缓存须属于卷内已知题。仍需有效登录认证，能够缺失/过期的是填写凭据。11项专项与完整文档核对见[13-重复提交与内容冲突.md](开发记录/13-重复提交与内容冲突.md)，本轮未改变接口。

收到SUBMISSION_OUTCOME_UNKNOWN或网络确认失败时，保留原key、原body和填写凭据，重新登录后重发原请求。已有成功记录的恢复优先于新填写凭据和收集状态检查，因此已关闭、凭据过期或缺失也可恢复原成功；这不允许创建新的过期答卷。不要为确认同一次提交更换key，否则属于另一次尝试。

客户端待恢复记录保存在同标签页、按问卷和账号分隔的sessionStorage中；当前实现不承诺关闭标签页或跨设备恢复。

## 6 收集控制

修改截止示例：

```json
{"expectedSettingsRevision":0,"deadlineAt":"2026-10-21T23:59:00+08:00"}
```

deadlineAt=null清除截止；省略表示不修改。allowMultiple必须是真正布尔值，显式null不合法。首次正式答卷后多填政策锁定，不能由修改政策绕过既有跨版本限次。关闭请求使用当前expectedSettingsRevision；目前不支持重开。关闭问卷后可以保存草稿、形成新版本，但不会恢复收集。

首次提交已持有事务gate时，并发的allowMultiple修改不会抢先改写该次提交所用的政策：提交成功后修改返回409/POLICY_LOCKED，settingsRevision不因这次失败递增；若提交因截止回滚，原先未锁定的政策修改可成功，提交序号和政策锁不残留。带allowMultiple及deadlineAt的混合修改被拒时，两字段均不改；政策锁定后，合法的仅deadlineAt修改仍可成功。两种原政策、回滚与后续限次的实测见[14-首次提交与多填设置.md](开发记录/14-首次提交与多填设置.md)，本轮接口及OpenAPI未变化。

新提交必须在服务端接受时间严格早于截止时成功；恰好到达截止也拒绝。settingsRevision与draftRevision用途不同，不能混用。

## 7 统计、明细与导出

三个查询均固定versionId；asOfSeq为非负问卷提交序号，缺省取当次读取的已提交边界。先读stats取得asOfSeq，再带同一值请求明细和导出，才能保持相同范围；不传边界的多次请求可能看到新增答卷。asOfSeq不能超过当前已提交序号，版本数与问卷级序号不等价。

三个查询统一支持respondentType=all（默认全部）、anonymous（仅匿名）、named（仅实名），其他值返回422/INVALID_REQUEST。在MongoDB读取和聚合前，使用相同的问卷、版本、序号上界及anonymous布尔条件过滤；不能只在前端隐藏明细行。stats与responses回传respondentType。页面切换填写方式保留当前序号范围，点击“刷新范围”才纳入新答卷。

stats返回surveyId、versionId、versionNo、respondentType、asOfSeq、totalResponses、uniqueParticipants、anonymousResponses、namedResponses和questions。逐题使用R（筛选集合内答卷数）、V（到达）、A（已答）、U（到达未答）、S（跳过），另有answeredResponses、answeredParticipants和uniqueParticipants；选择题有选项次数、独立人数、百分比及totalSelections，数字题有sum/avg。空数字集合avg=null，0是有效值；详细字段与公式见[schema.md](schema.md)及[系统设计.md](系统设计.md)第八节。

所有计数、分母、均值和独立人数都在当前填写方式集合内计算。匿名份数和实名份数可相加得到全部份数，但独立人数不可直接相加：同一个账号可能同时提交过匿名与实名答卷。asOfSeq仍是全问卷序号，不变成筛选后的份数。

questionId对stats筛选单题汇总；对responses则只保留真正具有该题有效答案的答卷，不返回该题跳过或选填未答的答卷。

responses的limit默认30、1—100；cursor为正整数，使用上一页nextCursor，并保持同一versionId/respondentType/asOfSeq/questionId。返回items、respondentType、asOfSeq、nextCursor，按acceptedSeq降序；nextCursor=null表示结束。每条有回执字段、answers数组、path、acceptedSeq；实名额外返回respondentName，匿名省略。所有者也得不到内部参与标识、摘要或实名用户ID。

CSV使用UTF-8 BOM、UTC时间，按acceptedSeq升序，包含回执、版本、提交时间UTC、匿名、填写者及该版本题目列。单选/多选转为选项标签，多选用分号样式分隔；可能被表格软件解释为公式的单元格加前导单引号，原数据库不修改。单次筛选集合超过20000份拒绝导出。API附件名为survey-v{版本号}-seq{范围}.csv，非全部模式追加-anonymous或-named；前端采用该版本标题并在非全部模式标注“匿名”或“实名”。CSV保持全部题目列，不跟随单题显示选择删列。

## 8 错误契约与客户端处理

业务错误、请求模型校验和已处理数据库异常使用以下形状；不要假定框架所有404/405或未处理异常都具有此结构：

```json
{"code":"DRAFT_CONFLICT","message":"草稿已在其他页面修改，请刷新后比较","fieldErrors":[],"requestId":"<requestId>"}
```

领域fieldErrors可包含code/message/questionId/ruleId/field；请求模型错误采用field/message/code。正常中间件响应包含X-Request-ID，API响应设置Cache-Control:no-store。请求体上限：draft/preview为512KiB，其他POST/PUT/PATCH为1MiB。

编辑器对预览/发布INVALID_DEFINITION中的字段错误提供定位、聚焦和关联说明；定义变化后旧错误失效，无可视化控件时转JSON高级编辑。该行为不新增接口；没有fieldErrors的通用保存错误仍只显示普通说明。完整页面验证及范围见[10-编辑错误定位.md](开发记录/10-编辑错误定位.md)。

| HTTP | 常见code | 处理方式 |
|---|---|---|
| 400 | INVALID_AS_OF | 统计边界超过已提交序号；负数或非整数查询先由请求校验返回422/INVALID_REQUEST，明细/导出超范围使用422/INVALID_RANGE |
| 401 | UNAUTHENTICATED、INVALID_CREDENTIALS | 登录或核对凭据；不可保留旧账号视图 |
| 403 | INVALID_FILL_CREDENTIAL | 填写凭据用途/账号/版本不匹配；新尝试从入口开始 |
| 404 | NOT_FOUND、VERSION_NOT_FOUND | 不存在或无权限；不以猜测ID绕过所有权 |
| 409 | USERNAME_EXISTS、DRAFT_CONFLICT、PUBLISH_CONFLICT、SETTINGS_CONFLICT | 修正用户名或刷新对应修订后比较；避免盲目覆盖 |
| 409 | IDEMPOTENCY_CONFLICT、ALREADY_SUBMITTED、POLICY_LOCKED | 保留原提交含义；按限次和政策锁处理 |
| 409 | SURVEY_CLOSED、SURVEY_EXPIRED、FILL_CREDENTIAL_EXPIRED、NOT_PUBLISHED | 新写入受状态限制，已有成功记录另按恢复流程确认 |
| 413 | PAYLOAD_TOO_LARGE | 缩小请求 |
| 422 | INVALID_REQUEST、INVALID_USERNAME、INVALID_DEFINITION、INVALID_DOCUMENT、ANSWER_VALIDATION_FAILED、ANONYMOUS_DISABLED、INVALID_RANGE、EXPORT_TOO_LARGE | 展示具体错误并修改输入 |
| 503 | DATABASE_UNAVAILABLE、SUBMISSION_OUTCOME_UNKNOWN | 稍后重试；提交结果不明时必须使用原key及原body |
| 500 | DATA_INVARIANT | 统计一致性检查失败，需检查数据与实现，不能展示为正确统计 |

接口快照与本文在修改API后应一并更新。本文是实现说明，不新增接口，不改变现有运行数据。

## 第二阶段：题目共享使用授权

新增以下登录接口，完整权限矩阵、撤销与并发语义见[共享使用授权](phase2-question-sharing.md)。

| 接口 | 请求与作用 |
|---|---|
| `PUT /api/questions/{questionId}/shares` | 所有者提交`recipientUsername/active/expectedRevision`。用户名按既有注册规则规范化；首次`active=true,expectedRevision=0`创建，201；后续状态更新或成功重放200 |
| `GET /api/questions/{questionId}/shares` | 所有者读取授权列表，含当前状态及修订 |
| `GET /api/shared-questions` | 接收者读取当前有效共享题目，条目为既有题目白名单视图 |

两个列表使用`limit`（默认30，1—100）及可选`afterId`（非空、最多160字符）。`nextAfterId`是授权记录的分页标识，须原样传回，不拿题目ID替代；列表为实时读取，不保证跨页冻结快照。不存在或无权限的题目返回404；非所有者不能探查授权列表或收件账号。

授权响应包含`id/questionId/recipientId/recipientUsername/active/revision/permission/versionScope/createdAt/updatedAt`，不返回ownerId、请求摘要或协调序号。`permission/versionScope`固定为`use/series`；请求拒绝额外权限字段。`Idempotent-Replay`表示是否恢复当前最后一次成功请求。摘要绑定规范化后接收账号、题目、所有者、目标状态及预期修订；后续状态变化后的旧激活请求409，不能复活授权，不承诺永久重放所有旧操作。

既有题目详情、版本列表/详情和本人使用查询允许当前有效接收者读取；派生、恢复和转授权仍仅题目所有者。使用查询继续只返回调用者自己的问卷；统计、明细及实际`/export`接口继续检查问卷所有权。

撤销阻止新增来源和换版，服务器当前已保存的同问卷/同实例/同题目版本绑定可继续保存和发布；解绑保存后资格消失。已发布快照、旧填写和答卷不受追溯修改。新增错误包括422`INVALID_SHARE_RECIPIENT`、404`RECIPIENT_NOT_FOUND/SHARE_NOT_FOUND`、409`QUESTION_SHARE_CONFLICT`和503`QUESTION_SHARE_WRITE_OUTCOME_UNKNOWN`。用户名非法仍422`INVALID_USERNAME`，模型或分页非法仍422`INVALID_REQUEST`。不明结果保留原请求与预期修订重试，冲突后先读取当前状态再决定。

## 第二阶段：题库与固定版本成员

新增7个登录API操作、4条路径，详细请求、白名单及权限矩阵见[题库后端说明](phase2-question-banks.md)。

| 方法与路径 | 作用 |
|---|---|
| `POST /api/question-banks` | 提交`name/description`和`Idempotency-Key`创建私有题库；首次201，重放200 |
| `GET /api/question-banks` | 本人题库，`archived=false`或true分别筛选未归档/归档；`limit/afterId`游标分页 |
| `GET /api/question-banks/{bankId}` | 本人题库白名单视图 |
| `PUT /api/question-banks/{bankId}` | `name/description/archived/expectedRevision`整体设置元信息，说明省略时清空 |
| `GET /api/question-banks/{bankId}/questions` | 固定版本成员，返回一致快照的`bank/items/limit/nextAfterId` |
| `PUT /api/question-banks/{bankId}/questions/{questionId}` | `questionVersionId/expectedRevision`收录或明确换版 |
| `DELETE /api/question-banks/{bankId}/questions/{questionId}` | 查询参数`expectedRevision`移除成员；不删题目或问卷引用 |

新库修订为0；元信息和成员决定统一使用该库revision，每次成功明确决定加1。后续变更只支持当前最后一次成功请求的有限重放，响应均为题库视图并含`Idempotent-Replay`；创建身份则按创建键永久去重并返回同库当前视图。旧移除或换版不能覆盖后续决定。

成员的版本仍由现有`questionSources`保存到问卷；真正保存继续检查来源及当前使用权。题库不授权，撤销后已有成员返回`available=false`，仅保留整理指针和时间，不返回内容或head。归档保留本人读取和恢复、禁止成员变更，不影响独立题目使用权。题库冲突为409`BANK_CONFLICT`，归档成员变更409`BANK_ARCHIVED`，不存在成员404`BANK_ITEM_NOT_FOUND`，不明写入结果503`BANK_WRITE_OUTCOME_UNKNOWN`；旧的401/404/422边界继续适用。


## 跨问卷单题统计（2026-10-27）

以下路径省略`/api`，均要求登录。题目作者或有本人已发布来源的使用者可统计自己的来源；他人来源须由其问卷所有者向题目作者单独授权汇总。题目使用共享不替代此授权。响应字段及口径见[统计说明](phase2-question-statistics.md)。

| 操作 | 请求 | 结果与分页 |
|---|---|---|
| POST `/questions/{qid}/statistics/scopes` | 必需Idempotency-Key请求头1—128字符；body仅questionVersionId（可空）和respondentType=all/anonymous/named | 首次201，同key同body200重放；返回id、固定筛选、createdAt/expiresAt、来源/发布/实例数、hasForeignSources |
| GET `/questions/{qid}/statistics` | scopeId必需；afterVersionNo默认0，limit默认10/最大20 | scope、overview、versions、nextAfterVersionNo；不同题目版本分别列R/V/A/U/S与分布 |
| GET `/questions/{qid}/statistics/sources` | scopeId；afterId可选，limit默认30/最大100 | items、nextAfterId；他人来源没有真实问卷标题、surveyId或surveyVersionId |
| GET `/questions/{qid}/statistics/answers` | scopeId；questionVersionId可选；cursor可选；limit默认30/最大100 | 本人问卷的逐实例有效作答、nextCursor和coverage；签名游标绑定账号/范围/版本筛选 |
| GET `/questions/{qid}/statistics/export` | scopeId；questionVersionId可选 | UTF-8 BOM CSV，仅本人作答；最多20000行、8MiB（含BOM），超限422 EXPORT_TOO_LARGE，无静默截断 |
| GET `/surveys/{sid}/question-statistics-grants` | 无body，仅问卷所有者 | 从真实已发布来源推导他人题目候选及当前授权，草稿引用不能授权 |
| PUT `/surveys/{sid}/question-statistics-grants/{qid}` | active严格布尔；expectedRevision严格非负安全整数，初次0 | 200及Idempotent-Replay；服务端指定接收者为题目作者，仅aggregate；同修订竞争409 |

范围有效30分钟，创建回执有限保留7天。key同而body不同409 IDEMPOTENCY_CONFLICT；错账号/错题范围404；缺失或过期410 STATISTICS_SCOPE_EXPIRED；授权撤销、换修订或归属变化409 STATISTICS_SCOPE_INVALIDATED。未知写入结果503 STATISTICS_SCOPE_WRITE_OUTCOME_UNKNOWN或STATISTICS_GRANT_WRITE_OUTCOME_UNKNOWN，保留原参数重试；保存成功后读取失败只重试GET。授权状态409 STATISTICS_GRANT_CONFLICT后须明确重新核对，不能自动提高expectedRevision重发。错题/不存在的明细版本404 QUESTION_VERSION_NOT_FOUND，超出已固定单版本范围422；游标签名或绑定不符422 INVALID_STATISTICS_CURSOR。

概览responseCount是一份来源答卷一次，answeredResponseCount表示至少有效作答过一个本题实例；每版本responseCount不可直接相加推断概览，更不代表跨问卷唯一人数。数字返回sum/avg/min/max及最多10组直方图（末组含上界），选择题列全部固定选项count/percentage，多选totalSelections与A分开；文本汇总不泄露原文。明细/CSV与汇总共享固定来源和序号，但本人明细不包含他人汇总贡献。


## 作者专用修改影响查询（2026-10-27）

新增`GET /api/questions/{qid}/impact`，仅题目作者可用，原usages权限不变。可选questionVersionId；offset默认0且0—9007199254740991；limit默认20且1—100。返回checkedAt、scope=saved_references、own/others摘要及每份问卷的有界items页和nextOffset。本人条目带标题/ID/状态，他人仅HMAC引用代号与引用数，不含真实ID、标题、身份、实例标识、定义或答案。摘要区分去重问卷、草稿实例、当前发布实例及历史快照/实例；当前/历史按真实发布指针判断，闭卷仍可有当前版。一次请求snapshot一致，跨页实时偏移不冻结；保存前检查不代表全局锁或换版授权。字段、解释和边界见[影响说明](phase2-question-impact.md)。接入后当前共47个API操作、40条路径，OpenAPI由实际应用生成。

## 15 题目摘要、历史摘要与明细文案（2026-10-29）

GET /api/questions和GET /api/shared-questions支持includeCurrentVersion，默认false保持原格式；true时每项追加currentVersion:{id,questionId,versionNo,content:{kind,title}}，不返回config。根记录与当前摘要在同一快照核对，打开题目、选用或保存仍需实际权限，摘要不是使用凭证。

GET /api/surveys/{survey_id}/versions支持summary，默认false保持完整版本及来源；true仅返回id、surveyId、versionNo、sourceDraftRevision、publishedAt、title。limit/offset和所有权规则保持。完整内容仍通过指定版GET获取。

GET /api/questions/{question_id}/statistics/answers的本人选择题明细追加displayValue：single为固定版本选项文案，multi为对应文案数组。原value不变，数字/文本不使用该字段；CSV仍保留原ID。他人来源仍只参与汇总。没有新增API路径或操作，仍为47项操作、40条路径。
