# 原始需求、实现位置与验收追踪

本表把任务书需求连接到当前代码和 V4 的 T01—T25 验收编号。T 编号是稳定的验收标识，不等于已经执行通过的测试名称；实现存在、自动用例存在、实际执行通过、人工核验完成必须分别记录。

当前状态（2026年10月22日）：第一阶段主要业务、干净目录安装、连续演示、正式报告及索引执行计划实验均已完成。正式报告见Word版（本地完整材料）及[文本版](phase1-report.md)，安装与演示见[demo.md](demo.md)，索引实验见[index-experiment.md](index-experiment.md)。10月22日报告补充单选、多选跳转的8组实例，已由现有领域函数核对，见[routing-examples-20261022.json](evidence/routing-examples-20261022.json)。第二阶段未实施。代码、测试及脱敏文档已上传GitHub；正式PDF留待课程平台提交。

10月21日最终回归批次为后端162通过、3项权限相关跳过、251个subtest通过；前端43通过，类型检查和构建通过。对应原始结果见[final-results-20261021.json](evidence/final-results-20261021.json)和[test-report.md](test-report.md)。后续专项结果单列，不与这批数量相加。下文保留各轮证据来源，历史轮次中的未测范围只描述当时状态；当前完成情况以各需求行及[完成情况.md](完成情况.md)为准。

10月22日补充保存/发布等待期间的编辑保护：编辑区锁定到写入及必要读回全部结束，重复操作和导航增加忙碌守卫，新增15项前端回归后合计58项全部通过。本轮执行结果及原生控件浏览器检查状态见[test-report.md](test-report.md)文末，不混入10月21日批次。

2026年10月20日第八轮根据原始XML和代码回填既有证据，当轮未新跑测试；第九轮新增匿名筛选及多页集合核对。首轮全量为77项及63个subtest通过；下表未注明后续批次的已执行API/数据库/领域用例均来自evidence/evidence/initial-tests.xml，具体方法名位于tests/。后续专项单独记录：T12页面见开发记录05，T21权限/会话/CSV见开发记录06，T15恢复见开发记录07，填写方式筛选及T22见开发记录09。

代码依据为 backend/app 下的 main、services、domain、database、repositories、security、statistics；纯领域用例位于 tests/test_domain.py，数据库组件用例位于 tests/test_database.py。页面和演示结果以实际运行记录为依据，相关证据按场景列于下表。

## 1. 教师需求到实现

