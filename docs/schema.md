# MongoDB 数据结构与访问边界

本文件根据 `backend/app/database.py`、`repositories.py`、`services.py`、`security.py`、`statistics.py` 与 `main.py` 的实际实现编写。初始化创建真实校验器和索引；是否已在指定环境成功执行、并发正确性和性能结论，需要对应运行证据，不能由本文替代。

正文保留第一阶段四集合的核心结构及历史验证说明。第二阶段另增 `questions`、`question_versions`，当前初始化共创建六集合；见[独立题目与版本](phase2-question-versions.md)。随后仅给 `surveys/survey_versions`增加可选来源数组，原字段约束和 `users/responses`校验器保持不变；具体结构与兼容规则见[问卷固定使用题目版本](phase2-question-reuse.md)。旧文档无需回填或将 `schemaVersion` 改成2。使用情况查询再给草稿和发布集合各增加两个复合多键索引，不增加业务字段或使用关系集合；设计与实际计划见[题目使用情况查询](phase2-question-usage.md)。恢复功能再为 `question_versions`增加成对可选的 `operation/previousCurrentVersionId`，不增集合、索引或回填；约束和旧格式说明见[恢复题目旧版本](phase2-question-restore.md)。

## 1. 集合与生命周期

| 集合 | 保存内容 | 生命周期与主要访问 |
|---|---|---|
| users | 登录账号、规范化用户名、密码哈希、注册时间 | 按 usernameKey 登录；按主键核验当前账号 |
| surveys | 所有者、草稿、发布指针、收集控制与协调序号 | 所有者列表、公开标识解析、草稿 CAS、设置与提交协调 |
| survey_versions | 不可变发布快照、来源修订、发布幂等字段 | 按问卷版本查定义；发布时只插入，不原地更新 |
| responses | 一次正式提交、路径、有效答案、防重与隐私字段 | 按问卷/版本/固定序号范围统计、分页和导出 |

题目、选项、约束和规则有共同发布生命周期且有上限，因此嵌入 draft/definition。答卷独立增长，因此不放进问卷中的无限数组，也不追加无限答卷 ID 列表。版本与答卷通过引用连接，跨集合原子性由事务保证。

业务实体 ID 使用服务端 UUID 字符串；publicId 使用独立随机字符串。MongoDB 日期为 UTC BSON Date；AsyncMongoClient 配置 tz_aware，API 返回带时区时间。整型数字答案由提交服务显式转换 BSON Int64，非整数按 double 保存。

## 2. users

| 字段 | 存储与含义 |
|---|---|
| _id | 非空字符串，服务端 UUID |
| username | 3—32 码点的展示用户名 |
| usernameKey | NFKC、去首尾空白后的用户名再 casefold，用于唯一查找；允许折叠扩展长度至 128 |
| passwordHash | Argon2 库生成的密码哈希，不对外返回 |
| createdAt | 注册时间，BSON Date |
| schemaVersion | 正整数，目前为 1 |

注册和登录共用用户名规范化。密码本身不去空白、不更改字符。public_user 只返回 id、username、createdAt；令牌不包含密码哈希。

注册直接插入由服务端生成的新文档，最终以usernameKey上的真实唯一索引uq_users_username_key仲裁竞争；不能只靠“先查重、再插入”保证唯一。第十五轮验证两组8个不同UUID、相同规范查找键同时准备插入，每组只有1个新账号、其余409/USERNAME_EXISTS；不是由相同_id误触发冲突。保存的展示名、注册时间及密码属于实际胜者，败者密码不能登录，既有账号与问卷/版本/答卷全文不变。4个不同规范查找键并发均成功，说明没有把不同账号错误合并。见[15-并发注册.md](开发记录/15-并发注册.md)；本轮只验证既有索引与实现，没有修改Schema。

## 3. surveys

