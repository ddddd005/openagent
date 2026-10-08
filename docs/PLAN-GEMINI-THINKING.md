# Gemini 工具与思考接入准备

日期：2026-10-09。状态：G1-G4 已实现，G5 本期离线联合验收完成；收费在线验收未执行。
开发基线：`fc2253902e7aa671472ccb03e8f282794fc36906`。

## 开发与稳定入口

- 产品修改只在 `develop` 推进，不另建功能分支。用户明确授权三并发：契约与历史、原生传输、前端与联合验收；统一在当前工作区汇合。本轮不推进 `main`、不推送、不改版本和标签，修改尚未提交。
- 根目录 `start.bat` 固定解析本仓唯一的 `main` 工作区，核对分支、提交和干净状态后委托其已有启动脚本；不存在或不干净时拒绝，绝不切换当前开发目录或回退 `develop`。
- 本机准备的稳定检出为 `.local/stable-main`，绑定现有 `main`，独立安装 Python / Node 依赖。默认稳定端口仍为 `8765` / `5178`，数据库保留根工作区原 `.local/dev/workflow.sqlite`，不复制或迁移原记录。
- `start-dev.bat` 只启动当前 `develop`；默认端口 `8766` / `5179`、数据库 `.local/develop/workflow.sqlite`。不同端口隔离浏览器 origin / localStorage；拒绝稳定默认端口及稳定默认数据库。显式自选其他数据库时，由操作者保证不共享正在使用的库。
- `-ResolveOnly` 只核对分支和目标；`-CheckOnly` 进一步检查目标依赖与源码，不启动服务、不打开数据库。仍使用可见普通控制台，不做隐藏启动、后台注册或自启动。
- 稳定入口从根工作区运行。直接执行 `.local/stable-main/start.bat` 是基线的旧入口，不作为推荐入口。
- 验收通过后另行决定把明确提交纳入 `main`；运行进程不会因合并自动换版，需在确认无活动或待核实业务后显式重新启动。

## 已核对的接线点

| 层 | 现有入口 | 本期准备结论 |
| --- | --- | --- |
| 供应商与配置 | `model_configuration.py`、`model_contract.py`、`frozen_model.py` | Gemini provider@2 与受控凭据引用已接入；旧 Chat provider@1 保持原声明 |
| 受控派发 | `model_service.py`、`model_host_service.py` | 事实、请求 digest、冷输入估算与实际 transport 共用 `prepare_request` |
| 模型边界 | `contracts.py`、`adapter.py`、`gemini_adapter.py` | `ModelResponse` 新增独立摘要和协议元数据；DeepSeek 显式拒绝不属于自己的协议材料 |
| Agent 内核 | `runtime.py`、`contracts_v2.py`、`contract_graph.py` | 原始 parts 经 AgentMessage@5 保存，内核 ID 只改变显式绑定，不改变签名内容 |
| 历史与上下文 | `agent_executor.py`、`context_contract.py`、`context_prompt.py`、`context_prompt_v6.py`、`context_v4.py` | CONTEXT_UNIT@2、四条 Gemini Agent 路线及 SQLite 冷重读闭环已实现 |
| 接纳与落盘 | `execution_facts.py`、`runtime_fact_store.py`、`type_contract_store.py`、`graph_store.py` | JSON 可存字段不等于端到端已保留；核对类型目录、摘要、引用及历史纯读取 |
| 用户界面 | `workflowModelResources.ts`、`ModelSourceFields.vue`、供应商编辑器、历史 / GraphChat / 酒馆展示 | 配置扩展和独立摘要展示，不向正文或常规日志输出签名 |

Python 表中路径继承 `backend/src/phase1_agent/`；Vue / TS 路径以开发说明的前端入口为准。`vendor/smolagents` 当前不是执行依赖，不在其中另造一条接入路线。

## ST 参考边界

已查看本地 `step1/SillyTavern`：

- `src/endpoints/backends/chat-completions.js`：Gemini 请求、思考参数、工具定义和完整 `responseContent`。
- `src/prompt-converters.js`：`functionCall` / `functionResponse` 与签名回投。
- `public/scripts/tool-calling.js`：调用关联和工具签名保存。
- `public/scripts/reasoning.js`：显示摘要与签名分离。

只参考协议转换，不照搬其缺签名时的 validator 绕过、文本签名广播或同角色文本合并。我们的原始 part 顺序和签名绑定必须可验证，不能靠工具名称或合并后的正文猜配对。

## 数据契约裁决

三个通道分别持有不同权威：

| 通道 | 内容 | 限制 |
| --- | --- | --- |
| `content` | 面向用户的回答正文 | 不混入摘要、签名或工具结果 |
| `thinking_summary` | 供应商实际提供的可见思考摘要 | 允许缺失；不是完整内部思维链，不从签名解码或从正文推断 |
| `provider_metadata` | 带版本的 Gemini Content / parts 协议封套 | 后端协议权威，保留原顺序、原 part 字段、原签名字符串；不进入普通用户正文 |