| 需求编号 | 原始要求 | 当前实现位置与数据 | 对应验收 |
|---|---|---|---|
| REQ-AUTH | 注册、登录、注册时间、只管理本人问卷及统计 | main 的 /auth/register、/auth/login、/auth/me；security 用户名规范化/Argon2/令牌；services.owned；users.usernameKey 唯一索引；管理、版本、统计、明细、导出均先验证所有者 | T01、T21 |
| REQ-SURVEY | 标题、说明、匿名选项、问卷列表 | services.create/save_draft；surveys.draft；main 的所有者问卷列表和详情 | T02、T09 |
| REQ-SURVEY | 发布、截止、关闭、填写链接 | services.publish/settings_update/close/public/start；survey_versions；publicId、deadlineAt、status；发布与主指针在同一事务 | T10—T12、T18—T19 |
| REQ-TYPE | 单选、多选、填空；文本与数字填空 | domain 的 single/multi/text/number 判别及 config；database.QUESTION_SCHEMA/ANSWER_SCHEMA；发布快照保存完整定义 | T02、T03 |
| REQ-VALID | 必答、多选数量、文本长度、数字范围与整数 | domain.validate_definition、_normalize、evaluate；API 严格模型；服务端只从规范化有效答案生成存储对象 | T03、T07—T08 |
| REQ-FLOW | 按单选、多选、数字配置跳转，默认按顺序 | domain 条件语法、priority、defaultTarget、完整默认+条件图判环；main 的 preview 与填写 evaluate 使用同一核心；显示顺序与执行流分开 | T04—T05、T07—T08 |
| REQ-SUBMIT | 登录填写、匿名、多用户填写、每次提交保存 | services.start/fill_claims/submit；responses 一次成功提交一份；匿名省略 identity；response_view 管理白名单 | T12—T15、T21 |
| REQ-SUBMIT | 按设置允许/禁止同人多填 | surveys.allowMultiple/policyLocked；事务内 submission_gate；participantKey；oneTimeParticipantKey 部分唯一；跨版本按问卷限次 | T16—T17 |
| REQ-STATS | 整卷和单题；单选人数、多选次数、全部文本、数值均值与原始值 | statistics.get_stats/build_stats_pipeline/format_stats；main 的 stats、responses（questionId 筛选）与 export；版本及 asOfSeq 固定范围 | T06、T22 |
| REQ-MONGO | 业务持久化全部使用 MongoDB、解释数据库设计 | 四集合；database.init_database 的真实校验器/索引；repositories 异步事务；schema.md 说明嵌入、引用、约束、代价 | T13、T16—T20、T23 |
| REQ-TEST | 创建、加题、跳转、校验、提交、统计测试及步骤/结果 | 领域/真实数据库/API/并发及最终全量已有原始结果，最终干净实例完成六类核心连续流程；demo.md及测试报告包含步骤/输入/预期/结果 | T01—T24 |
| REQ-DOC | 代码、启动、设计、API、数据库、关键逻辑、报告 | protocol.md、schema.md、api.md及api-openapi.json、本追踪表；正式报告、干净安装/演示与MANIFEST基线均已完成，见phase1-report.md及demo.md | T24—T25 |
| REQ-AI | 工具、提示词、产出、人工修改与验证；真实总结 | ai-log.md记录连续AI分工、真实问题与验证；正式报告已汇总方案取舍、AI错误及修正过程，10月22日修订保留实际人工参与情况 | T25 |
| REQ-EVOLVE | 第二阶段在原实现基础上扩展并保留旧数据/功能 | schemaVersion、draftRevision、不可变 versionId；canonicalizationVersion；版本与答卷引用；领域不依赖 HTTP/数据库 | 第一阶段基线冻结，第二阶段另执行变更与回归 |

上述需求不能因为实现了强化功能而省略。例如“统计有均值”不能替代全部文本和逐份数字明细；“页面隐藏按钮”不能替代 API 归属检查。

## 2. V4 强化成果到实现

| 强化成果 | 当前实现边界 | 当前证据与适用范围 |
|---|---|---|
| 不可变版本 | 发布快照只插入，引用具体版本，发布 CAS 和 publishKey | 原重放/回滚、旧凭据、V1/V2页面和第十六轮当前版本竞争证据保留；第十八轮真实页面复制V1→r3、编辑保存r4→V3通过，原两版/答卷完整文档不变，统计为V1均值110、V2均值90、V3零份 |
| 可解释规则 | 当前题条件、优先级、trace、interactive/final共用核心；第十轮字段定位，第十一轮非阻断设计提示 | 领域/API一致性、页面分支、7类编辑定位/JSON兜底及五类设计提示有证据；10月22日报告补充单选/多选8组配置与结果核对；分析能力范围见protocol.md与开发记录/11-规则设计检查.md |
| 可靠提交 | gate真写、事务、序号、多填锁、幂等恢复、匿名白名单 | 既有并发/截止/恢复、第十三轮T14提交边界及第十四轮T17提交先持有gate的成功/回滚政策竞态已有实际证据 |
| 可核算统计 | 分版本、填写方式、固定范围、多选人数与次数分开 | 六份样例及空范围已有；第九轮新增三模式多页明细/CSV集合一致性、去重人数、零值未答和空集合；真实页面及实名CSV已验 |
| 可复现工程 | 锁定依赖、本地配置、安全启停、身份检查、初始化 | 第十二轮启停/数据保留及最终新目录安装已通过，见demo.md与final-*；五类查询的真实执行计划、结果对照和计时已完成，见index-experiment.md；写入吞吐及多档并发压测为可选未实施项 |

## 3. T 用例索引与执行边界