| 字段组 | 字段与含义 |
|---|---|
| 标识/归属 | _id、ownerId、publicId |
| 草稿 | draft、draftRevision；修订号为非负整数 |
| 发布指针 | currentVersionId（未发布为 null）、lastVersionNo（初始 0） |
| 收集状态 | status=draft/open/closed、deadlineAt（BSON Date 或 null） |
| 收集政策 | allowMultiple、policyLocked；首份正式答卷后政策锁定 |
| 设置并发 | settingsRevision；收集设置变更或关闭成功后增加 |
| 提交协调 | submitSequence；每次成功新提交的问卷级递增序号 |
| 时间/格式 | createdAt、updatedAt、schemaVersion |

draft 与发布 definition 都包含 title、description、allowAnonymous、entryQuestionId、questions。草稿允许空 questions、null 入口、尚未写完的题目标题和选项标签，仍受 BSON 结构及规模约束。预览和发布必须通过更严格的领域检查。

草稿更新条件为 `_id + ownerId + draftRevision`，成功后 `$inc draftRevision`；不能先读出再无条件覆盖。settingsRevision 与 draftRevision 分开，submitSequence 不代替编辑修订号。

expired 是 API 根据服务端时间计算出的展示状态，不写入 status 枚举。截止属于实时收集控制，对全部版本生效。关闭只允许 open→closed，当前没有重开接口。

## 4. survey_versions

| 字段 | 含义 |
|---|---|
| _id、surveyId | 发布版本主键及所属问卷 |
| versionNo | 同一问卷从 1 开始的版本号 |
| sourceDraftRevision | 本次发布的草稿修订号 |
| definition | 完整题目、选项、匿名政策、约束、默认边、规则与入口 |
| publishKey | 本问卷内的发布幂等标识 |
| publishRequestDigest | 对预期草稿修订和预期当前版本的请求摘要 |
| publishedAt、schemaVersion | 发布时间及格式版本 |

发布事务内再次读取归属、draftRevision 和 currentVersionId，检查完整定义，插入新版本，再有条件切换主问卷指针。两次写入在同一事务中；失败不能留下一个已切换却不存在的目标版本。事务回调可能重跑。

第十六轮独立核对：draftRevision始终为1、定义不变，四种当前版本预期不符均409，users/surveys/survey_versions/responses完整文档不变；两个事务都通过原V1检查并准备插入V2，再同时放行真实插入，结果仅一个V2成功，另一方409/PUBLISH_CONFLICT且没有其publishKey对应的孤立快照。更新当前版本后，失败方沿用自己的key可发布V3，编号为1/2/3，原V1/V2与实名答卷全文不变。每次成功只新增目标版本并更新问卷的currentVersionId、lastVersionNo、updatedAt。见[16-并发发布.md](开发记录/16-并发发布.md)；只验证既有事务与索引，没有修改MongoDB Schema。

版本不可变由仓储边界和 API 权限实现，不是普通 JSON Schema 自带的“不可修改”能力。当前仓储提供 insert_version，没有版本更新或删除接口。复制历史定义应形成新草稿及后续新版本。

## 5. responses

| 字段 | 含义 |
|---|---|
| _id、surveyId、versionId | 回执主键、问卷与实际填写快照 |
| participantKey | 问卷范围的 HMAC 参与标识，用于去重、人数与限次；管理者不可见 |
| submissionKey | 一次提交的幂等标识 |
| payloadDigest、canonicalizationVersion | 规范化有效答案、版本、匿名选择的摘要与规范版本，目前为 1 |
| anonymous | 当次匿名选择，严格布尔值 |
| identity | 仅实名答卷保存 `{userId,usernameAtSubmit}`；匿名答卷完全省略 |
| answers | 有效非缺失答案数组，按实际路径顺序保存 |
| path | 已到达题目 ID 数组，不包含 END，元素唯一 |
| acceptedSeq、acceptedAt | 问卷级提交序号和服务端接受时间 |
| oneTimeParticipantKey | 禁止同人多填时等于 participantKey；允许多填时省略，不能写 null |
| schemaVersion | 格式版本，目前为 1 |

