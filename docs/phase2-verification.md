# 第二阶段公开复验说明

本文说明如何在独立测试库重跑第二阶段的主要业务流程，并记录2026年10月29日的改动和结果。重新核对两份原任务Word、当前代码和第一阶段基线后，没有发现新增的明确课程功能漏项；这次主要补复验入口、统一测试配置，并整理演示讲解。运行结果见[本轮证据](evidence/phase2-review-results-20261029.json)。外部96分是审阅估计，正式成绩由教师评定。

## 这次改了什么

新增[连续HTTP复验程序](../scripts/phase2_delivery_check.py)，把原来放在本地分析目录的连续流程纳入仓库。程序启动自己的HTTP应用，在新随机库中用两个合成账号依次验证版本、题库、共享撤销、旧绑定继续使用、0/3提交、独立汇总授权、CSV内容和恢复。本次实际完成67个请求，逐步状态见[HTTP结果](evidence/phase2-review-continuous-20261029.json)。

API与数据库组件测试原来分别读取MONGO_URI和MONGODB_URI，可能连到不同端口。现在两者共用一次会话的配置选择：显式MONGO_URI优先，其次兼容MONGODB_URI，都未设置才读本机配置；显式空值报错。应用导入会加载dotenv，因此在导入前保存这两个原始环境键，避免本地配置覆盖调用者的选择。纯配置测试、模拟导入顺序的子进程测试，以及只设旧变量的真实API和组件测试都已执行。

[启停指纹工具](../tests/support/lifecycle_snapshot.py)原来只检查第一阶段四个集合，现在按所选项目的VALIDATORS读取全部业务集合，第二阶段共十二个。读取在同一MongoDB快照事务中完成，输出文档数量和保留BSON类型的指纹。真实MongoDB测试确认，新增题目、版本、题库和成员能反映到指纹中，旧四集合保持不变。工具只读，不执行数据迁移。

统计服务中的32来源窗口、100项元数据批次、100项来源插入批次和100行CSV页分别改为具名常量，并补充缓存何时释放的说明。数值、查询和输出保持原样；窗口、事务重试和CSV跨页行为随完整回归检查。

## 运行连续复验

先完成README中的安装步骤，再准备独立的本机MongoDB单节点测试副本集：地址127.0.0.1:27227，副本集名phase2test。复验程序不读取.env，也不连接项目日常使用的27117端口。在项目根目录的新终端执行：

```powershell
$testDataPath = Join-Path (Get-Location) '.runtime\delivery-mongo-new'
if (Test-Path -LiteralPath $testDataPath) { throw '请换一个新的测试目录名。' }
New-Item -ItemType Directory -Path $testDataPath | Out-Null
mongod --dbpath $testDataPath --port 27227 --bind_ip 127.0.0.1 --replSet phase2test
```

保持这个终端运行，另开终端进入mongosh：

```powershell
mongosh --host 127.0.0.1 --port 27227
```

在mongosh中输入以下JavaScript，再用db.hello()确认isWritablePrimary为true：

```javascript
rs.initiate({_id: 'phase2test', members: [{_id: 0, host: '127.0.0.1:27227'}]})
db.hello()
```

回到项目根目录的PowerShell运行：

```powershell
.venv\Scripts\python.exe scripts/phase2_delivery_check.py --mongo-uri "mongodb://127.0.0.1:27227/?replicaSet=phase2test" --output ".runtime/delivery-check-new"
```

每次使用一个尚不存在的输出目录。results.json记录请求状态、数字案例、源码指纹、服务结束和随机库清理结果。ownership.private.json和server.private.log保留在私有.runtime中，不上传。

程序先停止自己启动的HTTP服务，再核对随机库名和本轮精确归属标记，匹配后才清理该库；归属变化时保留数据并报错。它不接收已有业务URL或数据库参数，也不启停Mongo。复验结束后，可在自己启动Mongo的终端按Ctrl+C正常结束。

若27227已被占用，先核对服务用途，不要停掉不明服务或复用它的数据目录。程序仅接受上述专用副本集，拒绝普通业务连接串、带凭据或多主机的地址。

## 本轮结果

| 批次 | 实际结果 |
|---|---|
| 完整后端 | 533通过、3项既有Windows权限相关跳过、251子测试通过；220.91秒，JUnit有536个testcase |
| 完整前端 | 216通过、0失败或跳过；类型检查、生产构建通过 |
| 仅设置旧MONGODB_URI的API和数据库测试 | 6通过 |
| 导入顺序与集合指纹定向测试 | 10通过 |
| 连续HTTP复验 | 67个请求完成，自建服务正常结束，随机库经归属检查后清理 |

新增31项已包含在后端533项中。定向批次、子测试和HTTP请求分别记录，不再加成另一份“总通过数”。

初轮定向为70通过、2失败，原因是新指纹工具误用了异步事务上下文，改用项目既有的with_transaction调用后通过。只读审查还发现应用在导入时就会读取dotenv，因此提前捕获原始显式URI，并增加子进程回归。过程见AI记录，旧失败结果仍保留。

## 其他证据与演示准备

本次脚本覆盖连续HTTP业务和隔离清理。安装、原启停脚本及代表页面检查另见[新目录安装与启停验证](phase2-delivery-review.md)；此前[502/216完整回归](evidence/phase2-optimization-results-20261029.json)保留原批次。[本轮收尾](evidence/phase2-review-shutdown-20261029.json)记录了专属Mongo正常退出，以及原项目163个已比较文件的哈希保持不变。

演示可从[八至十分钟讲解与自查](phase2-walkthrough.md)开始，完整操作见[演示手册](phase2-demo.md)。三项尚未得到教师逐项确认的需求解释见[验收说明](phase2-acceptance.md)。目前没有大规模负载、分片或全部浏览器分支的实测结论，原任务书也未将这些列为必做项。
