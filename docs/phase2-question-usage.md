# 题目使用情况查询

记录日期：2026年10月25日。本步从本地提交 `1576433` 继续，第一阶段基线 `99b66d1e637fa3b4ecb13719c0230ecb7e958701` 保留。前置能力见[固定使用题目版本](phase2-question-reuse.md)。

## 本步解决的问题

已有题目身份、不可变版本和明确来源之后，需要回答“某道题或某一版正在被哪些问卷使用”。查询直接读取草稿来源和发布快照来源，不另建一份需要同步的使用关系表，也不根据相同题干推断关联。

任务书的使用情况要求还涉及修改时的决定，以及共享后他人问卷的影响。本步完成私有题的使用查询后端基础；编辑时查看影响、决定派生版本的界面，以及共享后的最小信息展示仍需后续处理，不能将整条要求标为完成。

## 接口与结果口径

新增 `GET /api/questions/{questionId}/usages`，必须登录。参数如下：

| 参数 | 规则 |
|---|---|
| `scope` | `draft`或`published`，默认`draft` |
| `questionVersionId` | 可选，提供时必须是该题的真实版本；不提供则查询整个题目系列 |
| `limit` | 默认30，范围1—100 |
| `afterId` | 可选，上页返回的`nextAfterId`；长度1—160 |

题目及问卷都必须属于当前账号。无权访问的题目返回404 `NOT_FOUND`；题内不存在或属于另一题的版本返回404 `QUESTION_VERSION_NOT_FOUND`；非法参数返回422 `INVALID_REQUEST`。匿名请求沿用401认证错误。不能用已知版本ID绕过题目所有权。

响应共同字段为`questionId/questionVersionId/scope/limit/items/nextAfterId`。没有下一页时`nextAfterId`为null；没有匹配结果时`items`为空数组。下面是契约示例，尖括号代表运行时真实ID，不是实际请求记录：

```json
{
  "questionId": "<题目系列ID>",
  "questionVersionId": null,
  "scope": "draft",
  "limit": 30,
  "items": [{
    "surveyId": "<问卷ID>",
    "surveyTitle": "学习习惯调查",
    "surveyStatus": "open",
    "draftRevision": 4,
    "instances": [
      {"instanceId": "weekday", "questionVersionId": "<题目v1的ID>"},
      {"instanceId": "weekend", "questionVersionId": "<题目v2的ID>"}
    ]
  }],
  "nextAfterId": null
}
```

`draft`每行是一份问卷的当前草稿，同题多次使用汇总在该行的`instances`中。草稿换版或解绑后，结果随保存更新；未发布也可以查到。

`published`每行是一个不可变的问卷发布快照。同一问卷的多个发布版本可以分别出现，不能把行数称为去重问卷总数。每行返回`surveyId/surveyTitle/surveyStatus/surveyVersionId/versionNo/publishedAt/isCurrent/instances`；这里`versionNo`是问卷发布版本号。`surveyTitle`取快照里的冻结标题，`surveyStatus`取问卷当前状态，`isCurrent`只比较当前发布指针。关闭问卷不会使当前快照自动变成历史。

按题目版本筛选时，数据库匹配和返回的实例都要求题目ID、版本ID在同一来源元素中匹配。上例按v1查询只返回`weekday`，不能把另一实例的v2混进结果。查询不返回完整定义、共享用户、账号、公开链接、填写凭据、答卷或答卷统计。

## 分页与权限

草稿按`surveys._id`升序，发布快照按`survey_versions._id`升序。每次读取`limit+1`行，只返回前`limit`行，用最后一条已返回记录的ID作为下一页边界。边界可以不对应一条仍然存在的记录；服务端只按严格大于边界继续查询。

这是实时键集分页，没有冻结跨页快照。草稿编辑可能使记录加入或移出查询，新增UUID也可能排在已经翻过的边界之前；需要完整重查时从第一页开始。本接口不提供合计数，不将草稿数、快照数和问卷数混为一谈。`afterId`不是授权凭据，任意边界都不能扩大所有者范围。

发布快照没有冗余保存问卷所有者。聚合先精确匹配来源、排序，再通过问卷主键关联真实问卷并筛选所有者，最后才限制页数。因此他人记录、找不到所属问卷的记录都不会挤占本人页数，也不会泄露其标题。题目当前版本指针不参与匹配，历史使用不会因为题目产生新版而消失。

## 数据库设计与兼容

本步不增加集合或业务字段，不改变校验器、发布和答卷结构，不回填旧数据，也不改变`schemaVersion`。没有来源的旧问卷继续按原流程工作，并且不会因题干相同而被列入题目使用情况。

新增四个非唯一复合多键索引，均由既有`init_database`创建：

| 集合与索引 | 键顺序 | 用途 |
|---|---|---|
| `surveys.ix_surveys_question_usage` | `ownerId, draftQuestionSources.questionId, _id` | 题目系列的草稿使用 |
| `surveys.ix_surveys_question_version_usage` | `ownerId, draftQuestionSources.questionId, draftQuestionSources.questionVersionId, _id` | 指定版本的草稿使用 |
| `survey_versions.ix_versions_question_usage` | `questionSources.questionId, _id` | 题目系列的发布使用 |
| `survey_versions.ix_versions_question_version_usage` | `questionSources.questionId, questionSources.questionVersionId, _id` | 指定版本的发布使用 |