答案元素是 `{questionId,kind,value}`。single 保存选项 ID；multi 保存非空、无重复选项 ID 数组；text 保存去首尾空白后的非空文字；number 保存有限安全范围内数值。

```json
{
  "answers":[
    {"questionId":"q_age","kind":"number","value":17},
    {"questionId":"q_hobby","kind":"multi","value":["read","sport"]}
  ],
  "path":["q_age","q_minor","q_hobby","q_score","q_note"],
  "acceptedSeq":1
}
```

示例为结构片段，不是可以绕过必填字段直接插入的完整 BSON 文档。到达但未答的选填题仅在 path 中；跳过题既不在 path 也不在 answers。

## 6. 数据库校验器的实际范围

四集合均设置 `validationLevel=strict`、`validationAction=error`，主要对象使用 additionalProperties=false。响应使用 oneOf 区分题型及答案值类型；匿名/实名 oneOf 要求匿名没有 identity、实名有 identity。

第十七轮直接在隔离库核对实际strict/error选项，绕过HTTP/领域校验，但不绕过MongoDB validator。四集合60个必要字段路径在缺字段插入、缺字段替换、$unset三种形式下180次被121拒绝；其他三集合19种错误结构三形式57次拒绝。每次失败都比较四集合完整文档，替换/更新同时携带的合法标记修改也没有部分生效。字段可为null不等于可以缺失：surveys的currentVersionId、deadlineAt省略被拒，合法草稿克隆保留null可写入。

实名identity及其子字段删除被拒；真实匿名答卷克隆可以省略identity、oneTimeParticipantKey，也可以携带等于participantKey的可选一次性字段。数字0分别以BSON double和long成功保存，并读回核对类型。各集合合法插入/替换/更新的正向例只作用于新克隆，删除自建克隆后原四集合全文恢复；不代表业务接口提供改答、版本改写或Schema迁移。17项专项及内部237次拒绝的范围见[17-数据库字段约束.md](开发记录/17-数据库字段约束.md)，本轮没有修改Schema。

responses 另有 `$expr`：答案 questionId 不重复；答案 ID 是 path 的子集；oneTimeParticipantKey 要么不存在，要么等于 participantKey。path 非空、最多 100 项且唯一。数字答案的 BSON 类型只允许 int/long/double，明确排除 bool，并具有安全范围边界。

数据库不读取其他集合来验证：问卷归属、版本确属问卷、答案选项属于版本题目、题型和动态限制、实时多填政策、路径是否由规则正确推导。它也不自动保证 answers 数组按路径排序。这些由服务和领域层生成及验证。

数据库结构上限有意作为稳定兜底，部分文字长度和 ID 范围比领域发布协议宽；不能绕过领域发布检查，把能写入草稿当成可以正式发布。条件对象的具体语法、组合深度和节点数也由领域层检查。

`init_database` 不 drop、不清空、不自动修复旧数据。已有集合使用 collMod 更新校验器并创建索引；这不会追溯性地把历史非法文档变为合法文档。索引冲突或校验器配置失败会使初始化失败，不能静默忽略。

## 7. 索引清单与代价

| 集合 | 固定索引名 | 键与约束/访问用途 |
|---|---|---|
| users | uq_users_username_key | usernameKey 唯一，抵御并发重复注册 |
| surveys | uq_surveys_public_id | publicId 唯一，公开入口查找 |
| surveys | ix_surveys_owner_created | ownerId、createdAt 降序、_id 降序，所有者列表 |
| survey_versions | uq_versions_survey_number | surveyId+versionNo 唯一 |
| survey_versions | uq_versions_publish_key | surveyId+publishKey 唯一，发布重放 |
| responses | uq_responses_submission | surveyId+participantKey+submissionKey 唯一 |
| responses | uq_responses_one_time_participant | surveyId+oneTimeParticipantKey 部分唯一 |
| responses | uq_responses_accepted_sequence | surveyId+acceptedSeq 唯一 |
| responses | ix_responses_version_sequence | surveyId+versionId+acceptedSeq 降序，固定范围和明细 |