字段名称已固定。封套为 schema 1，保存 `provider`、`protocol`、冻结请求 `model`、可选实际 `response_model`、原 `content` 和有序 `tool_call_bindings`；后者保存 `part_index -> tool_call_id`。保留供应商原生 ID（有则原样回传）与内核执行 ID 的区别。内部无供应商 ID 时分配本地关联 ID，只用于接纳及持久化，不伪称供应商返回。元数据上限 2,000,000 bytes，摘要上限 131,072 字符，原 parts 最多 256 个。

- 归一化调用必须与原 part 的工具名称、参数和位置一致；签名不提升成单一 message / tool 属性后丢弃原 parts。
- 所有投影显式保留封套，回投使用原协议 parts；不能把呈现层摘要拼成请求历史，也不能跨模型、供应商或会话移植签名。
- 版本化扩展模型结果、Agent 消息及上下文，保留旧 DeepSeek 记录的原有读写语义；缺字段不自动授予 Gemini 可续用资格。
- 旧记录没有协议封套时仍可阅读。进入需要签名的工具轮次应给出缺失位置及来源诊断，不伪造、不填绕过值、不改原始记录。
- 对封套做 JSON 类型、长度、调用绑定和一致性检查；这些本地检查不能证明签名密码学有效，最终供应商校验不可绕过。
- 导出 / 常规诊断 / UI 展示与后端协议持久化是不同投影。脱敏掉协议封套的导出只能展示，不能宣称可直接恢复签名闭环。

## 五个阶段

| 阶段 | 修改内容 | 进入下一阶段的门槛 |
| --- | --- | --- |
| G1 契约 | 三通道、版本注册、part / 调用关联、兼容边界 | 校验、复制、序列化往返、旧格式读取及错配拒绝通过 |
| G2 闭环 | 模型结果、内核接受消息、运行事实、上下文写回、SQLite 重读、下一次投影 | 从真实形状协议 fixture 落盘重读后，原签名和 parts 逐项一致；完成回合追加及同进程暂停均不丢信息 |
| G3 原生 Gemini | 独立 `generateContent` adapter、受控 `env:GEMINI_API_KEY`、工具转换及 HTTP 错误分类 | 连续 / 多调用轮次、调用 ID 关联、注册工具 schema、既有 `final_answer`、零自动重试通过 |
| G4 思考配置与展示 | 模型能力表、预算或强度映射、冻结配置、摘要独立折叠展示 | 不支持配置在派发前拒绝；正文和签名分离，历史重开及窄屏展示通过 |
| G5 联合验收 | 普通串行图 / 原生上下文 / 酒馆消费者联合回归 | 下方离线验收矩阵通过并记录明确变更和证据；是否提交及稳定晋升另行决定 |

G2/G3 使用真实形状协议和 mock HTTP transport 验证闭环，未向供应商发出请求。没有为酒馆另建“无工具”执行器；继续由现有 Agent 注册 / 校验 / 执行工具并结算 `final_answer`。

初期沿用非流式 text + tools 范围，不增加多模态、供应商服务器会话或自动工具执行 SDK。保留现有 httpx 边界，避免 SDK 隐式执行工具或重试。工具 schema 转换不能静默删掉约束；不支持的字段派发前明确诊断，内核仍按原 schema 校验。

## 请求与安全边界

- `prepare_request` 形成唯一的实际 wire、协议版本和冻结参数依据；模型服务事实、digest、冷输入估算与 transport 使用同一投影。不继续用 DeepSeek 请求伪装 Gemini 审计事实。
- 凭据仅在受控 broker 解析，允许 `env:GEMINI_API_KEY` 与既有 DeepSeek 引用，不允许任意环境变量或明文 key 进入节点配置。
- 供应商 HTTP 错误、未派发、结果无效和派发结果未知分别保留现有失败语义。400 签名错误不是安全重试许可；timeout 不自动再次调用模型或执行工具。
- 一个回复可以包含多个函数调用；按原顺序接纳并匹配结果。多调用协议支持不意味着增加内核并发执行能力；同名多调用仍按 ID / part 对应，不能按名称覆盖。
- 暂停恢复依赖现有同进程现场。持久化重读用于历史展示、完成回合后续新请求和纯协议回投测试，不宣称已有活动执行可跨进程恢复；不得凭重读自动重放工具。
- 模型服务结果及终态产物的接纳重试只重新提交保留结果；不重复派发。任意中途 `message_accepted` 事实存储失败仍按既有规则终止，没有新增通用中途事实重试机制。
- 当前工具批次保留完整协议，精简不能截断 call / response 配对、合并签名 part 或把不透明元数据当作可总结正文。补测历史精简和提示词处理后的回投；无法证明有效时拒绝继续该签名轮次。
- 已实现的保守策略保护 Gemini 封套，正则不重写它；精简 / 历史摘要若会覆盖签名记录则明确拒绝。不生成替代签名，也不宣称支持签名历史的任意自动摘要。

## 模型能力核对

按明确模型 ID / 协议版本登记思考预算或等级、是否可关闭、摘要支持、签名要求及工具参数范围；未知模型不凭名称猜能力。不同时发送互斥的预算和等级，不把用户选择的 unsupported / disabled 静默换成供应商默认。

