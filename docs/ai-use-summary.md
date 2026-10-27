# AI使用说明与记录索引

本实验使用Codex辅助资料整理、方案讨论、代码生成、调试和测试。开发按阶段进行，每轮先完成一部分，再根据结果继续调整。这里汇总主要提示、方案来源及修改，较详细的过程保留在[AI使用日志](ai-log.md)中。

## 提出的要求与形成的产出

下表引号内为实际输入或反馈，代码路径指当前提交中的对应文件。

| 提示或反馈 | AI辅助产出 | 对项目的影响 |
|---|---|---|
| “还是参考一些资料哦，先设计一下大纲” | [V4设计大纲](系统设计.md)，包括集合、规则、版本和提交设计 | 先确定整体安排，再分模块开发 |
| “不要一次性做太多哦” | 分轮实现、测试和检查点记录 | 每轮控制范围，保留阶段结果 |
| “继续，我觉得你的前端做的AI味有点重，要不要先改一下” | App.vue、components/DefinitionEditor.vue、style.css的布局及文案调整 | 去掉宣传横幅和装饰，把问卷卡片改为列表，保留业务入口 |
| “你回忆一下评分点，不要太去内卷细节，主要看打分点” | 评分要求核对、报告及交付安排 | 停止继续细调布局，优先完善数据库论证、核心流程、测试与文档 |
| “只做完第一阶段哦”与“先不提交哦” | 第一阶段材料及本地归档 | 不提前实现第二阶段，不向课程平台提交 |
| “那增强吧” | scripts/index_experiment.py及索引执行计划实验 | 在前一轮索引增强建议的基础上，用实际查询检查扫描文档数量 |

本人的要求和调整主要落实在以上开发安排、使用效果和交付重点中。例如界面修改有明确反馈，修改前后的内容、涉及文件及验证结果可对照[第四轮记录](开发记录/04-页面调整.md)。这些反馈与AI生成的具体代码分别记录，便于看出一次调整是怎样完成的。

## 技术方案怎样形成

以下三项在AI辅助形成的V4设计稿中已有约定，后续通过实现、联调和测试逐步完善。现有记录能说明采用的方案及调整过程，但未保留讨论这三项方案的最早完整对话，因此不另行认定某个人首先提出。

| 设计 | 解决的问题及采用的方案 | 实现与后续检查 |
|---|---|---|
| 发布快照 | 草稿修改后，旧答卷仍需按原题目解释；发布时保存完整定义并切换当前版本 | backend/app/services.py的publish；第五轮补显示版本编号及按所选版本取统计标题；tests/test_publish_version_cas.py检查版本竞争 |
| 事务协调 | 提交、关闭和限次设置同时发生时需要确定顺序；相关操作更新同一问卷记录，并在事务内检查 | backend/app/repositories.py的submission_gate、services.py的submit；tests/test_policy_race.py检查提交先取得协调记录时的政策竞争及回滚，tests/test_api.py另检查设置先完成的情况 |
| 固定范围统计 | 查看统计后再翻页，新增答卷不应混入原范围；首次读取确定asOfSeq，后续查询沿用 | backend/app/statistics.py的get_stats和build_response_scope；第九轮统一匿名筛选范围，第十九轮检查冻结45份后追加第46份 |

日志中保留了实现阶段由Codex给数据库子任务的提示摘录：

> 创建四集合strict validators和命名索引；统计用同一只读snapshot事务固定submitSequence，再从原始responses聚合R/V/A/U/S、独立人数、选项与数字。不得返回内部参与标识。

这段是AI内部任务提示，和上表的本人输入分开标注。它对应数据库约束、索引与聚合代码，不作为本人原话。

## AI实现中出现的问题与修正

### BSON整数判断

早期整数类型判断未兼容BSON Int64，导致合法整数被拒绝。修正后接受整数子类，并继续排除布尔值。当前backend/app/domain.py中的整数判断为：

```python
def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)
```

tests/test_domain.py中的test_actual_bson_int64_roundtrip_remains_executable执行BSON编码、解码后，检查定义和答案仍可求值，同时确认布尔值仍被拒绝。问题和修正记在日志第一轮。

### 组合条件切换后丢失原值

原条件从“且”改为“或”时，已有子条件被重置。后续借助Codex修改转换逻辑，复用原子条件及嵌套结构。当前frontend/src/components/ConditionEditor.vue的相关代码为：

```typescript
function changeMode(mode:string) {
  const retained=group.value ? children.value : [props.modelValue]
  emit('update:modelValue',mode==='leaf' ? leaf() : {[mode]:[...retained]})
}
```

frontend/tests/conditionEditor.test.cjs直接编译并执行这个组件，检查且/或互换、嵌套条件、边界值和选项值保留。另有页面保存、刷新重开和预览检查，见[组合条件专项](开发记录/20-组合条件编辑.md)及[原始测试输出](evidence/combination-condition-tests-20261021.txt)。

## 随附文件

- [实验报告正文](phase1-report.md)：第九节概述工具使用、设计来源、反馈和问题处理。
- [AI使用日志](ai-log.md)：按轮次保存用户提示、AI任务提示摘录、产出及修改记录。
- [设计大纲](系统设计.md)：保留实现前的设计约定。
- [测试报告](test-report.md)：说明各轮实际测试的输入、结果和批次，链接原始证据。

这些文件与源码一起放在完整压缩包内。日志是主要开发过程的记录和提示摘录，不是全部原始对话的逐字转录。


2026-10-27新增跨问卷单题统计：主代理完成数据库/后端/授权界面与统一集成，辅助代理完成统计页面、定向及组件测试、只读审查、审计扩展和本步文档。只读复核代理发现筛选、编码字节上限和快照错误边界，主代理完成修正，统一执行回归及真实页面验收；夹具失败及修正如实记入ai-log。权限是用户实际确认的选择，不能写成AI自行决定或用户亲手实现。详见[跨问卷统计说明](phase2-question-statistics.md)。
