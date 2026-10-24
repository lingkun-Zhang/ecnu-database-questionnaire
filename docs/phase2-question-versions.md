# 第二阶段第一步：独立题目与不可变版本

记录日期：2026年10月25日。本轮工作从10月24日的需求分析继续推进，代码与测试结果分别核实，本文不把设计目标当成已通过的验收结论。

## 本步解决什么

第一阶段的题目嵌在问卷草稿及发布快照中，局部题目ID不能标识跨问卷的同一道题。本步建立脱离问卷的题目身份、固定内容版本和直接来源关系，使一个私有题可以创建v1、从指定已有版本派生新版，并独立读取旧版与历史。

这为新增八项需求中的保存复用、发布保护、历史来源、多版本并存提供共同基础。题目尚未接入问卷，因此不能据此声称复用或发布保护链路已经完成；共享、恢复、使用情况、题库管理和跨问卷统计仍需后续实现。本轮不扩展前端、不改答卷和统计协议。

开发副本位于本仓库，本地分支为 `phase2-question-versions`，第一阶段基线为 `99b66d1e637fa3b4ecb13719c0230ecb7e958701`。桌面原项目、`phase1`原材料和本地差异保留，不用开发副本覆盖原目录。本轮没有推送GitHub或提交课程平台。

## 内容与MongoDB模型

版本内容只有 `kind/title/config`。四题型及选项、答案约束沿用原领域规则；`id/required/defaultTarget/rules`属于问卷使用实例，不进入独立题目。接口拒绝未知字段，不能通过悄悄丢弃路由字段实现“校验通过”。

内容先校验，再将省略的可选边界规范化为 `null`、省略的 `integerOnly`规范化为 `false`，等价数值使用统一表示。标题不自动去除空白，选项顺序保留。校验复用既有领域引擎，具体问卷的必答与跳转兼容仍在接入问卷时检查。

新增两个集合，旧 `users/surveys/survey_versions/responses`的定义保留：

| 集合 | 主要字段 | 原因与约束 |
|---|---|---|
| `questions` | `_id, ownerId, currentVersionId, lastVersionNo, creationKey, createdAt, updatedAt, schemaVersion` | 表达私有所有权和当前选用指针，不把无限增长的版本数组嵌入主记录 |
| `question_versions` | `_id, questionId, versionNo, parentVersionId, content, requestKey, requestDigest, createdAt, schemaVersion` | 每版保存完整内容及直接来源；v1父版本为空，派生版父版本必须属于同题 |

新文档的 `schemaVersion=1`表示这两种文档格式的首版，与课程阶段编号不同。它不要求旧数据改成2。新集合校验器约束必需字段、未知字段、BSON类型、编号及内容大小；跨集合来源存在性由后端检查。

唯一索引分别保障 `(ownerId, creationKey)`、`(questionId, versionNo)`和 `(questionId, requestKey)`。本人列表及历史查询按实际排序建立索引；主键索引用于读取具体版本。所有权检查先定位题目，再读取所属版本，避免通过版本ID绕过权限。

创建题目与v1在同一事务中完成。派生时，在同一事务读取题目及父版本、插入新版，并用 `expectedCurrentVersionId`比较并切换当前指针，同时递增 `lastVersionNo`。失败整体回滚，不留下半个题目、孤儿版本或已占用的请求标识。

`parentVersionId`与当前指针是两种关系。例如已有v1、v2，仍可基于v1创建v3：请求的父版本为v1，预期当前版本为v2；成功后当前指针为v3，v2继续存在。版本号表示创建顺序，不能把历史误画成v1→v2→v3的单链。

应用不提供版本更新或删除接口。MongoDB校验器不能禁止数据库管理员把合法内容替换成另一份合法内容，因此本步的不可变性依靠应用写入边界、权限和事务，不能称为校验器独自保证的绝对不可修改。

## 接口与冲突语义

全部接口要求登录，当前只允许所有者读取和派生。不存在与无权访问统一返回404，不披露他人私有题是否存在。

| 接口 | 用途 |
|---|---|
| `POST /api/questions` | 以完整内容创建题目和v1 |
| `GET /api/questions?limit=20&offset=0` | 有界读取本人题目系列列表 |
| `GET /api/questions/{questionId}` | 读取系列ID、当前版本、最后编号及时间 |
| `POST /api/questions/{questionId}/versions` | 基于指定父版本创建完整新版 |
| `GET /api/questions/{questionId}/versions` | 有界读取版本历史与父版本关系 |
| `GET /api/questions/{questionId}/versions/{versionId}` | 读取具体不可变版本内容 |

