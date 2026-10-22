# 题型、条件与答案协议

本文依据 `backend/app/domain.py`、`services.py`、`security.py` 和 `main.py` 的实际接口编写。V4 是设计基线；实现与测试证据分开记录。本文不代表浏览器端到端、并发或性能测试已经通过。

## 1. 执行边界

领域模块只依赖 Python 标准库，不访问 HTTP、账号或 MongoDB。公开函数为：

```python
validate_definition(definition) -> list[dict]
evaluate(definition, rawAnswers, mode="final") -> dict
```

`validate_definition` 当前只返回阻断错误，不混入非阻断提示。不可达题、规则重叠的可视化提示不是该函数当前返回的内容。相互重叠的合法规则由优先级确定唯一去向，不会因为重叠本身被拒绝。

创建者预览调用 `Services.preview`；正式填写的 `/api/survey/{publicId}/evaluate` 调用 `Services.evaluate_fill`，后者固定 `interactive`；提交服务固定调用 `final`。创建者身份、填写凭据、版本、匿名政策、截止和收集状态由服务层判定。

## 2. 问卷定义

发布定义必须具有以下字段，拒绝未知字段：

```text
title, description, allowAnonymous, entryQuestionId, questions
```

`title` 为非空文字，最长 200 码点；`description` 最长 5000 码点，可空；`allowAnonymous` 必须是布尔值。问卷含 1—100 题。题目统一结构为：

```text
id, kind, title, required, config, defaultTarget, rules
```

题目标题非空且最长 500 码点，`required` 为布尔值。题目、选项、规则 ID 采用 1—64 位字符串，首位为 ASCII 字母，其余为字母、数字、下划线或连字符；`END` 为保留终点，不能作为这些 ID。题目 ID 整卷唯一，规则 ID 整卷唯一，选项 ID 在所属题内唯一。

题目数组只表示编辑和展示顺序。入口由 `entryQuestionId` 指定；去向只能是有效题目 ID 或 `END`，不在运行时按题号推断。编辑器可初始化默认顺序，但保存后的 `defaultTarget` 是执行依据。

| kind | 非缺失答案 | config |
|---|---|---|
| single | 本题一个合法选项 ID | `options:[{id,label}]` |
| multi | 本题合法选项 ID 数组 | options、`minSelected/maxSelected` |
| text | 字符串 | `minLength/maxLength` |
| number | 有限 JSON 数字 | `min/max/integerOnly` |

每个选择题有 1—50 个选项，选项标签非空且最长 200 码点。恰好选 N 项用 `minSelected=maxSelected=N` 表达。选择数量边界为 0 到选项数之间的整数；文本长度边界为 0—10000 的整数；数值上下界必须是有限安全数字。非空上下界要求下界不大于上界。

配置拒绝不适用于该题型的未知字段。可选边界可省略或写 `null`，均表示不设置该边界；省略 `integerOnly` 时按 `false`。选择题不能省略 `options`。必答多选不能把最大选择数设为 0，必答文本不能把最大长度设为 0；整数题的有界区间必须至少包含一个整数。

空问卷及尚未补全的草稿可以编辑保存，但不能预览执行或发布。MongoDB 草稿结构也有边界检查；它不等价于领域层的完整发布检查。

## 3. 答案、规范化和缺失

API 使用题目 ID 到值的对象，不接收客户端指定的“有效路径”：

```json
{"q_age":17,"q_hobby":["sport","read"],"q_note":null}
```

| kind | 缺失表示 | 非缺失校验与规范化 |
|---|---|---|
| single | 缺字段、null | 必须是合法选项 ID 字符串；空字符串不是缺失 |
| multi | 缺字段、null、空数组 | 仅允许字符串数组；重复项报错，不静默去重；检查归属和数量，按 ID 排序 |
| text | 缺字段、null、去首尾空白后为空串 | Python `str.strip()` 后按 Unicode 码点计长度并保存；不作 NFKC 改写 |
| number | 缺字段、null | 拒绝数字字符串、布尔值、NaN、Infinity 及超范围数字；0 有效 |

数字绝对值不得超过 `9007199254740991`。整数要求按数学值判断，因此 18.0 合法、18.5 不合法；所有数学整数规范化为 Python int，1 与 1.0、0 与 -0.0 得到相同语义。非整数按浮点数保留，比较没有额外的近似相等规则。

选填题缺失时跳过最短长度、最少选择数和数值范围检查；只要提供了非缺失值，这些约束都要执行。文本全局上限为 10000 码点；不接受未配对代理码点。

未知题目 ID 在遍历前返回错误。已知但未到达题目的缓存值不进入逐题规范化、约束检查或规则求值，也不进入最终保存答案；整体对象格式和大小限制仍然适用。输入对象不会被领域函数修改。

## 4. 条件语法

条件只读取规则所属当前题，不提供跨题 `questionId`。每个条件对象恰好采用一种语法，拒绝额外字段：

```json
{"operator":"lt","value":18}
```

