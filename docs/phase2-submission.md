# 第二阶段交付说明

按助教要求，课程平台只提交PDF，代码通过GitHub仓库查阅。报告已由本人提交，仓库同步提供[提交版PDF](2026_ECNU_PJ1_2_张凌琨.pdf)和[Markdown正文](phase2-report.md)。当前报告为13页，包含姓名、学号，AI使用说明已精简为两段。

第二阶段代码位于[phase2-question-versions分支](https://github.com/lingkun-Zhang/ecnu-database-questionnaire/tree/phase2-question-versions)。`main`保留第一阶段基线`99b66d1e637fa3b4ecb13719c0230ecb7e958701`，可用于比较两个阶段。运行方法见[README](../README.md)，实现与测试入口见[需求验收](phase2-acceptance.md)。

最近完整后端测试为533项通过、3项Windows权限相关跳过，另有251个子测试；前端216项通过，类型检查和构建通过。本次同步报告与说明，没有修改业务代码或重新运行测试。

本地交付包和历史Word保留在本机。真实配置、密钥、数据库、业务答卷、临时凭据、依赖缓存及构建产物不随仓库发布。报告中的详细证据目录已精简，原始测试记录和AI日志仍保留在docs中，供需要时查阅。
