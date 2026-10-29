# 第二阶段公开复验与本轮核对

2026年10月29日，参考外部代码审阅后重新完整核对两份原任务Word、当前实现和第一阶段基线，没有发现新增的明确课程功能漏项。外部96分是审阅估计，不是教师正式成绩。本轮集中补第三方复验入口、测试配置一致性和演示讲解，没有增加无关业务功能。新运行结果见[本轮证据](evidence/phase2-review-results-20261029.json)。

## 本轮实际改进

此前67请求记录真实存在，但连续链脚本只在本地分析目录；现在[公开复验入口](../scripts/phase2_delivery_check.py)随仓库提供。它启动自己的真实HTTP应用，以新随机库和两个合成账号完成版本、题库、共享撤销、固定绑定留用、真实0/3提交、独立统计授权与撤销、CSV内容及恢复。新运行也是67请求，按本轮实际记录计数，没有为对齐历史而删改步骤。见[HTTP逐步结果](evidence/phase2-review-continuous-20261029.json)。

API与数据库组件测试此前分别读取MONGO_URI和MONGODB_URI，专用端口配置可能分叉。现在两类夹具共用会话级选择：调用者显式MONGO_URI优先，其次保留旧MONGODB_URI，均缺失才用本机配置；显式空值直接报错。特别在导入会触发dotenv的应用之前冻结这两个原始环境键，避免本地配置抢占旧别名。纯测试、模拟导入顺序以及只设旧变量的真实API/组件测试均已执行。

公开[启停指纹工具](../tests/support/lifecycle_snapshot.py)此前只列四个第一阶段集合，现在按所选项目的VALIDATORS读取全部业务集合：第一阶段四个、第二阶段十二个。各集合在同一MongoDB快照事务里读取，只返回文档数量和保留BSON类型的指纹。真实MongoDB测试确认新增题目、版本、题库和成员的变化能被检测到，旧四集合不受这些操作影响。它是只读工具，不是自动执行真实业务迁移的工具。

统计服务将32来源窗口、100项元数据批次、100项来源插入批次和100行CSV页分别命名，并补缓存生命周期说明。现值、查询和输出语义保留；本轮没有改动授权候选查询或扩大他人访问权限。关键窗口、重试和CSV跨页行为随完整回归验证。

## 运行公开连续复验

先完成README安装，准备一个独立的本机测试MongoDB单节点副本集，地址为127.0.0.1:27227、副本集名phase2test。该工具不会读取.env，也不会使用项目日常运行的27117端口。准备示例可在项目根目录的新终端执行：

```powershell
$testDataPath = Join-Path (Get-Location) '.runtime\delivery-mongo-new'
if (Test-Path -LiteralPath $testDataPath) { throw '请换一个新的测试目录名。' }
New-Item -ItemType Directory -Path $testDataPath | Out-Null
mongod --dbpath $testDataPath --port 27227 --bind_ip 127.0.0.1 --replSet phase2test
```

保持该终端运行；另开终端进入mongosh：

```powershell
mongosh --host 127.0.0.1 --port 27227
```

在mongosh提示符输入以下JavaScript，随后用db.hello()确认isWritablePrimary为true：

```javascript
rs.initiate({_id: 'phase2test', members: [{_id: 0, host: '127.0.0.1:27227'}]})
db.hello()
```

回到项目根目录的PowerShell运行：

```powershell
.venv\Scripts\python.exe scripts/phase2_delivery_check.py --mongo-uri "mongodb://127.0.0.1:27227/?replicaSet=phase2test" --output ".runtime/delivery-check-new"
```

每次换一个尚不存在的输出目录。results.json记录真实请求状态、数字金样、源码指纹、服务正常结束和随机库清理结果。ownership.private.json和server.private.log留在私有.runtime内，不上传。只有随机库名及本轮精确归属标记都匹配、且自建HTTP服务已停止，才删除本轮随机库；遇归属变化会保留数据并报告失败。脚本不接收已有业务URL或已有数据库参数，也不启停Mongo。结束后可在自己启动Mongo的终端按Ctrl+C正常结束。

如果27227已经被其他程序占用，先核对其用途，不能为了运行本脚本停掉不明服务或复用其数据目录。本工具明确固定到专用副本集，因此不接受普通业务连接串、凭据或多主机地址。

## 本轮实际结果与范围

完整后端533通过、3项既有Windows权限相关跳过、251子测试通过，220.91秒，JUnit有536个testcase。新增31项已经包含其中。前端216通过、0失败/跳过，类型检查及生产构建通过。仅设旧变量的真实API/数据库定向6通过；导入顺序与指纹定向10通过。这些批次和67个HTTP请求不相加。

初轮定向70通过、2失败，原因是新指纹工具误用异步事务上下文；改用项目既有的with_transaction调用方式后通过。只读审查另发现应用导入就会读取dotenv，因此将原始显式URI的捕获提前，并新增子进程回归。失败与修订保留在AI记录中，没有放宽业务逻辑凑结果。

原来的[新目录安装与启停保留验证](phase2-delivery-review.md)仍是独立历史证据；本次连续脚本只证明真实HTTP业务链及其隔离清理，不声称重新安装、操作浏览器或复演所有启停分支。[原始完整回归502/216](evidence/phase2-optimization-results-20261029.json)按原批次保留。[本轮收尾](evidence/phase2-review-shutdown-20261029.json)核对专属Mongo正常退出和原项目163个已比较文件不变。

演示准备从[八至十分钟讲解与自查](phase2-walkthrough.md)开始，完整页面步骤仍见[演示手册](phase2-demo.md)。三项未获得教师逐项确认的解释保留在[验收说明](phase2-acceptance.md)，不改写为课程标准。大规模负载、分片和全部浏览器分支仍无实测结论；原任务书也没有要求以这些功能补足评分。
