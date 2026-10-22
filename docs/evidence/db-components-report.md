# MongoDB 初始化与统计组件实测

记录日期：2026-10-19。

本次测试使用真实 MongoDB 副本集 `rs0`，连接本机独立端口 `27117`。解释器为项目 `.venv`，Python 3.12.14、PyMongo 4.19.0、pytest 9.1.1、pytest-asyncio 1.4.0。每个测试仅创建自己的 `survey_test_components_<随机值>` 数据库，结束后仅清理这个已验证前缀的库；不读取或修改已有业务集合。

原始脚本：`tests/test_database.py`。原始测试输出：`docs/evidence/db-components-pytest.txt`。

结果：**5 项测试通过，pytest 报告执行时间 2.52 秒。** 这是测试执行耗时，不是接口性能指标。

## 实际验证范围

| 测试 | 输入或操作 | 实际结果 |
|---|---|---|
| 初始化 | 同一新库初始化两次；检查命名索引；写入零题目、空入口的初始草稿 | 均成功，不重置已有内容 |
| 六份人工样例 | 按 V4 的六份受控样例插入真实 responses，由 snapshot 事务与 MongoDB 聚合统计 | 总答卷 6、独立参与者 5、匿名 3、实名 3；各题 R/V/A/U/S 与人工值一致 |
| 统计边界 | 数值 0、选填未答、分支跳过、多选及零次选项 | 得分总和 28、平均 5.6；年龄均值 101/6；多选各项计数均 3，独立人数分别 2/3/3；零次选项返回 0 |
| 固定序号 | 先查询范围 6，再以事务加入第 7 份答卷；重查范围 6，另查最新范围与范围 0 | 范围 6 的完整响应保持一致；最新答卷数 7；范围 0 为零，数字平均值为 null |
| 查询错误 | 负数、布尔、非整数、超过当前序号的范围及不存在的题目 | 均抛出明确 StatsError |
| BSON 校验 | 匿名带 identity、数字为布尔、重复答案、答案不属于路径、限次标识为 null/不一致、未知字段、错误答案类型、空路径 | 9 种直接数据库插入均被拒绝，错误码 121；正式答卷数仍为 6 |
| 唯一约束 | 更换文档 ID 重复提交键；省略限次字段的合法多份；同人限次重复；重复序号 | 三类重复被唯一索引拒绝；允许多填的省略字段记录不受部分唯一索引误限 |
| 响应隐私 | 检查统计输出 | 没有 participantKey、oneTimeParticipantKey、payloadDigest 或 usernameAtSubmit |

六份样例是人为构造的核算数据，不是真实填写者。文本与数字原始明细由主 API 提供，本组件测试不冒称已经验证页面或导出。

## 实现边界

- 初始化安装四集合的 `validationLevel=strict`、`validationAction=error` 校验器与固定名称索引，不删除集合、不修复旧记录。对已有库执行 collMod 不等于自动迁移或重新审计全部历史文档。
- 动态题目约束、引用归属、规则正确性和实时限次政策仍由领域层与事务服务层负责；集合校验器只保证本地 BSON 结构及部分本地一致性。
- 统计先在只读 snapshot 事务中读取主记录序号，再聚合原始答卷。数据库处理路径、答案、独立人数和选项分布；Python 只为有界汇总附加题目标签与零值，不加载全部答卷计算。
- 上述五项组件测试不包含提交/关闭竞态、发布回滚、连接中断或性能压测；对应场景由单独的测试与实验提供证据。下文另外记录驱动边界的事务恢复注入。

## 复现

先按项目说明启动独立副本集，然后从 `phase1` 目录执行：

```powershell
$env:PYTHONPATH = 'backend'
& '.venv/Scripts/python.exe' -m pytest tests/test_database.py -vv -ra
```

如使用其他测试实例，可设置 MONGODB_URI。测试数据库名仍由测试自身生成，不使用 URI 中的数据库名作为清理目标。

## 追加：事务体重试与提交结果恢复

原始脚本：`tests/test_transaction_recovery.py`。原始输出：`docs/evidence/db-components-transaction-recovery.txt`。

执行结果：**3 项测试通过，pytest 报告执行时间 1.75 秒。** 复用 `tests/conftest.py` 的实际应用生命周期、登录、发布、开始填写和提交流程，连接真实 MongoDB；只使用该 fixture 创建的 `survey_test_` 独立测试库。

| 注入位置与异常 | 实际观察 | 验证结论 |
|---|---|---|
| 第一次真实 gate 写入后抛出带 `TransientTransactionError` 标签的 OperationFailure | gate 回调运行两次，观察序号 `[1,1]`；插答卷回调仅一次；最终 1 份答卷、主序号 1 | 驱动确实重跑事务体，首次递增回滚，成功序号不被多消耗；同键重放取得原回执 |
| 同一 gate 位置先把受控应用时钟从截止前推进到截止后，再抛一次瞬态异常 | 两次 gate 的 updatedAt 分别在截止前和截止后；HTTP 409、SURVEY_EXPIRED；最终 0 份答卷、主序号 0、policyLocked=false | 重试采用新的服务端测试时钟，拒绝过期提交，第二次 gate 及政策锁也全部回滚 |
| monkeypatch AsyncClientSession.commit_transaction；第一次先 await 原始 commit 成功，再抛带 `UnknownTransactionCommitResult` 标签的 ConnectionFailure | 注入前在真实数据库已读到 1 份已提交答卷；commit 方法调用 2 次；gate 与插答卷回调各 1 次；最终 1 份答卷、主序号 1 | 驱动只重试同一事务的 commit，不重跑业务回调；返回成功，同键重放回执一致 |

异常构造使用已安装 PyMongo 4.19.0 的实际签名：OperationFailure 的标签位于 details.errorLabels；ConnectionFailure 接收 error_labels。测试在抛出前验证标签确实存在。

**故障模型边界：**这是受控的驱动方法边界故障注入，事务读写、回滚与 commit 都在真实数据库执行；没有断开 TCP、删除网络包或启动故障代理，不能称为已经复现实际网络丢包或网络层提交确认丢失。第三项是在真实提交完成后人工让驱动看见不明提交结果，用于验证 convenient transaction API 的 commit 重试分支。性能压测、真实网络故障及主节点故障切换仍需各自的实验，不能由这三项测试替代。

复现命令：

```powershell
& '.venv/Scripts/python.exe' -m pytest tests/test_transaction_recovery.py -vv -s -ra
```