| T 编号 | 验收内容 | 当前代码、执行证据与适用范围 |
|---|---|---|
| T01 | 注册登录、重复用户名、未登录拒绝 | 原规范化/归属及页面注册证据保留；第十五轮test_registration_race.py两函数/3用例通过：两组8个同规范名同时准备插入，均1个201及7个409；胜者密码绑定、等价用户名登录、不同规范名并发及既有四集合文档核对，见开发记录/15-并发注册.md |
| T02 | 创建、加四类题、保存重开 | test_draft_cas_and_publish_cas_idempotency及六份样例API经过真实保存/发布；开发记录02有创建、六题定义、保存发布及填写。最终干净实例已从空白逐个点击添加四种题型、配置约束、保存重开，见demo.md及final-demo-* |
| T03 | 类型、整数、范围、多选数量、Unicode、0 | test_numeric_invalid_values、test_integer_float_and_negative_zero_canonicalization、test_multi_count_duplicates_and_strict_type、test_unicode_trim_and_codepoint_length；领域单测已执行 |
| T04 | 条件跳转、组合与优先级 | test_age_boundary_and_skipped_required、test_priority_not_array_order、test_current_question_combination_and_closed_interval、test_multi_operators；领域单测已执行。第二十轮真实页面建立≥18且（≤60或=80）与优先级5的=30例外，核对7个数值、且/或切换、子条件增删、保存重开；新增5项真实组件setup回归，见开发记录/20-组合条件编辑.md。10月22日报告单选/多选8组实例另经领域函数核对，见evidence/routing-examples-20261022.json；不计作新增pytest或浏览器用例 |
| T05 | 默认边/条件边判环及合法显示前跳 | test_invalid_target_and_default_cycle、test_unreachable_cycle_is_still_rejected、test_display_order_can_differ_from_flow；领域单测已执行 |
| T06 | 六份样例、空统计、零次选项、文字及数字明细 | 领域test_six_fixture_independent_statistics、DB test_six_samples_are_counted_by_mongodb/test_fixed_sequence_and_empty_statistics、API test_six_response_fixture_statistics_details_and_privacy均已通过。页面另有开发记录02/03/05/06小量核算 |
| T07 | 回改后旧分支失效 | 领域test_inactive_invalid_old_answers_are_ignored及API test_preview_evaluation_submit_and_inactive_old_values_agree通过；开发记录03真实回改18→17，旧分支不保存/不统计 |
| T08 | 预览、实时求值、提交一致 | API test_preview_evaluation_submit_and_inactive_old_values_agree、test_pending_required_answer_is_not_default_branch通过；领域暂停/最终错误用例通过；页面分支证据见开发记录02/03/04 |
| T09 | 草稿冲突 | test_draft_cas_and_publish_cas_idempotency已通过：同修订两次PUT，后次409，无覆盖；不是已完成双浏览器并发演练 |
| T10 | 发布前草稿变更/当前版本变化 | 原旧draftRevision用例保留；第十六轮test_publish_version_cas.py两函数/5项通过：修订相同、4类当前版本预期不符409，四集合不变；两事务同基线竞争只生成V2，失败方更正后V3且编号连续，见开发记录/16-并发发布.md |
| T11 | 发布幂等、插入后失败回滚 | CAS测试确认同publishKey 201/200及相同结果；test_publish_transaction_rolls_back_snapshot_and_pointer在after_version_insert注入失败，版本0/指针空，已通过。没有注入发布HTTP回执丢失 |
| T12 | V1 填写后发布 V2 再交 V1 | test_old_version_fill_survives_v2_and_repeat_policy_spans_versions 包含在首轮77项测试中；开发记录05 已通过真实页面验证旧V1提交110、新V2拒绝110并提交90、历史快照及分版本统计 |
| T13 | 同键同内容并发二十次 | test_twenty_identical_requests_have_one_receipt已通过：20并发相同请求，同回执，1份答卷、序号1；不是仅唯一索引组件证据 |
| T14 | 同键修改版本/答案/匿名 | 既有匿名冲突与test_submission_boundaries.py新增4项已通过：9类改答案、相同定义跨版本、10类等价恢复、不同内容竞争及重试。逐次四集合完整文档不变，胜者序号1；开发记录13及原始输出可核对 |
| T15 | 成功重放、过期/关闭/重启 | 开发记录07：API真实入库后注入ReadError，在同配置新应用生命周期重新登录、问卷关闭、无填写凭据下重放成功；既有过期凭据重放专项也通过。浏览器真实201被包装器替换503，同标签退出刷新登录后原key恢复200，回执相同且总数/序号均1。前端新增2项回归通过；这是应用响应故障及生命周期重建，尚非TCP断连或操作系统重启 |
| T16 | 禁/允多填、跨版本限制 | test_twenty_distinct_keys_respect_multiple_policy两参数已通过：20不同键禁多填1份、允多填20份；old_version用例覆盖跨版限制；六份样例另证人数去重 |
| T17 | 首次提交与政策修改竞态 | 设置先完成的既有测试通过；第十四轮test_policy_race.py两函数/三个用例通过，提交先持有gate，currentOp确认修改已到达Mongo后才放行；成功后两方向均409/POLICY_LOCKED，截止回滚后200且无残留，后续实际限次符合政策 |
| T18 | 关闭与提交竞态 | test_close_and_submit_controlled_order两参数已通过：提交先持有gate再并发close；关闭先完成再提交。已有真实事务证据 |
| T19 | 截止前/恰好/截止后及写等待 | test_deadline_boundary_uses_server_clock三参数、test_gate_wait_crossing_deadline_aborts_all_writes及事务重试跨截止均通过，拒绝时无答卷/序号/政策锁残留 |
| T20 | 冲突重试与未知提交结果 | test_transaction_recovery.py三项均通过：事务体瞬态重试/回滚、重试跨截止、真实commit后合成UnknownTransactionCommitResult仅重试commit。是驱动边界注入，非TCP断网；第七轮另有HTTP回执故障恢复 |
| T21 | 归属、匿名、实名字段来源 | 首轮API包含读取归属/匿名/导出；第六轮新增非所有者5类写入口404且数据库不变、公开投影隐私检查，另通过6项前端会话回归；真实浏览器复现并修复账号缓存残留，核对第二账号列表、匿名/实名明细与实际CSV。见开发记录06；不等于所有权限攻击或故障场景均验收 |
| T22 | 固定范围统计、分页、导出 | 既有固定统计及CSV公式用例通过；第九轮test_respondent_cohorts_share_frozen_statistics_cursor_and_csv在三种填写方式下遍历多个非空cursor页，冻结范围后追加答卷，比较明细回执集合=CSV集合且份数=统计；无重复遗漏。第十九轮真实页面验证全卷20→40→45、实名20→22、匿名20→23、备注20→24；追加46保留旧范围，刷新后备注25/数字46，单题空结果提示修复，见开发记录/19-统计筛选与分页.md |
| T23 | 校验器、索引、事务失败 | 既有组件和事务证据保留；第十七轮test_collection_constraints.py三函数/8项通过：60个缺字段路径与19类非法结构三形式共237次121，逐次四集合全文不变，合法/可选字段/long及double零值通过；同批17项及XML见开发记录/17-数据库字段约束.md |
| T24 | 干净环境启动和演示 | 最终新目录无旧环境/配置/构建，重新安装、新库连续演示及seed真实导入通过，见demo.md与evidence/final-* |
| T25 | 文档、需求映射、AI 日志 | 本表、protocol/schema、api.md及OpenAPI快照、连续AI日志完整；正式报告已汇总干净安装、最终回归、演示和索引实测，10月22日补充配置实例与设计取舍；Word版13页排版检查见report-quality-20261022.json |

