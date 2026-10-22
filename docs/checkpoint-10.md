# 第十轮检查点 编辑错误定位

日期：2026年10月20日。范围：完成V4的F02，将预览及发布检查中的定义错误定位到可修改控件；同时防止旧定义的错误误定位，并保护JSON导入时的渲染结构。本轮没有开发非阻断规则提示。

## 完成的行为

顶部定义错误列表增加“定位”按钮。点击或键盘回车后，从预览切回编辑题目，选中对应题目；入口错误切到流程概览；规则和嵌套条件按完整字段路径找到控件。等待组件挂载后滚动并聚焦，错误控件有红色边框和aria-invalid，aria-describedby指向实际错误说明。上下限整体错误同时标记两个边界，默认聚焦第一个。

resolver优先核对稳定题目/规则ID，修正当前索引；无ID时只按合法、可确认的字段索引定位。删除或重复ID、无可视化控件的稳定ID字段、未知字段及不可靠结构转JSON高级编辑，并保留原始路径，不猜测相邻控件。JSON缓冲中未应用的文字不会因再次点击定位而被覆盖。

错误上下文包含问卷、会话、定义快照和编辑代次。定义变化同步清除旧诊断、红框和定位入口，点击时再次核对；旧预览在下一次防抖请求发出前已经失效，不能将旧错误重新写回。预览回答更新也立即使旧求值失效。退出或换账号清掉上下文，延续既有会话隔离。

既有JSON应用只检查title/questions，可接受config=null、rules=null或condition=null，随后模板可能先失败而无法使用错误定位。本轮增加仅供可视化渲染的结构门槛：结构不完整时保留原JSON缓冲及当前已应用草稿，显示修正说明。空文案、重复ID、反向边界、非法优先级/去向等语义错误仍交后端检查，不在前端悄悄修正或冒充发布校验。前端结构保护的条件树安全预算为32层、整份10000节点；业务协议仍为后端规定的每树4层/100节点。

## 自动验证

| 验证 | 实际结果 |
|---|---|
| 定位及JSON结构helper | 15项通过，0失败/跳过，约0.58秒 |
| 会话与页面状态回归 | 14项通过，0失败/跳过，约1.56秒；原有12项及本轮新增2项 |
| 类型检查和生产构建 | npm run build成功；最终JS为index-BAsxSWq6.js，CSS为index-ru5YnBaz.css |

15项helper执行真实editorErrors.ts，覆盖题目/规则重排、全局规则ID归属、嵌套allOf/anyOf、元信息及入口、约束整体、索引回退、删除/重复ID、未知路径、单多选控件、输入不变性和坏结构/递归预算。它们不替代浏览器DOM验证。

2项新增状态回归执行实际App.vue脚本与Vue响应式对象，检查有效定义错误等待编辑器挂载后再定位、下一次编辑同步作废旧错误，以及防抖等待期间旧响应不能恢复错误；HTTP和组件ref边界受控。原有账号、回执、筛选、分页、导出隔离12项也通过。两类测试合计29项，不把先前执行次数重复累加。

本轮没有修改后端、数据库Schema、HTTP契约或依赖，也没有重跑后端35项或全量pytest。真实页面仍调用实际后端检查。

## 真实浏览器操作

使用原有AI测试账号browser_1020_01，创建唯一独立草稿“错误定位验收 2026-10-20”，通过JSON应用并保存三题定义，修订1。定义故意包含7个语义错误：数字上下限反转、嵌套条件区间反转、重复规则优先级、空选项文案、空题目名称、无效入口、无效规则去向。

在最终构建中重新加载并打开草稿，预览的真实后端422返回7项字段错误。逐项点击定位，读取真实DOM的activeElement、data-editor-field、aria-invalid及对应错误说明：

| 场景 | 实际聚焦字段 |
|---|---|
| 数字整体边界 | questions[0].config.min，min/max均标红 |
| 嵌套组合条件 | questions[0].rules[0].condition.allOf[1].anyOf[0].value.min，区间两端均标红 |
| 键盘回车定位优先级 | questions[0].rules[1].priority |
| 切换到第二题选项 | questions[1].config.options[0].label |
| 切换到第三题标题 | questions[2].title |
| 切换流程面板的入口 | entryQuestionId |
| 第二条规则去向 | questions[0].rules[1].target |

随后实际点击发布，后端仍拒绝，未生成版本；从发布错误再次定位空选项成功。输入“是”后立即确认旧错误按钮和aria-invalid控件数都为0，焦点仍在正在编辑的选项输入框。

另在未保存定义中加入unexpectedField，预览错误定位到JSON textarea；改变未应用的JSON缓冲后再次点定位，文字完整保留。将config改为null后尝试应用，页面保持JSON区、显示结构说明，缓冲和已应用草稿标题均不变。最后通过界面恢复最初的三题定义，回到可复现的错误定位示例，没有保存这些额外输入。

只读数据库核验：surveyId=8543109c-62eb-4a5f-a5c8-7663d0199c2c，publicId=d590e9a9ed7e479f87d2b4d454a2ca1c；status=draft、draftRevision=1、currentVersionId=null、发布版本0、答卷0、提交序号0，存储定义与原始验收输入一致。仅新增该独立草稿，未改既有三个测试问卷。

## 文件与证据

修改App.vue、DefinitionEditor.vue、ConditionEditor.vue、style.css、package.json、session.test.cjs；新增editorErrors.ts和editorErrors.test.cjs。test:editor为新增运行入口，test:session继续用于会话与页面状态。

- evidence/editor-location-tests-20261020.txt：15项helper原始TAP。
- evidence/editor-session-tests-20261020.txt：14项会话/状态原始TAP。
- evidence/editor-invalid-definition-20261020.json：三题、7个语义错误的准确输入。
- evidence/editor-errors-20261020.txt、editor-publish-errors-20261020.txt：真实预览和发布检查的页面。
- evidence/editor-focus-checks-20261020.json：7种预览定位、1种发布定位、1种JSON兜底的真实焦点/ARIA断言，及JSON缓冲保留。
- evidence/editor-cleared-20261020.json：编辑后错误控件/定位按钮为0。
- evidence/editor-json-fallback-20261020.txt、editor-json-guard-20261020.txt及.json：JSON兜底与结构保护。
- evidence/editor-database-check-20261020.json：独立草稿的只读核验。
- evidence/editor-location-20261020.jpg：选项输入框实际聚焦并标红的截图。

## 边界与下一项

定位针对有fieldErrors的预览/发布INVALID_DEFINITION；保存草稿的通用定义错误可能没有字段列表，仍显示普通说明，不推测具体位置。JSON诊断描述已应用定义，未应用缓冲的语义需要应用后重新检查。未知结构、ID冲突和集合整体等保守转JSON，不宣称所有非法输入都能在普通表单中编辑。

本轮验证了代表性真实控件、键盘回车及错误说明关联，没有宣称完成屏幕阅读器验收、全部浏览器/宽度或所有字段组合。实际编辑后旧错误作废；helper能够跟随稳定ID，不表示页面继续沿用重排前的诊断。

F02完成后，下一项为F03非阻断设计提示。停止流程、其余测试子场景、干净安装、性能实验、正式报告与最终冻结仍见acceptance-status.md。