两种写接口均要求 `Idempotency-Key`请求头。创建请求示例：

```http
POST /api/questions
Authorization: Bearer <登录凭据>
Idempotency-Key: create-number-001
Content-Type: application/json

{"content":{"kind":"number","title":"本周运动几次？","config":{"min":0,"integerOnly":true}}}
```

成功返回201，规范化后的版本响应为下列结构。ID和时间仅为说明格式的占位示例，不是测试证据：

```json
{
  "id": "<v1-id>",
  "questionId": "<question-id>",
  "versionNo": 1,
  "parentVersionId": null,
  "content": {
    "kind": "number",
    "title": "本周运动几次？",
    "config": {"min": 0, "max": null, "integerOnly": true}
  },
  "createdAt": "<创建时间>"
}
```

派生接口提交完整新内容，不提交局部补丁：

```json
{
  "parentVersionId": "<v1-id>",
  "expectedCurrentVersionId": "<v2-id>",
  "content": {
    "kind": "number",
    "title": "过去七天运动几次？",
    "config": {"min": 0, "max": 30, "integerOnly": true}
  }
}
```

幂等摘要绑定操作、目标、父版本、预期当前指针和规范化内容。创建key在用户范围唯一，派生key在题目范围唯一。原请求成功后重试返回200及 `Idempotent-Replay: true`，内容仍为原先创建的不可变版本，即使当前指针后来已前进。创建重放恢复v1，不把最新版本冒充原结果。

同key改变绑定内容返回幂等冲突；预期指针过期返回版本冲突，均为409。两个不同key争抢同一预期指针时只允许一个成功；失败方刷新当前指针后可以继续基于原父版本派生。格式、题型约束、未知字段或无效来源按接口规则拒绝；数据库故障不得伪装成成功。最终错误码以实现和本轮API实测为准。

两类列表均返回 `items/offset/limit`，默认每页30项，`limit`为1—100，`offset`为0—100000。题目系列按创建时间和ID降序，历史按版本号降序。系列详情只含 `id/currentVersionId/lastVersionNo/createdAt/updatedAt`；版本内容按具体版本ID读取。API不返回内部请求标识和摘要。

| HTTP与错误码 | 含义 |
|---|---|
| 401 `UNAUTHENTICATED` | 未登录或凭据失效 |
| 404 `NOT_FOUND` | 题目不存在或当前用户无权访问 |
| 404 `QUESTION_VERSION_NOT_FOUND` | 已有权限的题目中不存在目标版本，或父版本属于另一题 |
| 409 `QUESTION_VERSION_CONFLICT` | 当前指针与预期不符，刷新后再决定派生 |
| 409 `IDEMPOTENCY_CONFLICT` | 成功key已绑定不同内容、来源、操作或预期指针 |
| 422 `INVALID_REQUEST` | 外层字段、请求头、字段类型或分页范围不符 |
| 422 `INVALID_QUESTION_CONTENT` | 内容内部字段或题型约束不符，错误路径指向 `content` |
| 503 `QUESTION_WRITE_OUTCOME_UNKNOWN` | 写事务或其失败后恢复查询无法确认结果，保留原key和原内容重试 |

开始写入前的数据库访问失败仍沿用全局 `DATABASE_UNAVAILABLE`。成功key在创建者范围或题目范围内长期保留；初始版本的key也不能再当作派生请求使用。

## 兼容边界与验收

本步只新增集合，不需要业务数据迁移，不自动把旧题导入题库或按相同题干合并身份。旧草稿、发布快照、答卷及其局部题ID保持原义，没有题库来源也应继续可用。初始化可以更新集合配置和索引，但不能因此改写旧四集合的文档。

验证使用随机命名的隔离MongoDB测试库，结束只清理该测试自有数据库，不使用真实运行数据。验收步骤为：