```json
{"allOf":[{"operator":"gte","value":0},{"operator":"lt","value":18}]}
```

```json
{"anyOf":[{"operator":"eq","value":17},{"operator":"eq","value":18}]}
```

| 当前题 | 运算符 | 比较值与含义 |
|---|---|---|
| single | eqOption | 本题合法选项 ID，相等才命中 |
| multi | containsAny | 非空、无重复合法选项数组；交集非空 |
| multi | containsAll | 非空、无重复合法选项数组；答案包含全部比较项 |
| multi | setEquals | 非空、无重复合法选项数组；集合相等，与排列无关 |
| number | lt、lte、gt、gte、eq | 有限安全数字，普通大小或相等比较 |
| number | between | `{"min":0,"max":18}`，包含两端的闭区间 |
| 同一题 | allOf、anyOf | 至少一个子条件，分别表示全部/任一成立 |

文本题当前只能使用默认去向。条件树从根节点计深度，最多 4 层，每条树最多 100 个节点。所有叶子都必须适用于当前题。缺失答案不执行规则，因此不会因空集合或数字 0 的替代而误命中。

每题最多 20 条规则，规则结构为 `id,priority,condition,target`。priority 为安全整数，同题不得重复，可以是负数；数值小者先执行，命中后立即停止，数组中的存放顺序不决定优先级。

## 5. 图检查与执行结果

发布检查同时纳入全部默认边和条件边，对包括不可达题在内的完整图判环；无效目标、缺失默认去向、重复 ID、类型/选项/约束错误都阻断。显示序号较前的目标可以合法执行，条件是完整图无环。每个题都有合法去向且完整图无环时，入口的每次执行最终能够到达 END。

求值从入口逐题进行：到达当前题 → 规范化并校验当前答案 → 对合法非缺失值按优先级检查规则 → 命中去相应目标，否则默认去向。选填缺失直接采用默认边。运行时保留重复节点和步数上限保护。

结果固定包含：

| 字段 | 精确含义 |
|---|---|
| path | 本次已到达题目 ID，包含当前待答/错误题，不包含 END |
| activeAnswers | 已到达且合法非缺失的规范化答案对象，按路径插入 |
| trace | 逐题状态、答案、已检查规则、命中规则、目标及原因 |
| errors | 结构或逐题错误；无错误时为空数组 |
| blockedAt | interactive 暂停所在题；没有暂停则为 null |
| complete | 是否真正到达 END，不能单独理解为允许提交 |
| provisional | 是否因最终错误而沿默认边生成了暂定诊断路径 |

interactive 遇到必答缺失或非法值时在当前题暂停，trace 的该题 target 为 null，后续路径尚未确定。final 记录错误并沿默认边继续收集诊断；即使最后 complete=true，只要有错误或 provisional=true 就不可保存。

**正式成功门槛：complete=true、errors=[]、provisional=false，三项同时成立。** 成功结果的 activeAnswers 再次求值后，路径和规范化答案应保持不变。

trace 中每个元素具有 `questionId,status,answer,ruleResults,matchedRuleId,target,reason,provisional`。status 为 answered/missing/invalid；ruleResults 只列实际检查过的规则，到首条命中为止，每条含 ruleId、priority、matched、reason。未检查的规则不能显示为“条件不成立”。

### 非阻断设计提示（第十一轮）

`design_checks.analyze_definition`先运行原定义校验，非法定义返回空提示，由原预览/发布流程继续用422阻断，不把错误降级。分析不修改定义和统一求值器；只有所有者预览响应额外附加warnings，公开填写和持久化结构保持原语义。

- 完整默认边和所有条件边从入口遍历，找出结构上不可达的题目；包含不可能命中的连线也会被遍历，因此这不是全部语义不可达证明。选填缺失仍按原默认边执行。
- 规范化重复只重排可交换的组合子节点、移除同组重复子节点、排序多选比较集合并统一等价数字表示，不改原定义。多选仅使用该保守判据，不证明任意集合条件的覆盖关系。
- 单选在当前选项ID上精确枚举。数字在配置上下限、安全数值范围和integerOnly限制内计算集合；组合树使用区间并/交，严格浮点边界使用相邻可表示数，整数边界使用ceil/floor。未用少量样例抽样代替证明。
- 按priority从小到大累计已覆盖的有效答案域。后规则无有效答案时提示无解；否则按重复、完全遮蔽、部分重叠的顺序至多产生一条规则提示。关联规则只列与其相交的实际有效前规则；同去向仍可能提示冗余，不改变首次匹配行为。

每项包含code/message/questionId/ruleId/field/relatedRuleIds，具体代码与实例见api.md。页面显示先匹配规则的序号和优先级，提示定位仅添加说明关联与琥珀色标记，不设置aria-invalid；真正的阻断错误仍用红色和aria-invalid。检查绑定当前用户、问卷、定义快照/代次及最新请求，编辑后立即失效。尚未检查、请求失败或响应缺少warnings数组时，界面不会显示“无提示”。

