# 第十五轮检查点：规范化用户名的并发注册

日期：2026年10月21日。范围：补齐T01中“同时注册同一规范化用户名，只成功一个”的缺口，并核对密码与身份不会串入失败请求。现有逻辑通过，本轮只新增测试与说明，没有修改业务代码、前端、Schema、依赖或配置。

## 测试如何形成竞争

[新增测试](../tests/test_registration_race.py)通过真实ASGI应用及真实MongoDB执行。每例由既有fixture生成自己的随机测试库，预先创建1个既有账号、问卷、发布版本和实名答卷，用作不可被注册流程改动的基准；结束只清理该受控测试库。

测试使用临时包装器在该测试库users.insert_one入口暂停，不改文档和参数，放行后调用原PyMongo方法，真实执行写入、唯一索引仲裁及DuplicateKeyError处理。它不是产品新增hook，也没有伪造成功/失败响应。所有请求先完成真实用户名规范化和Argon2密码哈希，到达入口后才统一放行；放行前断言每个请求均未完成、四集合全文仍与基准一致，不靠sleep或任务创建顺序推断竞争。

同名组还检查真实索引unique=true、键为usernameKey；8个准备写入的文档ID均不同、规范查找键均相同。期望展示名与规范键是测试数据中独立列出的常量，没有调用被测规范化函数来生成预期值。异常退出也会释放屏障、取消/回收自建任务，并恢复原insert_one方法。

## 三项新增用例的实际结果

| 用例 | 并发输入 | 返回与实际新增账号 |
|---|---|---|
| 大小写、兼容字符及首尾空白 | Race_User的8种写法，含全角字母/下划线、全角空白、Tab/换行、Unicode首尾空白 | 1个201，7个409/USERNAME_EXISTS；只新增1账号 |
| Unicode casefold扩展 | Straße_1、STRASSE_1及其全角/大小写/空白组合，共8个；统一规范键strasse_1 | 1个201，7个409/USERNAME_EXISTS；只新增1账号 |
| 不同规范名的正向例 | Race_User、RaceUser、Race_User_2、全角Race_User_3，共4个 | 4个201，实际新增4个不同账号 |

不预设哪个请求胜出。本批同名组的胜者索引分别为1和2（从0计），完整请求状态顺序见原始结果；这不表示稳定调度或优先权。

每组同名请求使用不同密码：成功注册的令牌可读取本人me，存储文档ID、展示名、BSON注册时间与响应一致，密码以Argon2id哈希保存。8种等价用户名使用胜者密码均200并认证同一账号；7个败者密码均401/INVALID_CREDENTIALS。再用另一等价写法和新密码注册仍409，不改原账号。认证、失败密码和重试后，users/surveys/survey_versions/responses四集合全文与竞争结束时相同。

不同规范名组的4个注册令牌均能认证对应本人，各自密码登录200；使用另一账号的密码均401，确认没有把不同用户名合并。登录前后四集合全文相同。

所有用例均核对原账号和原问卷、版本、实名答卷全文不变；同名组只增加1个users文档，不仅检查409或集合总数。证据只保存合成场景标签、状态和计数，不写入密码、哈希、登录令牌、随机数据库名或内部标识。

## 执行批次与复现

新增两个测试函数展开为3个用例，另选跑3项既有回归：顺序注册/登录/所有者隔离、非所有者写入及公开投影隐私、数据库初始化重复执行与空草稿约束。**同批6项通过，0失败/跳过，5.20秒**。8个请求、登录检查和重试均是用例内步骤，不另算独立pytest项数。

在项目目录运行，需已有本项目MongoDB副本集，测试使用各自随机隔离库：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_registration_race.py tests/test_api.py::test_registration_normalization_login_and_owner_isolation tests/test_api.py::test_participant_cannot_mutate_owner_survey_or_see_private_public_fields tests/test_database.py::test_init_is_repeatable_and_empty_draft_is_valid -q -s --junitxml=docs/evidence/registration-tests-20261021.xml
```

- [原始输出](evidence/registration-tests-20261021.txt)和[JUnit](evidence/registration-tests-20261021.xml)。
- 实际结果摘要（本地完整材料），从已通过测试打印的三项结果提取，保留实际请求状态顺序和胜者索引。
- 演示库前（本地完整材料）/后（本地完整材料）完整规范BSON摘要与.env字节摘要一致，仍为2账号、5问卷、5发布版本、6答卷，没有新增演示业务文档。
- 本轮范围和文件摘要（本地完整材料），供同步核对，不代表第一阶段最终冻结。

本轮只覆盖代表性NFKC/空白/casefold碰撞及正确区分不同规范名，没有穷尽全部Unicode字符、视觉混淆、所有输入边界或账号安全攻击。8请求是正确性竞争测试，不是注册容量、吞吐或性能实验；没有重跑全部后端、前端检查/构建，也没有执行浏览器、干净安装或网络故障实验。

T01本轮缺口已补齐。下一步补T10中“草稿修订相同、仅当前版本预期陈旧”的独立发布冲突，再继续其余[收尾清单](acceptance-status.md)。第一阶段尚未全部完成。
