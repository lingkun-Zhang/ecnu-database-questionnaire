# 第一阶段 API 说明

本文依据2026年10月20日的backend/app/main.py、services.py、statistics.py及实际请求模型整理，供离线查阅。当前有21个API操作、20条路径。机器可读快照见[api-openapi.json](api-openapi.json)，运行中的交互文档为http://127.0.0.1:8000/docs。

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

同一规范化用户名并发注册时只创建一个账号；其余返回409/USERNAME_EXISTS，不覆盖成功账号的密码、展示名或注册时间。展示名保留胜者输入经过NFKC和去首尾空白后的结果；casefold仅用于内部查找键。例如Straße_1与STRASSE_1具有相同查找键。等价用户名可使用胜者密码登录，失败注册请求各自不同的密码返回401/INVALID_CREDENTIALS。两组8请求及4个不同规范名并发的真实结果见[checkpoint-15.md](checkpoint-15.md)，接口和OpenAPI未变化。

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

草稿修订正确也不能忽略expectedCurrentVersionId：当前已为V2时，传旧V1、过期null、不存在ID或其他问卷的版本ID，新发布均409/PUBLISH_CONFLICT且不写入。失败尝试没有占用publishKey，更正当前版本预期后可沿用该key正常发布；已有成功记录的key则绑定原预期参数，更改参数409/IDEMPOTENCY_CONFLICT。两个不同key竞争同一发布基线时只新增一个版本，失败方刷新后可继续；后续版本发布不影响旧成功key按原参数恢复原快照。实际测试和完整文档核对见[checkpoint-16.md](checkpoint-16.md)，本轮接口及OpenAPI不变。

草稿可以保存未完成题目，但预览、发布要通过完整定义检查。预览请求可使用当前未保存的definition；不等于正式发布或持久化答卷。

第十一轮所有者预览在原求值结果上增加 `warnings` 数组。每项固定包含 `code,message,questionId,ruleId,field,relatedRuleIds`；无相关规则时 `relatedRuleIds=[]`，题目级提示的 `ruleId=null`。例：

```json
{"code":"shadowed_rule","message":"条件的全部有效答案已被较高优先级规则覆盖，此规则不会被命中。","questionId":"q_gate","ruleId":"r_shadow","field":"questions[0].rules[2].condition","relatedRuleIds":["r_first"]}
```

五种code为 `unreachable_question`（结构不可达）、`duplicate_rule_condition`（规范化后重复）、`shadowed_rule`（完全遮蔽）、`overlapping_rules`（部分重叠）、`unsatisfiable_rule`（有效答案范围无解）。规则按优先级分析，field仍采用原定义数组索引；`relatedRuleIds`只引用实际先匹配且有效的前规则。适用范围见protocol.md。`warnings=[]`仅表示在支持范围内未发现提示。

提示不进入 `errors`，不改变HTTP状态、路由或发布成功条件。非法定义仍422/INVALID_DEFINITION及fieldErrors；鉴权仍先于分析。公开元信息、start、填写evaluate、submit及发布回执不新增warnings；草稿、版本和答卷不存储它。接口数量仍为21操作/20路径。现有OpenAPI未声明预览响应模型，因此该JSON快照的路径/模型无需改变，不能把它当作包含输出字段的完整契约；本节和真实专项证据补充说明响应。详见[checkpoint-11.md](checkpoint-11.md)。

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

等价恢复只针对有效提交语义：原本空白未答的选填可用空串/null/省略表示，已回答的选填改成未答则冲突；多选换顺序可恢复，换集合或重复选项不行；路径外缓存须属于卷内已知题。仍需有效登录认证，能够缺失/过期的是填写凭据。11项专项与完整文档核对见[checkpoint-13.md](checkpoint-13.md)，本轮未改变接口。

收到SUBMISSION_OUTCOME_UNKNOWN或网络确认失败时，保留原key、原body和填写凭据，重新登录后重发原请求。已有成功记录的恢复优先于新填写凭据和收集状态检查，因此已关闭、凭据过期或缺失也可恢复原成功；这不允许创建新的过期答卷。不要为确认同一次提交更换key，否则属于另一次尝试。

客户端待恢复记录保存在同标签页、按问卷和账号分隔的sessionStorage中；当前实现不承诺关闭标签页或跨设备恢复。

## 6 收集控制

修改截止示例：

```json
{"expectedSettingsRevision":0,"deadlineAt":"2026-10-21T23:59:00+08:00"}
```

deadlineAt=null清除截止；省略表示不修改。allowMultiple必须是真正布尔值，显式null不合法。首次正式答卷后多填政策锁定，不能由修改政策绕过既有跨版本限次。关闭请求使用当前expectedSettingsRevision；目前不支持重开。关闭问卷后可以保存草稿、形成新版本，但不会恢复收集。

首次提交已持有事务gate时，并发的allowMultiple修改不会抢先改写该次提交所用的政策：提交成功后修改返回409/POLICY_LOCKED，settingsRevision不因这次失败递增；若提交因截止回滚，原先未锁定的政策修改可成功，提交序号和政策锁不残留。带allowMultiple及deadlineAt的混合修改被拒时，两字段均不改；政策锁定后，合法的仅deadlineAt修改仍可成功。两种原政策、回滚与后续限次的实测见[checkpoint-14.md](checkpoint-14.md)，本轮接口及OpenAPI未变化。

新提交必须在服务端接受时间严格早于截止时成功；恰好到达截止也拒绝。settingsRevision与draftRevision用途不同，不能混用。

## 7 统计、明细与导出

三个查询均固定versionId；asOfSeq为非负问卷提交序号，缺省取当次读取的已提交边界。先读stats取得asOfSeq，再带同一值请求明细和导出，才能保持相同范围；不传边界的多次请求可能看到新增答卷。asOfSeq不能超过当前已提交序号，版本数与问卷级序号不等价。

三个查询统一支持respondentType=all（默认全部）、anonymous（仅匿名）、named（仅实名），其他值返回422/INVALID_REQUEST。在MongoDB读取和聚合前，使用相同的问卷、版本、序号上界及anonymous布尔条件过滤；不能只在前端隐藏明细行。stats与responses回传respondentType。页面切换填写方式保留当前序号范围，点击“刷新范围”才纳入新答卷。

stats返回surveyId、versionId、versionNo、respondentType、asOfSeq、totalResponses、uniqueParticipants、anonymousResponses、namedResponses和questions。逐题使用R（筛选集合内答卷数）、V（到达）、A（已答）、U（到达未答）、S（跳过），另有answeredResponses、answeredParticipants和uniqueParticipants；选择题有选项次数、独立人数、百分比及totalSelections，数字题有sum/avg。空数字集合avg=null，0是有效值；详细字段与公式见[schema.md](schema.md)及[design-v4.md](design-v4.md)第八节。

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

编辑器对预览/发布INVALID_DEFINITION中的字段错误提供定位、聚焦和关联说明；定义变化后旧错误失效，无可视化控件时转JSON高级编辑。该行为不新增接口；没有fieldErrors的通用保存错误仍只显示普通说明。完整页面验证及范围见[checkpoint-10.md](checkpoint-10.md)。

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
