# 第七轮检查点：未确认提交的回执恢复

日期：2026年10月20日。范围：答卷已保存但客户端未收到成功回执，保留原提交内容，在同一标签页退出、刷新、重新登录后恢复。此次未发现需要修复的业务代码问题，新增的是回归测试、可复现的隔离故障工具和验收证据。

## 实际结果

| 检查 | 实际结果 |
|---|---|
| 前端会话回归 | 8项通过、0失败、0跳过，约1.15秒；其中2项为本轮新增 |
| 真实MongoDB API专项 | 2 passed、25 deselected，0.86秒；其中1项为本轮新增 |
| 浏览器第一次提交 | 服务器实际201且已持久化；测试包装器将客户端响应替换为503，页面显示“确认提交结果” |
| 同标签页重新登录 | 退出、刷新、登录原测试账号后，恢复页显示相同提交标识 |
| 点击原标识恢复 | HTTP200，完整回执与最初成功结果一致，匿名和数字42不变 |
| 数据库核对 | 第一次及恢复后均为1份答卷，submitSequence均为1 |
| 恢复请求核对 | key、请求体、填写凭据SHA-256摘要一致，两次submit之间没有start/evaluate |
| 测试环境收尾 | 临时服务已停止；仅本工具生成的精确测试库被删除，并查库列表确认不存在；项目服务健康检查成功 |

本轮没有修改App.vue、api.ts或后端业务代码，没有新增依赖，页面沿用第六轮已构建版本。没有重新执行前端构建或第一轮全量77项测试；前述专项结果单独记录，不累加为一次新的全量结果。

## 三层验证各自证明什么

前端frontend/tests/session.test.cjs直接执行实际App.vue脚本和Vue响应式状态。新增用例确认提交503后，原key、版本、匿名选择、原始答案和填写凭据保留；新建组件实例共享同标签页存储，重新认证后恢复记录，重试仅发送原submit，不重新start/evaluate或生成新key，成功后清除待恢复记录。这是受控HTTP边界的状态回归，不是浏览器渲染测试。

API新增test_lost_committed_submit_response_recovers_after_new_app_login_and_close：使用真实MongoDB与实际ASGI路由，等201响应及数据库提交完成后，由HTTPX传输边界抛出合成ReadError。随后关闭问卷，在同配置、同数据库的新应用生命周期及新客户端中重新登录，不提供填写凭据，仍以原key和原请求体得到200及相同回执。前后答卷文档完整相等、总数1、序号1；匿名答卷没有实名identity。另选跑既有过期填写凭据与关闭后重放用例。这里重新建立的是应用生命周期，原测试fixture仍承担关闭与清理，不能写成操作系统进程重启或真实TCP断连。

浏览器使用tests/support/recovery_browser.py启动的隔离服务。工具先让真实业务请求提交并生成201，再记录数据库状态，将送给浏览器的HTTP响应替换为503；故障只发生一次。主代理通过页面完成填写42、匿名提交、退出、刷新、重新登录和恢复，没有用接口脚本代替这些页面操作。服务日志证明提交与重放的数据关系；恢复前后页面文本和截图记录实际呈现，二者互相补充。

## 浏览器证据标识

| 字段 | 实际值 |
|---|---|
| 测试问卷 | 回执恢复验收（独立测试） |
| publicId | 03ccf27f64914557b1786b71195239f7 |
| versionId | 2802cc57-2770-4c30-aa9e-d7adc6c9bc58 |
| 提交标识 | sub_3e28de68fbc7 |
| 原始及恢复回执 | 2c8c8cea-cdef-4543-9efc-93e6e12ea584 |
| 输入 | q_recovery = 42，匿名填写 |
| 独立测试库 | survey_test_0c383752cd784313aee36488b564cfe1（已清理） |

恢复成功页使用通用“答卷已保存”文案，没有借用当前公开问卷标题或猜测历史版本号。本轮的退出和刷新均在同一标签页进行；sessionStorage不承诺关闭标签页后或跨设备恢复。

## 证据文件

- evidence/recovery-session-tests-20261020.txt：8项前端回归原始TAP。
- evidence/recovery-api-20261020.xml：2项API专项原始XML。
- evidence/recovery-pending-20261020.png、.txt：503后待确认页面与原key。
- evidence/recovery-restored-20261020.txt：退出、刷新、重新登录后待恢复页面。
- evidence/recovery-receipt-20261020.png、.txt：恢复成功页与回执。
- evidence/recovery-browser-state-20261020.json：真实201、合成503、重放200的原始事件及已核验清理状态。
- evidence/recovery-browser-check-20261020.json：对事件、请求一致性和计数的断言摘要。

事件记录不含Authorization或可用填写令牌，只保留填写凭据SHA-256摘要；测试账号、数字42和回执均为AI生成的隔离验收数据。临时服务退出后，本地数据库曾处于停止状态；恢复项目原数据库后，按state中的精确随机库名补充清理并查验，而非把进程退出推定为清理完成。

## 如何复现

先启动本项目MongoDB副本集，在项目根目录执行前端npm run test:session（需进入frontend目录）及以下API专项：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_api.py -q -k "lost_committed_submit_response or idempotency_includes_anonymity" 
```

浏览器故障工具在项目根目录运行；输出目录必须是一个新的目录：

```powershell
.venv\Scripts\python.exe -u tests/support/recovery_browser.py --output .runtime/recovery-check-new
```

它复用MongoDB连接地址，但使用全新随机测试库、独立密钥及随机本地端口，不使用运行中业务库。打开READY输出的fillURL，以临时账号recovery_browser_test、密码RecoveryBrowser2026!登录，开始填写42并匿名提交。看到503后的恢复页时退出、刷新、重新登录，再点“使用原标识确认结果”。对比state.json的原始回执、两个submit事件及count/sequence；不要直接把恢复结果与自身比较。

验收后在同一项目根目录创建停止请求：

```powershell
Set-Content -LiteralPath '.runtime/recovery-check-new/stop.request' -Value 'stop'
```

正常退出时工具清理自己创建的测试库，并将state.json的cleanedUp写为true。若进程被强制终止，不应假定finally执行：先确认对应服务已停，再依据state中的精确随机库名核对并清理，不能批量删除其他测试库。

## 后续范围

本轮完成回执恢复这一项，尚未进行真实网络中断、操作系统重启恢复、完整多尺寸验收、万级数据性能实验或干净环境安装。关闭/截止等其他浏览器场景和正式项目报告仍按后续检查点推进。