这些提示可帮助审阅设计，但不表示问卷一定合理，不分析用户自然语言意图，也不证明默认路径的所有语义冗余。27项纯分析测试与真实API/页面边界见开发记录/11-规则设计检查.md。

## 6. 错误与请求边界

领域错误包含 `code,message,questionId,ruleId,field`；不适用的定位字段为 null。常见 code 包括 required、invalid_number、integer_required、number_range、invalid_option、duplicate_option、choice_count、text_length、unknown_question、invalid_target、flow_cycle、unknown_field。API 的统一错误外层为 `code,message,fieldErrors,requestId`；求值返回的 errors 与 HTTP 请求模型校验错误不是同一种层次。

第十轮前端使用INVALID_DEFINITION的fieldErrors定位编辑控件，保留问卷/会话/定义快照与代次，内容变化后使旧诊断失效。可视化控件以完整字段路径标识，条件递归延续allOf/anyOf索引；ID冲突、未知或无对应控件的字段保守转JSON。保存接口没有字段列表的通用定义错误不猜测位置。JSON导入另有渲染结构保护，失败保留缓冲和已应用草稿；这不改变后端协议或代替正式校验。

领域定义上限为 512 KiB、答案上限为 1 MiB，按紧凑 JSON 的 UTF-8 表示估算。HTTP 中间件另外检查原始请求流：draft/preview 整个请求最多 512 KiB，其他写接口最多 1 MiB；不能依靠领域函数代替 HTTP 原始大小保护。

## 7. 版本与幂等连接

填写凭据通过 X-Fill-Token 发送，绑定当前登录用户、surveyId、versionId 和有效期。正常提交还发送 Idempotency-Key；answers 是请求体的一部分，anonymous 是严格布尔值。

服务端只用领域结果生成存储数组。摘要版本目前为 1：对固定 versionId、anonymous 和规范化 activeAnswers 生成确定性 JSON 摘要。显示顺序、多选排列、等价整数表示、首尾空白、路径外旧值不应产生不同的有效答案语义；改变有效答案、发布版本或匿名选择则产生冲突。

同用户同问卷同键的成功重放先验证原记录及摘要，再返回原回执，不要求新填写凭据仍有效，也不要求问卷仍开放。只有未成功过的新写入才继续验证填写凭据、对应不可变版本和当前收集状态。

第十三轮在真实Mongo/API中补齐该连接的边界：版本ID属于语义，即使两版定义完全相同也不合并成功标识；改有效答案或将原有内容改成无效答案均不能替换原记录。规范化等价包括选项顺序、整数/等价浮点、正负零、文本首尾空白和原本未答的选填表示，不包含删除原先已答的选填或添加未知题目。

两份不同合法内容在首次成功查找后、事务前同时到达同步屏障，放行后只有一份201，另一份409/IDEMPOTENCY_CONFLICT；失败事务没有留下额外序号，最终一份答卷且acceptedSeq=submitSequence=1。后续胜者恢复200、败者仍409，四集合完整文档不变。这是一次有明确开始屏障的实际事务竞争证据，不是所有并发交错或政策更新竞态的证明，详见开发记录/13-重复提交与内容冲突.md。

### 首次提交与多填设置的连接

submission_gate在同一事务中对surveys真写：递增submitSequence并设置policyLocked，返回该次正式提交使用的allowMultiple。答卷插入与gate一并提交或回滚。政策更新条件包含当前settingsRevision、非关闭状态和policyLocked=false；不能在事务外先读一次政策便直接决定答卷的一次性唯一字段。

第十四轮在after_submission_gate屏障暂停提交，再发起真实政策修改，用currentOp观察到对应随机测试库、问卷与目标政策的活动findAndModify后放行。此时外部读仍看到提交前完整快照，两个请求均未完成。成功提交时，两种原政策都保留，修改409/POLICY_LOCKED且修订不变；服务端时钟跨截止导致真实事务回滚时，修改200，答卷0、序号0、policyLocked=false，只出现预期设置字段修改。清除截止后首次成功使用新政策，acceptedSeq从1开始。

本次三条currentOp记录waitingForLock均为false；证据只表明命令已到服务器且尚未完成，不将其称为Mongo锁等待指标。观察命令需要测试服务器允许currentOp，业务路径没有增加该调用或权限。混合修改的完整原子性、仅改截止的正向例和后续限次见开发记录/14-首次提交与多填设置.md；未穷尽所有调度交错。

## 8. 当前验证证据

`tests/test_domain.py` 包含类型、图、规则、回改、规范化、六份人工样例核算，以及真实 BSON 编解码后的 Int64 边界回归；本轮在项目 .venv 中执行 44 项 unittest 全部通过。Int64 数字边界、条件值、优先级能读回求值，布尔值仍被拒绝。领域模块仍不依赖 PyMongo；只有可选的 BSON 回归测试导入驱动。该结果不代表真实数据库事务、API、浏览器或性能验证已经完成，其他证据以单独测试报告为准。