1. 创建四题型并读取v1，检查约束、缺省规范化、未知字段及非法数值；派生v2后确认v1全文不变，再从v1派生v3，核对父关系及当前指针。
2. 以另一用户访问列表、系列、版本和派生接口，核对私有隔离；以另一题的版本作为来源，核对拒绝且无写入。
3. 验证同key同内容重放、同key异内容冲突、旧成功key在当前指针前进后重放，以及失败key未被占用。
4. 使用真实MongoDB事务构造同key并发与不同key同指针竞争，核对版本数、编号、指针、回滚及无孤儿；直接写入非法BSON文档验证新集合约束。
5. 建立第一阶段问卷及答卷样例，对初始化和题目操作前后的旧四集合做全文等值比较；再运行旧发布、填写、统计等回归。敏感字段只参与比较，不打印到证据中。

**2026年10月25日实际结果：完整后端257项通过、251个subtest通过、3项跳过，0失败，用时93.27秒。** 其中新增内容纯测试56项、新增真实MongoDB/API测试39项，共95项全部通过。旧后端162项通过；3项跳过仍为Windows真实符号链接权限限制。JUnit的260个testcase元素对应257通过与3跳过；其tests=511包含251个subtest，不能称为511项独立pytest测试。

环境为Python3.12.14、PyMongo4.19.0、pytest9.1.1、MongoDB6.0.29。本轮使用新建本机副本集 `phase2test`、端口27227，所有用例创建随机测试库；借用已有锁定依赖环境，测试密钥仅注入进程，不读取或改写原真实配置。测试后确认无残留测试库，并关闭本轮服务。原桌面项目163个已核对文件与分析时哈希一致，没有覆盖原项目。

复现时先准备依赖、测试密钥和支持事务的隔离MongoDB，再同时设置两种测试入口使用的变量。下列命令使用本轮端口作示例，不会替用户自动启动数据库：

```powershell
$env:MONGO_URI = 'mongodb://127.0.0.1:27227/?replicaSet=phase2test'
$env:MONGODB_URI = $env:MONGO_URI
.venv\Scripts\python.exe -m pytest -q --junitxml=.runtime/phase2-tests.xml
```

实测证据为[原始最终输出](evidence/phase2-question-versions-tests-20261025.txt)、[JUnit结果](evidence/phase2-question-versions-tests-20261025.xml)、[环境与批次摘要](evidence/phase2-question-versions-results-20261025.json)。公开证据省略本机用户名、绝对目录和机器名，保留计数、输入场景及结果。

过程中的失败也保留说明：首次旧组件测试漏设其使用的 `MONGODB_URI`，出现5项连接错误；修正环境后通过。第一次全量为255通过、2失败、3跳过，失败来自新故障恢复测试把HTTP时间字符串与原生datetime直接比较；修正预期序列化方式后，两项定向通过，再取得上述最终全量结果。内容纯测试初次收集缺测试密钥，以及一处新增参数化测试漏写形参，也在正式全量前修正，没有把这些失败计作成功。

并发测试使用同步点同时放行真实请求与事务；它们验证本轮构造的竞争，不穷尽所有调度。事务重试和未知结果场景使用明确的合成服务或驱动边界故障，实际数据库写入、提交与回滚仍由MongoDB执行；不将其写成真实断网实验。本轮未重跑前端测试、构建或浏览器端到端，也未执行业务迁移或吞吐压测。

## 本轮AI使用与后续

用户在需求与现状分析后给出的原文为“开始跑吧”。Codex据此推进已建议的首个开发小步骤。主代理负责基线与工作副本检查、后端实现和整体验证；测试代理编写真实数据库与API验证；设计复核代理审查内容边界、分支与当前指针、幂等和旧格式风险，并撰写本文。实际分工与最终结果由主代理收口核对。

本轮AI产出包含方案、代码、测试和说明。不能据此虚构学生亲手设计、独立修改或已经理解掌握的贡献；人工判断与学习过程应在真实发生后追加。测试结论仅按实际输出记录，不补写未发生的演示、迁移或提交。

审查还发现写入异常后的恢复查询再次失败会落到通用数据库错误，丢失同key重试提示。主代理将其统一映射为 `QUESTION_WRITE_OUTCOME_UNKNOWN`，测试代理补齐提交后丢失返回、无成功记录及恢复查询失败场景。主代理修正两项测试的时间表示比较错误，完整保留持久化全文对照。连续过程另记在[AI日志](ai-log.md)。

下一步是把指定题目版本加入问卷并冻结可信来源：A/B两卷固定使用v1，创建v2后只有A显式换版，B、旧发布版和旧答卷保持原样。该链路验证后，再逐步完成恢复、使用追踪、共享与题库、跨问卷统计及必要迁移演练。
