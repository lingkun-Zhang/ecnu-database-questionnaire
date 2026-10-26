# 问川：基于 MongoDB 的问卷系统

华东师范大学数据库课程项目。用户可以创建带条件跳转的问卷、发布版本、登录填写，再查看统计与导出原始回答。业务数据全部保存在 MongoDB。第二阶段已接入个人题目管理、内容版本与恢复、使用查询，以及问卷固定选版、换版和解绑界面；已接入指定账号的共享管理与共享选题界面；题库后端已支持私有库、固定版本成员与显式更新；题库界面、跨问卷统计和最终交付整理仍待推进。当前整体进展见[第二阶段进度](docs/phase2-progress.md)。

仓库地址：[lingkun-Zhang/ecnu-database-questionnaire](https://github.com/lingkun-Zhang/ecnu-database-questionnaire)。第一阶段基线为 `99b66d1e637fa3b4ecb13719c0230ecb7e958701`；本地第二阶段工作尚未推送。

## 先看哪些文件

| 内容 | 入口 |
|---|---|
| 第二阶段当前进展与验证 | [进度与待办](docs/phase2-progress.md)、[题库后端](docs/phase2-question-banks.md)、[共享管理与选题界面](docs/phase2-sharing-ui.md)、[共享使用授权后端](docs/phase2-question-sharing.md)、[个人题目与问卷选题界面](docs/phase2-question-ui.md)、[换版/填写/解绑浏览器补验](docs/phase2-question-ui-browser-check.md)；前置后端见[恢复旧版本](docs/phase2-question-restore.md)、[使用查询](docs/phase2-question-usage.md)、[固定引用](docs/phase2-question-reuse.md)和[独立题目与版本](docs/phase2-question-versions.md) |
| 实验报告与设计说明 | [公开版报告](docs/phase1-report.md)、[数据库设计](docs/schema.md)、[关键业务协议](docs/protocol.md) |
| 增强功能、实现与实测结果 | [功能说明与测试](docs/功能说明.md)；下方也列出主要内容 |
| 从创建到统计的完整操作 | [安装与演示](docs/demo.md)，包含输入、预期结果与六份教学样例 |
| 测试范围、原始结果与需求对应 | [测试报告](docs/test-report.md)、[需求追踪](docs/requirements.md) |
| 接口参数 | [API说明](docs/api.md)、[OpenAPI快照](docs/api-openapi.json)；运行后也可打开 `/docs` |
| AI辅助过程、问题与修改 | [AI使用说明](docs/ai-use-summary.md)、[详细日志](docs/ai-log.md) |

课程平台单独收取PDF。带姓名、学号的正式PDF和Word报告留在本地，GitHub提供脱敏正文；真实配置、运行数据和原始截图也不公开，见[公开材料说明](PRIVACY.md)。

## 安装、运行和停止

当前安装、启停脚本适用于 Windows。先准备以下程序：

| 程序 | 要求 / 实测环境 |
|---|---|
| Python | 3.12及以上；实测3.12.14 |
| Node.js（含npm） | 20.x至少20.19，或22.12及以上；实测24.18.0 |
| MongoDB Server、mongosh | 将 `mongod`、`mongosh` 加入PATH；MongoDB实测6.0.29 |

1. 克隆仓库或下载ZIP并解压，进入包含 `Install.cmd` 的根目录。
2. 双击 **`Install.cmd`**。它建立 `.venv`、安装锁定依赖、生成本机配置，并完成前端类型检查与构建。
3. 安装成功后双击 **`Start.cmd`**，再打开 **<http://127.0.0.1:8000>**。首次使用注册自己的账号。
4. 用完双击 **`Stop.cmd`**。下次双击 `Start.cmd` 继续使用，账号、问卷和答卷保留。

源码不包含 `.venv`、`node_modules` 或 `frontend/dist`，所以克隆后必须先安装。后端启动时同时提供构建后的页面，不需要另外启动前端开发服务。

项目使用本地 MongoDB `127.0.0.1:27117`、单节点副本集 `rs0`，数据与日志在 `.runtime/`，配置在 `.env`。单节点副本集用于支持事务，不代表高可用集群。首次安装生成独立密钥，后续安装和启动保留配置。

如果安装提示找不到Python，可在根目录明确指定解释器（路径替换为自己的安装位置）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/install.ps1 -PythonExe "C:\Python312\python.exe"
```

启动、停止遇到8000端口被其他程序占用时会拒绝操作；先检查 `.runtime/` 日志。MongoDB程序不在PATH时，可按[演示说明](docs/demo.md)准备环境，或使用 `scripts/mongo.ps1 -Mongod 路径 -Mongosh 路径` 初始化数据库。

## 快速演示

完整手工流程见[demo.md](docs/demo.md)：注册 → 创建 → 添加四种题型 → 配置年龄跳转 → 保存并发布 → 匿名/实名填写 → 查看统计。

第二阶段可从“题目管理”的“我的题目”创建或打开题目，查看历史、编辑、恢复、查询本人使用情况并按用户名共享；接收者可在“共享给我的”只读查看。问卷编辑器通过“从已保存题目选用”选择本人或共享题目的具体版本，再保存、发布。引用题可更换同系列版本或解除引用，均先改草稿，发布后才供新填写使用。恢复和题目更新不会自动改变已有问卷；操作说明与本轮实际验证范围见[选题界面说明](docs/phase2-question-ui.md)。

也可在应用启动后，从根目录导入六份教学样例：

```powershell
.venv\Scripts\python.exe scripts/seed.py
```

脚本通过真实API导入，保留已有数据。教学账号为 `demo_owner / DemoLab2026!`，仅用于本地演示。预期6份答卷、5位参与者、匿名/实名各3份；评分有效5份，含数字0，总和28、均值5.6。核对结果见[样例统计记录](docs/evidence/final-sample-checks-20261021.json)。

## 怎样运行测试

先完成安装并启动项目MongoDB，然后在仓库根目录执行后端测试。集成测试连接真实MongoDB，创建随机命名的独立测试库，完成后只清理自己创建的库。

```powershell
.venv\Scripts\python.exe -m pytest -q
```

前端全部回归、类型检查与生产构建：

```powershell
Set-Location frontend
node --test tests/conditionEditor.test.cjs tests/session.test.cjs tests/editorErrors.test.cjs tests/questionReuse.test.cjs tests/questionLibrary.test.cjs tests/questionSharing.test.cjs
npm.cmd run build
Set-Location ..
```

下表保留第一阶段的历史批次，两个日期不能合并计数。第二阶段最近前端118项及浏览器检查见[共享界面说明](docs/phase2-sharing-ui.md)；最近后端全量382项见[题库后端批次](docs/phase2-question-banks.md)，两个批次分别记录。

| 批次 | 结果 | 原始记录 |
|---|---|---|
| 2026-10-21 后端全量 | 162通过、251个subtest通过、3项Windows符号链接权限相关跳过 | [日志](docs/evidence/final-tests-20261021.txt)、[JUnit](docs/evidence/final-tests-20261021.xml) |
| 2026-10-22 前端回归 | 58通过，0失败/跳过；类型检查与构建通过 | [测试输出](docs/evidence/editor-lock-frontend-tests-20261022.txt)、[构建输出](docs/evidence/editor-lock-build-20261022.txt) |
| 2026-10-21 干净目录安装 | 新环境、依赖、配置、构建和独立数据库；从空白创建到统计的演示完成 | [安装验收摘要](docs/evidence/final-clean-install-20261021.json)、[演示步骤](docs/demo.md) |

## 主要增强内容

以下是基础流程之外的设计与实现，具体评分由课程标准决定。每项的代码、测试与结果入口集中在[功能说明与测试](docs/功能说明.md)。

| 增强内容 | 说明与证据 |
|---|---|
| 数据库约束与真实索引实验 | 四集合BSON校验器、唯一/部分索引；1,000份问卷、20,000份合成答卷，五类查询对照默认计划、指定索引和全表扫描；[实验说明](docs/index-experiment.md)、[完整执行计划](docs/evidence/index-experiment-20261021.json) |
| 不可变发布版本与并发冲突处理 | 草稿修订、发布预期检查、旧版本答卷解释与历史复制；[版本协议](docs/protocol.md)、[并发发布回归](tests/test_publish_version_cas.py) |
| 事务、幂等与限次政策协调 | 重复提交恢复、关闭/截止与提交的顺序、失败回滚；[提交边界测试](tests/test_submission_boundaries.py)、[政策竞争测试](tests/test_policy_race.py)、[事务恢复测试](tests/test_transaction_recovery.py) |
| 规则编辑与设计检查 | 嵌套且/或、优先级、路径外答案清理、可定位错误和五类非阻断设计提示；[规则协议](docs/protocol.md)、[8组单选/多选实例结果](docs/evidence/routing-examples-20261022.json) |
| 固定范围统计与隐私 | 按发布版本、题目及匿名/实名筛选；统计、分页、CSV使用相同序号范围；[统计组件实测](docs/evidence/db-components-report.md)、[筛选回归](tests/test_anonymity_filter.py) |
| 编辑与交付可靠性 | 保存/发布等待期间锁定编辑、防迟到响应覆盖；独立安装与安全启停；[编辑保护摘要](docs/evidence/editor-lock-results-20261022.json)、[启停测试](tests/test_runner_control.py) |

索引实验可在MongoDB启动后独立复现。输出文件必须使用尚不存在的路径：

```powershell
.venv\Scripts\python.exe scripts/index_experiment.py --output .runtime/index-experiment-review.json --repeats 7
```

实验自动建立并清理归属可确认的独立合成数据数据库，不使用业务库。它验证查询计划和扫描量，不是20,000次API提交或并发压测。

## 目录

```text
backend/app/       API、领域规则、MongoDB约束、事务与聚合
frontend/src/      Vue页面、编辑器、填写与统计
frontend/tests/    实际组件与会话回归
tests/             领域、真实数据库/API、并发及启停测试
scripts/           安装启停、教学样例和索引实验
docs/              设计、报告、需求追踪、AI记录与实测结果
.env.example       配置字段示例（不含真实密钥）
requirements.txt   锁定的Python依赖
```

第一阶段开发历史保留在 `docs/开发记录/` 和 [测试报告](docs/test-report.md)。第二阶段当前范围、实际结果和待完成项见[进度与待办](docs/phase2-progress.md)及[共享界面说明](docs/phase2-sharing-ui.md)。