## 4. 已执行事实与报告要求

本节按执行时间保留历史记录。下列“本轮未测”等措辞限定于相应轮次，后续补齐情况见第3节和文末最终验收记录。

早期实现轮曾在项目 `.venv/Scripts/python.exe` 执行领域unittest，44项通过；随后首轮完整pytest为77项及63个subtest通过，包含真实MongoDB。BSON回归实际编码/解码超过32位的数字约束和优先级。领域测试本身不连接数据库。T编号不是测试计数，一个T可以需要多层证据。

数据库测试使用独立受控的真实MongoDB测试库。上表回填的是已保存的实际结果；后续新增测试与浏览器/性能实验仍必须分别执行记录。第八轮仅核查已有证据；第九轮结果单独保存在anonymity-*，不冒充新的整套后端全量。

每个 T 的最终记录至少包含：前置环境和版本、具体操作或请求、输入、数据库前后状态、预期、实际输出、通过/失败、时间、相关代码版本和原始证据路径。发现未完成项需明确保留，不以“理论可行”代替验收。

第十轮补充编辑定位证据：开发记录/10-编辑错误定位.md及evidence/editor-*包含15项helper、14项会话/状态、7类实际预览字段聚焦、发布错误、JSON兜底/结构保护和错误清除。后端没有修改或重跑；其现有字段协议被实际页面使用，不将本轮前端29项计入此前后端结果。

