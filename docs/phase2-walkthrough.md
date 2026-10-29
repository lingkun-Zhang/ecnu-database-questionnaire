# 第二阶段演示讲解与自查

这份提纲按八至十分钟安排演示，先用一个例子讲清设计，再展示页面。示例沿用[连续交付验收](phase2-delivery-review.md)的数据。完整操作见[演示步骤](phase2-demo.md)，独立HTTP复验见[复验说明](phase2-verification.md)，各项需求的完成情况见[逐项核对](phase2-acceptance.md)。

## 先记住一个场景

同一道数字题，V1允许0到7整数，V2改为1到5整数。作者有问卷A和B，接收者有问卷D：

| 问卷 | 固定的题目版本 | 已接受的答案 |
|---|---|---|
| 作者A | V1 | 0 |
| 作者B | V2 | 3 |
| 接收者D | V1 | 3 |

D的所有者另行授权汇总后，V1有0和3两份答案，总和3、平均1.5；V2单独显示3。作者的原始明细和CSV只有A的0、B的3，D的3只参与汇总。这里要分清题目版本和问卷发布版本：这次D重新发布过，所以三份发布问卷共有四个发布快照。

演示前用测试账号准备好这三份问卷，核对发布状态和答案数量。试讲时可以使用填写预览；反复正式提交会改变统计数字，填写预览则不会保存答卷。

## 八至十分钟怎么展示

1. **第0至1分钟：介绍第二阶段改了什么。** 第一阶段已经能编辑、发布、填写和统计问卷；第二阶段增加独立题目及其复用和版本管理。打开题目管理，再看原有问卷编辑入口。
2. **第1至3分钟：展示固定版本。** 打开V1/V2历史、A/B问卷来源和题库成员。A固定V1，作者出新版或题库换版都不会改变A；B固定V2，填写0会被校验拦住。
3. **第3至5分钟：展示修改和恢复。** 查看影响检查的确认、取消，以及恢复记录：V3复制V1内容，恢复前的当前版是V2。如果演示数据已经有V3，直接展示记录即可；再次恢复会产生后续版本。
4. **第5至7分钟：换到接收者账号。** 查看D的固定引用和独立汇总授权，再回作者账号看V1的0/3汇总及本人明细。结合页面说明题目使用权、汇总权和原始答卷访问权的区别。也可展示撤销共享后D仍保留旧绑定的状态。
5. **第7至9分钟：解释统计和测试。** 两个内容版本分别计算，每份问卷固定自己的提交序号。打开对应测试和结果，说明真实API、受控故障测试、页面检查各自覆盖了什么。
6. **最后1分钟：说明取舍。** 简述下面三项需求解释及尚未测试的范围。演示结束后使用正常停止入口保留数据。

任务书有三处细节需要说明：本系统将“不创建新版”解释为取消保存或恢复，已有内容版保持不可变；他人问卷用稳定脱敏代号展示引用；跨卷统计先核对授权，再按内容版本分别解释。这些是当前实现的选择，尚未得到教师逐项确认，理由见[需求解释](phase2-acceptance.md#已确认的规则与采用的解释)。

## 五个常见追问

### 为什么需要三种题目标识

`questionId`表示是哪道题，`questionVersionId`表示用了哪一版内容，`instanceId`表示在这份问卷的哪一处出现。同一道题可以在一卷出现两次，两处的必答和跳转设置可能不同，因此需要各自的实例标识。存储和校验见[问卷服务](../backend/app/services.py)、[数据库结构](../backend/app/database.py)，也可对照[固定来源测试](../tests/test_question_sources.py)中的`test_same_bank_question_can_have_two_local_instances_and_local_routing`。

### 恢复V1为什么不直接把当前指针改回V1

系统把恢复记为一次新操作：V3复制V1内容，`parentVersionId`指向V1，`previousCurrentVersionId`记录恢复前的V2。这样既保留V1/V2，也能查到这次恢复的来由，问卷原来的固定引用照常保留。实现见[题目服务](../backend/app/questions.py)的`restore`与`_append_version`。[恢复测试](../tests/test_question_restore.py)中的`test_restoring_old_content_creates_v4_without_rewriting_history_or_survey_references`先建到V3，再恢复成V4，验证的是同一规则。

### 题目共享了，为什么还不能看D的答卷

共享题目允许接收者在自己的问卷里选题。D收集的答卷仍由D的所有者管理，只有另行授权后才参加题目作者的汇总，而且不开放逐条回答。作者原始明细仍只来自自己的A/B。实现见[共享服务](../backend/app/question_sharing.py)，以及[统计服务](../backend/app/question_statistics.py)的`set_grant`、`validate_scope`和`raw_rows`；对应[统计测试](../tests/test_question_statistics.py)中的`test_use_sharing_does_not_grant_answers_and_survey_owner_can_authorize_aggregate`。

### 为什么分版本统计，还要固定提交序号

V2可能改变题意、选项或量纲，所以0/3的均值1.5只解释V1，不能直接混入V2。固定提交序号则保证翻页时汇总和明细使用同一批数据。

序号还必须按问卷分别记录。例如另一个测试中E截至序号3、F截至2，F后来新增的序号3不属于旧范围，即使它不大于E的3。实现见[统计服务](../backend/app/question_statistics.py)的`create_scope`与`summary`，案例见[统计测试](../tests/test_question_statistics.py)中的`test_multi_instance_routing_numeric_golden_and_independent_survey_boundaries`。总体均值应按总和除以有效作答数计算，不能直接平均各问卷的均值。

### 测试覆盖到了哪些情况

本轮完整批次为后端533项通过、3项权限相关跳过，前端216项通过；251个子测试单独记录。故障测试在真实MongoDB事务钩子或驱动返回边界抛异常，检查回滚和原请求恢复，尚未做实际断网实验。67个连续HTTP请求、两个账号的代表页面另有记录，浏览器检查只覆盖其中部分操作。详见[当前结果](evidence/phase2-review-results-20261029.json)、[共享恢复测试](../tests/test_question_share_recovery.py)和[交付复验](phase2-delivery-review.md)。

## 试讲后的自查

合上本文，试着解释四件事：A为什么仍能接受0，恢复为什么不改变A，D的3为什么出现在汇总却不出现在作者明细，以及F的新序号3为什么不能混入旧范围。如果某一步还讲不清，可以回到对应测试看输入和断言，再结合页面走一遍。