部分过滤条件精确为 `{"oneTimeParticipantKey":{"$type":"string"}}`。不存在该字段的允许多填答卷不受这项唯一约束；字段生成必须依据事务内实时政策，客户端不能自行选择是否受约束。

索引增加磁盘空间和每次写入维护成本。实际扫描收益应记录 explain 的 nReturned、totalDocsExamined、totalKeysExamined 和访问计划；索引存在本身不等于已经证明性能提升。

2026年10月21日已补[索引执行计划实验](index-experiment.md)：同样的校验器/索引、独立库1,000问卷/20,000合成答卷，五类实际查询的默认计划均命中目标索引，三种方式返回结果一致。所有者页30对1,000、答卷页31对20,000、完整统计80对20,000份扫描文档；前两者消除阻塞排序。匿名筛选仍需读取80份后过滤40份。本次未新增索引或改业务查询，实际耗时和分布边界见实验报告；不能由读取实测推断写入吞吐。

## 8. 提交、状态和事务

Repository.transaction 使用 PyMongo Async 会话及 with_transaction，read concern 为 snapshot、write concern 为 majority、主节点读取，设置事务总超时及提交时间上限。部署必须是副本集；单节点副本集用于本地事务，并不提供高可用能力。

提交先恢复同键成功记录，再检查新填写凭据和固定版本。新写入事务中 `submission_gate` 对同一 surveys 记录递增 submitSequence 并锁定政策；取得真实变更写之后再次读取服务端接受时间并检查截止，然后写入答卷。关闭和设置修改也写同一主记录，因此能与提交发生实际写冲突，而非只进行快照读取。

失败事务回滚序号、政策锁和答卷；重试重新通过 gate 并检查当前截止。幂等恢复优先查同请求的真实结果，不能只根据某个唯一索引的错误名称猜测是否已成功。无法确认的新结果返回待确认错误，客户端保留同键重试。

## 9. 查询、统计和隐私

统计以 surveyId、versionId、acceptedSeq≤asOfSeq 为范围；取得当前序号和聚合在同一快照事务中进行。答卷只追加，因此固定边界下可分页和重复导出。当前不提供正式答卷改答、删除或改变历史匿名状态的接口。

第九轮增加respondentType=all/anonymous/named。statistics.build_response_scope统一生成统计、明细、导出的MongoDB范围；非all额外限定anonymous=true/false。过滤发生在聚合facet与分页前，所有R/V/A/U/S、均值和人数均来自筛选集合；全局asOfSeq不变。匿名/实名集合的份数可以相加，独立人数因同人可跨集合提交而不可相加。没有改变存储结构、添加集合或增加索引；新增索引是否值得需结合后续真实执行计划和性能实验判断。

MongoDB 聚合分别计算到达、已答、到达未答和跳过集合；多选展开只用于选项计数，数值均值单独计算。代码核验 `0≤A≤V≤R`、`R=A+U+S`、单项次数≤A、单选分布总和=A。默认按版本统计，不自动跨语义变化的版本合并。

`response_view` 白名单只返回回执、答案、路径、序号；实名另加提交时姓名。participantKey、oneTimeParticipantKey、摘要和原始用户 ID 不作为管理响应字段。统计只返回参与人数，不返回参与标识；CSV 同样按匿名规则输出，并对可能被表格软件解释为公式的内容作导出转义，不修改数据库原值。

## 10. 已有测试与证据边界

`tests/test_database.py` 已定义真实 MongoDB 组件用例：重复初始化、空草稿、六份样例统计、固定序号/空统计、非法 BSON、唯一键和部分索引。它直接写受控组件样例，不等于经过注册、填写、提交服务的完整端到端流程。实际运行环境、通过/失败数量及证据以测试报告为准。

领域与 BSON 边界已执行的 44 项 unittest 不连接数据库，不能用于宣称事务、集合校验器或查询计划已经通过真实验证。BSON 回归实际编码再解码大于 32 位的数字边界、条件和优先级，确保 Int64 作为 int 子类不会被误拒绝，同时继续排除 bool。