第十一轮F03对应T04/T05强化：test_design_checks.py新增27项纯分析测试，test_design_checks_api.py新增4项真实Mongo/API测试，与原44项领域回归合计75项及233个subtest通过。前端会话17项、定位15项和构建另计。实际页面五类提示、定位/键盘、修改清除、限定范围零提示、45分流预览及带提示发布均有证据；新增独立问卷一份/V1一版，无新增答卷。见开发记录/11-规则设计检查.md及evidence/design-*。当前没有重新执行全部API/事务/数据库测试，不据此扩大T14/T17/T23或最终交付结论。

第十二轮F04：test_runner_control.py与test_lifecycle_scripts.py共26项通过、3项真实符号链接权限跳过、18个subtest通过；脚本身份夹具49个场景/86次断言属于其中一个pytest，不另计独立用例。9步真实启停验收通过，原有2账号/5问卷/5发布版本/6答卷及配置完整摘要一致，未增加业务文档。见开发记录/12-启动停止与重启.md与evidence/lifecycle-*。这只补足T24的本机启停证据，不代表无依赖的新环境安装、所有业务回归或性能已完成。

第十三轮补齐T14，4项新增与7项相关回归合计11项通过，5.41秒，0失败/跳过。数据来自随机隔离库，演示业务库和配置前后摘要一致；代码、状态及并发屏障证据见开发记录/13-重复提交与内容冲突.md与evidence/submission-*。本轮不修改业务代码，不把请求内分支数量当成独立pytest，也不推定T17政策竞态已完成。

第十四轮补齐T17，3个新增参数用例与9项相关回归合计12项通过，6.25秒，0失败/跳过。除状态码外，还比较四集合完整文档、政策字段、修订、一次性参与者字段及后续新标识提交。演示业务库与配置摘要一致，未修改产品代码。见开发记录/14-首次提交与多填设置.md和evidence/policy-*；currentOp仅为本地测试观察，不新增业务权限要求，也未测所有并发交错或外部Mongo部署。

第十五轮补齐T01并发注册子场景，新增3项与3项相关回归共6项通过，5.20秒，0失败/跳过。测试只在insert_one入口包装同步屏障，放行后原PyMongo方法执行真实写入和唯一约束；没有伪造返回或依赖sleep推测先后。两组碰撞分别覆盖大小写/全角/空白和ß/SS的casefold扩展，正向例不合并不同规范名。完整文档核对及演示库前后摘要见开发记录/15-并发注册.md与evidence/registration-*；不是全Unicode、注册容量或完整安全审计结果。

第十六轮补齐T10独立当前版本预期子场景，新增5项与3项发布相关回归合计8项通过，3.62秒，0失败/跳过。同步屏障在真实事务读取校验之后、原版本插入之前，使两个请求都准备生成V2，再委托原方法执行；只出现一个V2，草稿修订及原实名答卷不变。失败key未被占用，成功key绑定参数不能更改，后续V3发布后旧成功仍恢复V2。四集合、演示库和配置摘要及范围见开发记录/16-并发发布.md与evidence/publish-cas-*；未模拟发布HTTP回执丢失或穷尽所有调度。