思考 token 用量和可见正文长度分开；输出预留和精简摘要上限需要核对思考占用。摘要请求可启用但不能保证供应商每轮都会提供摘要。生产签名只有响应原样返回可用，测试 marker 仅作传输夹具且绝不用于真实请求或冒充有效签名。

## 验收矩阵

| 场景 | 必查断言 |
| --- | --- |
| 正文 + 摘要 + 元数据 | 三通道互不污染，签名不展示，空摘要不失败 |
| 连续两轮工具后最终答案 | 每轮完整 parts 和原签名回传，`final_answer` 仍是既有受控工具 |
| 多调用 / 同名不同参数 | 接纳顺序、ID / part 绑定和 function responses 精确一致；不要求每个 part 都有签名 |
| 文本与工具交错 / 多签名 part | 不重排、不合并、不广播签名，完整 Content 可回投 |
| 同进程暂停恢复 | 在模型响应保留、消息接纳前后、工具结算边界暂停，不重复派发已执行外部效果 |
| SQLite 重读 / 下一回合 | 关闭存储后新读实例读取，摘要、封套和配对一致；新回合请求使用原持久化材料 |
| 缺签名 / 错配 / 非法 part | 必需场景在工具执行前诊断；供应商错误不触发自动重试，不修改历史 |
| 混用模型 / 供应商 / 老历史 | 可读与可续用区分，不自动借用另一个模型的签名 |
| 精简 / 提示词处理 | 未完成工具批次不可被精简或重写；完成历史处理遵守模型的协议规则 |
| 错误及接纳失败 | 请求事实匹配真正 wire；保留结果后仅重试接纳，不重新调用模型或工具 |
| 旧路径回归 | DeepSeek、原模型配置 / 历史、串行 A/B、原生上下文、酒馆共用 Agent 路径不回归 |
| 启动与界面 | 根 BAT 为 main；开发端口 / 库隔离；摘要显示和历史重开不泄漏签名 |

离线测试使用合成协议 fixture，不伪造真实有效签名。收费在线验收另行确认账号、明确模型 ID、费用上限和请求次数，使用独立数据库及用户明确允许的无副作用工具；无凭据时不得把离线通过写成真实 Gemini 通过。

## 官方核对来源

2026-10-09 准备时核对 Google 官方的 Thought Signatures、Thinking 与 GenerateContent 文档：签名是不透明协议状态，显示摘要不替代签名；签名与 part 位置不能随意合并；工具回复需正确配对。Google 当前还提供 Interactions 文档，本期范围仍固定用户先前选定的 `generateContent`，不静默切换 API 路线。

官方来源供实施时复核：

- Thought Signatures：`https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures`
- Thinking：`https://ai.google.dev/gemini-api/docs/generate-content/thinking`
- GenerateContent：`https://ai.google.dev/api/generate-content`
- Function Calling：`https://ai.google.dev/gemini-api/docs/function-calling`
- Deprecations：`https://ai.google.dev/gemini-api/docs/deprecations`

代码使用明确能力白名单，不声称全部 Gemini 型号具有相同签名要求，或白名单型号在所有账号仍可调用。真实在线验收前仍需按选定模型、账号和弃用状态重新核对。

## 准备阶段验证

- `main` 与 `develop` 引用仍为上述基线；稳定工作区干净。本轮准备文件留在 `develop` 工作区，尚未提交，未晋升或推送。
- 分支隔离 15 项与启动预检 / BAT 转发 9 项联合运行：24 通过、8 个实际启动 / 浏览器场景明确未运行，耗时 33.75 秒。JUnit 在 `.local/gemini-prep-tests/launcher-preparation.xml`；首次测试缺临时目录父级的夹具错误不计入通过数。
- 根稳定 BAT、开发 BAT 的真实 `-CheckOnly` 均通过，源码、节点注册、代理配置目标和解释器路径一致；PowerShell 语法及 `git diff --check` 通过。
- 稳定依赖独立安装，23 个非 editable Python 包版本与原开发环境对应包一致，`pip check` 通过；Node 依赖按稳定锁文件 `npm ci` 安装。
- 未启动、停止或重启服务，未打开用户数据库，未执行工具或调用收费模型。不把预检写成完整用户启动验收，更不把本轮结果写成 Gemini 已接入。

以上是实施前准备阶段记录。后续实现验收另行记录，不能用这些预检数字替代 Gemini 闭环和浏览器验收。

## 实施结果

三并发实现已汇合。四条 Agent/Graph/SQLite 路线、连续及同名多调用、冷重读后下一回合、暂停恢复、模型失败及接纳边界、摘要展示和实际开发 BAT 页面验收均形成离线证据。根组合回归首轮发现的旧暂停断言已按新的“响应保留但未接纳”语义修正，82 项相关补测通过。

版本配对、配置示例、分组结果、真实页面证据以及历史测试债务见 [Gemini 实现与验收记录](GEMINI-ACCEPTANCE-2026-10-09.md)。本期完成不等于后端全量绿色、真实 Gemini 供应商通过、活动执行跨进程恢复或已晋升稳定版。