数组内题目和版本条件使用`$elemMatch`，保证同一元素匹配并允许组合索引边界，见[MongoDB多键索引边界说明](https://www.mongodb.com/docs/manual/core/indexes/index-types/index-multikey/multikey-index-bounds/)。下述计划以MongoDB 6.0.29实际输出为准，不把当前在线手册当成本机版本的实测。系列索引和精确版本索引的等值前缀不同，均在等值条件之后放`_id`服务分页；精确版索引不能仅因共享前缀就当作系列排序索引的替代。每份草稿或快照都需要维护两个新增的多键索引，代价是额外存储和写入维护；本轮没有测量存储增量或写入延迟。初始化还需实际建立索引，不能将索引构建说成零成本。

发布查询还会按现有问卷`_id`索引查找所有者；即使索引减少了来源候选，也不能把跨集合权限校验描述成覆盖查询。查询构建器在`backend/app/question_usage.py`，应用与复现实验共用，避免报告验证另一种查询。

## 验证与实际结果

本轮新增18项真实MongoDB/API用例，定向运行 `python -m pytest -q tests/test_question_usage.py --maxfail=3` 为18通过、0失败、14.92秒，随后已包含在完整回归中，不能相加。

完整运行 `python -m pytest -q --junitxml=.runtime/phase2-usage-tests-20261025.xml`：291通过、3跳过、251个子测试通过，0失败，128.46秒。3项跳过均为原有Windows真实符号链接权限用例。JUnit有294个testcase元素，tests属性545包含251个子测试，不写作545项独立测试。证据为[原始输出](evidence/phase2-question-usage-tests-20261025.txt)、[JUnit](evidence/phase2-question-usage-tests-20261025.xml)和[结果摘要](evidence/phase2-question-usage-results-20261025.json)。

用例实际核对两卷多版本、同卷多实例及按版投影、冻结标题、草稿换版和解绑、历史引用保留、当前指针移动和关闭状态、同名不同题与旧格式排除、匿名/越权、参数边界、双scope分页，以及题目与版本必须同元素匹配。专门通过绕过服务的合成数据构造外部问卷及孤立快照，证明它们不占用本人页数；这不是共享写入成功的演示。查询前后比较六集合全文，未改写业务文档。完整回归包含既有发布、填写、统计、题目版本和固定来源测试。

索引实验使用Python 3.12.14、PyMongo 4.19.0和MongoDB 6.0.29，固定种子直接生成4个合成账号、21题、42题目版本、1000问卷、2000发布快照、0份答卷。应用与实验共用查询构建器，实际集合校验器和索引均启用；部分来源被故意构造成语义异常，用于检验权限和同元素匹配，不冒称API允许这些写入。

复现命令：`python scripts/question_usage_experiment.py --mongo-uri <独立测试副本集URI> --output .runtime/question-usage-plans-20261025.json`。脚本不读取.env、不接受业务库名，随机建库并保存归属标记；结束时核实标记后仅删除自建库。结果见[完整计划](evidence/phase2-question-usage-plans-20261025.json)和[实验输出](evidence/phase2-question-usage-index-20261025.txt)。

| 查询 | 取回的容器数 | 默认/对应索引读取来源文档 | 强制全扫读取来源文档 | 精确版查询强制系列索引 |
|---|---:|---:|---:|---:|
| 草稿·题目系列 | 31 | 31 / 31 | 1000 | 不适用 |
| 草稿·指定版本 | 8 | 8 / 8 | 1000 | 42 |
| 发布·题目系列 | 31 | 340 / 340 | 2000 | 不适用 |
| 发布·指定版本 | 8 | 8 / 8 | 2000 | 340 |

31是应用取`limit+1`时的查询结果，实际API只返回前30行；8则表示已不足一页。四种查询共14次计划/结果组合，均与独立核算的容器及投影一致。默认计划在本数据上选择相应新索引，且来源游标没有阻塞排序。精确版索引减少了只用系列索引时的候选读取，支持分别保留两个索引的理由。

表格只统计来源集合游标，问卷所有权关联的读取单独保留在原始lookup字段中。发布系列查询仍读取340份来源候选，不能把最终31条容器当成扫描量，也不能说关联查询已经被覆盖。执行计划毫秒值仅为诊断，没有重复计时、存储增量、写入延迟或吞吐结论。

本轮pytest及数据库实验首次实际运行均通过。复核脚本时补齐了入口缺少的asyncio导入，然后才运行实验。原桌面项目163个已比较文件与分析时一致；没有业务迁移或数据回填。索引实验库与随机测试库已清理，专用服务已关闭。前端未修改，未重跑前端批次或浏览器流程；上一步64项前端结果仍是独立历史批次。未推送GitHub或提交课程平台。

## AI使用与后续

用户本轮指示为“继续开发”。主代理负责契约、查询、API、索引和统一验证；测试代理编写真实MongoDB/API回归；设计代理只读核对任务书、权限与分页边界；索引实验代理编写复现脚本并核对MongoDB官方说明。代码、测试及文档均按实际AI分工记录，不虚构学生手写、个人学习或测试结果。

后续需要补齐题目恢复、选题与版本管理界面，再在明确权限和版本口径下推进共享、题库管理与跨问卷统计。使用查询结果可以支持后续影响提示，当前不会自动修改任何使用方。