第十七轮补齐T23的缺字段及关键集合结构约束，新增8项与9项相关回归共17项通过，6.88秒，0失败/跳过。60个必需路径含41个顶层与19个嵌套，三种写入形式180次拒绝；其他三集合19类结构57次拒绝，总237次均由Mongo返回121，每次四集合全文不变。237是内部操作，不叠加成pytest项数。合法写入、允许省略的匿名字段及实际BSON long/double零值为正向例；原演示库和配置摘要不变，见开发记录/17-数据库字段约束.md及evidence/constraints-*。

第十八轮完成历史复制页面专项：独立随机库与临时本机服务中，通过真实浏览器点击复制、编辑标题、保存及发布，三个写入200/200/201，草稿修订2→3→4、版本1/2→1/2/3。完整文档核对只出现预期草稿/指针/新快照变化，原用户/历史版本/两答卷不变；页面分版统计V1匿名110、V2实名90、V3零份，新旧填写分别绑定V3/V2。没有新增pytest计数、构建或产品修复；清理、原演示库摘要及边界见开发记录/18-历史版本复制与发布.md与evidence/history-*。

## 5. 第一阶段结束时冻结

冻结实际代码版本、依赖、API 示例、MongoDB 校验器和索引脚本、发布定义、规范化版本、六份核算数据、真实测试报告、运行说明及 AI 日志。第二阶段逐项记录新增需求、影响范围、旧数据兼容或迁移、回滚及回归结果。

第十九轮补齐T22的页面分页/单题筛选证据，21个页面检查点及29次真实读取独立核对通过；不是新增pytest数量。修复一处题目空结果被误称为全卷无答卷的提示，类型检查/生产构建及新构建页面复验通过，原演示数据和配置不变。具体输入、预期、结果、清理和未测范围见开发记录/19-统计筛选与分页.md及evidence/pagination-*。

第二十轮补充T04及T08的代表性组合编辑/预览、T02保存重开证据：真实页面创建≥18且（≤60或=80）及=30高优先级例外，7次数值预览、保存r2、刷新重开及再预览30/80均正确。修复且/或切换丢失配置，新增5项组件逻辑与原32项前端回归同批37项通过，类型检查/构建通过；11次真实预览无写库，1次保存仅改预期草稿字段。原演示库及配置摘要不变，临时库核验恢复清理；不扩大T05图判环或所有题型/深度、最终全量结论。见开发记录/20-组合条件编辑.md与evidence/combination-*。

第二十一轮补充T18/T19页面证据：关闭取消无请求，确认后设置禁用；实际时钟跨2026-10-21 16:28，旧答卷预检409且原因/31或41保留，新邀请直接禁用，旧邀请被拒后更新状态，原统计各1份/均值10和20。修复公开状态未使用及预检错误被通用校验覆盖，增加页面内二次确认及目标/修订保护。13个页面检查点、28次真实服务观察通过；新增6项前端与原37项共43项通过，类型检查/构建通过，两测试库正常清理，原演示库/配置不变。没有发最终submit、重跑后端pytest或声称完成T24；见开发记录/21-关闭问卷与截止提示.md与evidence/availability-*。

2026年10月21日按用户“主要看打分点”的指令收敛范围：评分为MongoDB35、后端25、测试10、文档15、AI说明15。正式报告初稿已按五项组织已有证据；优先完成T24及最终回归/提交包，额外布局与扩大性能/故障矩阵列可选，不新增硬门槛。本次仅整理文档，没有新增测试通过数或改动产品代码。

第一阶段最终验收（2026-10-21）：后端162通过/3权限跳过/251个subtest通过，42.32秒；前端43通过，类型/构建成功。T02逐项加题及T24新目录安装/新库演示已补齐。原演示库/.env摘要一致；提交包按MANIFEST.json固定第一阶段，不含第二阶段新增需求。

后续材料增强（2026-10-21至2026-10-22）：独立库索引实验已完成，五类实际查询在默认计划、指定已有索引、强制全扫三种方式下结果一致；报告补充单选/多选8组跳转实例及事务、版本和扩展取舍，Word版13页已检查。对应证据见index-experiment.md、evidence/routing-examples-20261022.json和report-quality-20261022.json。这些实验与文档核对未改变上述全量回归的计数。
