# Agent 内核上下文精简规划

确认与启动日期：2026-10-07，Asia/Shanghai。承接 [0.2.0 基线](BASELINE-0.2.0-2026-10-07.md)，归属 DEMO-05 / PLAN-11，不新增架构主线。本次用户明确授权勘察、落入规划及三个并发子代理连续实施，不需要逐片再次询问授权。

## 当前状态

三批已按本期范围完成：基础内核、普通图接纳、提示词生命周期和配置界面，集中测试、失败项修复复测及隔离浏览器验收已收口。最终证据和未覆盖边界见本页末尾。2026-10-07 用户授权统一提交，本次提交收录三批实现、测试和收口文档，承接 `18cc6a1`；未推送，常驻版本及固定基线不变。

## 开工依据

- 开工前工作树干净，`main`、`origin/main`、`v0.2.0^{}` 均为 `18cc6a1e037db7f299ea7f42f51a7ba04fb036d1`。
- 已用 `git ls-remote` 核实远端分支及标签，不只是读取本地 remote-tracking ref。
- 固定旧基线 SHA256 为 `0690253bec23a2e377245e6f570b2814c3320cea2c384a4de927534678c6fcc4`；0.2.0 基线文件 SHA256 为 `d73f206d7431eee7cb54ddd67dad8719f77956deacaab713e5859a42a80b2170`，均不改写。
- 当前源码版本与已安装常驻仍是 0.2.0。本次开发不创建新发布基线、不改版本号、不切换常驻、不触碰真实运行库。
- 本次不授权额外收费调用、自动收费重试或推送。历史真实验收不充当新精简能力的证据。

这次授权提前启动长上下文维护中的内核精简范围；DEMO-06 规模测量、其他 COND / LATER、活动内核跨进程恢复及暂停事项没有同时恢复。

## 目标与职责

内核自带上下文精简能力，外部策略节点连接 Agent 的配置端口。内核维护本次运行的工作视图；上下文更新节点验证并持久接纳会话的下一版本。

```text
上下文读取 -> 提示词装配 -> Agent 内核 -> 有序上下文更新 -> 会话对象
                              ^
                        精简策略节点
```

明确区分：

1. 原始执行事实与已接受消息只追加，不被摘要改写。
2. 模型可见工作视图允许按已接受精简记录替换。
3. 一次精简先替换既有范围，精简后的新增内容再追加；不能把原始完整增量重新追加而恢复旧原文。
4. 同一次执行允许表达 `append -> compact -> append -> compact -> append`，更新节点按原顺序复放，不通过正文 diff 猜变化。
5. 精简不创建新 session，不重新开始工作流，不虚构真人的新 user 回合。

## 已确认配置

| 配置 | 语义 |
| --- | --- |
| `enabled` | 是否启用精简；未接入或未启用策略不等于使用默认摘要 |
| `trigger_tokens` | 正整数表示提前触发阈值；`0` 表示只使用 90% 守卫 |
| `keep_depth` | 保留最近的完整历史对话回合；checkpoint 不计新真人回合 |
| `summary_prompt` | 自定义精简指令；空字符串或仅空白时使用固定版本的默认精简提示词，JSON `null` 不合法 |
| `target_tokens` | 本期必须为 `null`，仅占位，不参与调度或构成能力承诺 |

没有自定义提示词和没有启用精简是两个状态。默认提示词保留目标、约束、决策、精确标识与数值、未完成事项和不确定结果，不强制编码助手的专用章节或英文。

总上下文上限属于明确的模型容量描述，不作为供应商生成参数发送。没有可靠窗口元数据时不能猜测供应商上限；普通图接线批必须为新能力提供明确容量依据。没有新容量描述的既有 snapshot 不会被静默启用 90% 守卫，既有图的端口与行为保持基线。

`keep_depth=2` 表示最近两个完整历史回合保持原文，更早的可精简文本允许被替换。`keep_depth=0` 表示不因深度额外保留可精简历史文本，不是删除全部记录，也不取消当前任务与工具协议保护。它与提示词插入 `depth` 是不同参数。

## 容量与安全边界

- 每次模型请求前判断压力，核算消息、工具定义、输出预留和安全余量；不能只计可精简文本。
- 已启用策略时，配置的更早 token 阈值或 90% 总窗口守卫触发精简。
- 未启用策略达到 90% 时，当前已接受工具批次闭合后停止 Agent 并抛错；已闭合则立即停止，不派发新的模型请求或新的工具批次。
- 最终 `final_answer` 工具闭合也不能绕过 90% 守卫；仅因更低的提前阈值，不在成功终态额外发起摘要调用。
- 存在未闭合工具调用时不得精简。维护只能在闭合边界进行，不能丢掉调用或结果来凑配对。
- 首批使用明确标记的保守 token 估算，并允许测试 / 后续精确 tokenizer 注入；不把字符预算称为准确 token 计数，不把响应 `usage` 当成下一次请求的预检结果。
- 摘要请求本身也要受输入容量约束；完整前缀加维护指令及输出预留达到窗口时拒绝派发。固定材料或工具协议已占满保护预算时直接报错，不能扩大精简范围。
- 普通图使用明确的 `context_window_tokens / output_reserve_tokens / summary_max_tokens / max_cold_input_tokens`。维护请求即使预计命中缓存，也必须通过完整冷输入 tokens 上限；模型服务在派发前独立再检查，不依赖内核单方判断。
- 精简失败、输出为空、含工具调用 / 不支持内容或缩减后仍不安全时不提交替换，不无限反复调用。
- `MODEL_BINDING@2` 必须声明正整数业务 `max_tokens`，不得大于输出预留；维护调用使用冻结的独立 `summary_max_tokens`，不得大于预留。不修改业务调用参数，不采用供应商隐式默认输出上限。
- 新模型源默认窗口及冷输入上限为 `0`，只是未配置草稿，不能解析为可运行绑定；不猜测供应商窗口。冷输入 tokens 预算不是货币预算，也没有按价格自动拒付功能。

## 可精简内容

本期只处理持续进入上下文的文本：

- 已进入会话的 user / assistant 普通历史文本。
- 明确允许精简的一次性背景提示词。
- 已有 checkpoint 中仍有效的背景信息。

每次重新装配的固定提示词、系统规则、工具 schema、tool 消息及 assistant 的工具调用协议默认不参与。一次进入不自动等于可精简。

checkpoint 在内部是独立来源类型，携带精简 ID、覆盖范围和维护调用凭据。最终 wire 投影为一条 `user` 背景消息，不记录为真实用户的新输入。注入框架固定并版本化，不因自定义摘要提示词变化；说明其为较早对话的派生背景，不覆盖系统规则和后续明确修正。

## 缓存设计

- 维护请求复用精简前实际请求的完整消息前缀与工具定义，只在尾部添加摘要指令及明确范围。
- 不将选区另行序列化成摘要专用 JSON 并放在新 system 提示词后。
- 历史调用 ID、文本、参数原串及顺序须稳定；现有跨轮投影 ID 问题在普通图接线批处理，不能只验证摘要正文。
- 记录实际输入 / 输出及 hit / miss usage。相同前缀仅提供复用条件，不承诺缓存必定存在或命中。
- 替换后的新短视图需要建立相应缓存；不能把摘要生成请求的命中与下一次业务请求的命中混为一谈。
- DSH 仅作为用户提供的机制参考，不核对其源码，不复制其 surface 存储、事件日志或专用编码模板。

## 有序上下文更新

内核输出原始消息增量，同时独立输出工作视图和操作序列。原始增量用于事实证明，不由更新节点重新追加一次。

更新包至少明确基准视图 / 对象版本、归属、更新身份、有序操作、最终候选与执行凭据。每项精简记录明确精简前视图、覆盖消息、checkpoint 和精简后视图；其后 append 只携带之后真正新增的消息。

上下文更新节点：

1. 验证实际 Agent 来源与对象 / 会话归属。
2. 从原始基准按序复放已证明的 append / compact，验证最终候选。
3. 保留全部已接纳增量身份和一次提示词的消费状态，不因可见原文被替换而恢复或重新注入。
4. 核实对象版本并使用现有 CAS、幂等和事务接纳流程写回。
5. 接纳失败只复用已接受产物重试，不能重新调用 Agent 或摘要模型。
6. 基准冲突拒绝覆盖，不自动合并两份摘要。

首版成功完成后才交外部更新节点持久提交；运行中精简仅变更内核工作视图并记录维护事实。暂停现场包含工作视图和操作序列，同进程继续不重复生成摘要。精简结果即使仍高于提前阈值，恢复同一已维护工作视图也先继续业务；真正新增消息后重新判断，90% 守卫始终不跳过。失败后独立采用工作视图、手动维护命令、活动内核跨进程恢复不在首批承诺。

维护调用独立记录 `context_compaction_started / finished / applied` 及供应商原始 usage，不增加原有业务 `model_requests / attempts` 计数。展示和费用汇总必须同时读取两类事实，不能只看业务计数。

## 代码勘察结论

| 现有边界 | 必要改动 |
| --- | --- |
| `runtime.py` 请求固定为初始 `s0` 加全部接受消息 | 独立工作视图，闭合边界维护，有序操作及暂停现场 |
| `execution_facts.py` 强制请求等于完整原始历史 | 按初始视图及已接受操作证明请求，原始事实仍不改写 |
| `KernelResult` / checkpoint 只有原始消息进度 | 工作视图与精简进度纳入结果及现场完整性检查 |
| 当前 Agent 输出只有整轮 Unit / receipts | 新增有序更新业务契约，不能精简后又合并完整原文 |
| `CONTEXT_VIEW@3` 只有整轮或外部摘要 | 新有效视图版本与来源证明，支持文本片段及 checkpoint |
| prompt 材料 / 资源无一次注入生命周期 | 后批新增显式生命周期及消费账本，不借 metadata 隐式激活 |
| 通用记录与会话对象保存版本化 JSON | 复用存储 / 接纳，不预设新增 SQL 表或改通用调度 |

现有显式摘要节点保留独立用途，不伪装为内核自动维护。既有 0.2.0 图不会因修改内部 helper 被静默切换语义。

## 合并实施批次

### 第一批：基础契约与原生内核

状态：已完成 Native 基础范围，三个并发组交付并经根代理合并验证；随本次三批收口统一提交。

- 策略契约、默认提示词及无状态节点注册 helper。
- 有序 append / compact 的严格复放、候选验证和纯接纳准备。
- 原生内核容量守卫、维护调用、工作视图、事实证明和同进程暂停。
- 专项离线测试覆盖先精简后新增、两次精简、工具闭合、无策略守卫、失败不替换、前缀复制、重试不重复调用。

这一批不等于普通图 / HTTP / 前端已支持新 Agent 接线；未接线能力不能作为工作台使用完成。

首批落盘代码：`context_compaction_policy.py`、`context_compaction.py`、`context_update.py`，以及 `runtime.py` / `execution_facts.py` / `contracts_v2.py` 的原生扩展。第一批单独完成时尚无普通图接线；后两批已补上下面列出的明确版本，不将第一批的历史范围改写为当时已完成。`agents.execute@1/2/3` 仍保持既有端口，不能连接新策略端口。

### 第二批：普通图与上下文接纳

状态：已完成，普通图和持久接纳已接通；离线整图、集中定向验证及隔离浏览器多轮 / 分叉验收完成。

- 新 Agent / PROMPT / 视图版本及策略输入端口。
- 明确窗口元数据、稳定 transport 投影和维护调用参数边界。
- 独立维护 usage、供应商原始 hit / miss 字段透传与完整冷输入 tokens 保护；未声明输出上限时拒绝新绑定。真实缓存命中与货币费用核对没有收费验收授权，不能冒称完成。
- 更新节点的实际 artifact / facts 证明、对象 CAS、事务 / 接纳重试。
- 独立 A / B、多轮、完成分叉及重开验证；旧图版本的明确处置，不自动重写冻结历史。

### 第三批：提示词生命周期与交互收口

状态：已完成，前端配置及维护展示已接入；类型 / 构建、生命周期交互与真实信息信封展示补验完成。

- `per_request` 与 `context_once`，以及独立的 `never` / `allowed` 精简许可。
- 消费账本覆盖去重前所有来源身份；仅接纳后消费，摘要、复制 / 分叉不重新注入。
- 原提示词编辑不自动重新注入；显式重注入的范围另行明确。
- 策略配置界面、容量 / 维护状态与错误展示。
- 集中一次直接受影响的前端类型 / 构建及离线联合交互验收。

## 普通图接线

新增的是版本化业务契约及节点，不修改通用工作流 SQL、调度器、对象 CAS 或 manifest 结构，不自动升级已保存的旧图。

| 接线 | 明确版本与职责 |
| --- | --- |
| 模型源 | `models.source@2 -> MODEL_BINDING@2`，配置资源、生成参数及明确容量 |
| 提示词材料 | `prompts.item/group/source/summary/global-reference/global-resolve/tool/tool-summary@2`，输出 `PROMPT_MATERIALS@1` 或精确资源引用 |
| 长期视图读取 | `context.output@4 -> CONTEXT_VIEW@4`，绑定当前会话对象与 Agent |
| 请求装配 | `context.assembly@4`，读取 view4、TEXT2 当前输入和新材料，输出 `PROMPT@6` |
| 原生 Agent | `agents.execute@4`，输入 PROMPT6、MODEL_BINDING2 和可选 `compaction_policy` |
| 更新接纳 | `context.merge@4`，输入原 view4 和 `AGENT_CONTEXT_UPDATE@1`，输出下一 view4 与实际 `CONTEXT_COMMIT@2` |
| 会话对象 | `workflow.effective-context@4`，保存已接纳视图指针和增量身份 |

`AGENT_CONTEXT_UPDATE@1` 同时声明 `basis_view_ref`、`compaction_policy_ref`、原始增量、操作序列、下一候选与 receipts。更新节点从完整 PROMPT6 工作视图复放操作，随后显式投影持久历史：丢弃每次装配材料、将本次用户输入晋升为普通历史、从实际已接受 final facts 派生 assistant 正文。它不将整轮原始增量再次追加到已精简视图。

写回同时核对官方 Agent 的实际 prompt、model、policy 输入 artifact、冻结 snapshot、已接受 executor facts 和更新候选；静态对象证明再次核对官方产物链与精简许可。引用替换、合法形状但不同内容的策略、重复或过期候选不能绕过证明。显式采用入口支持 `context.merge@4`，仅采用本会话已接受、同一 merge 节点的产物闭包；相同候选幂等，不覆盖后续版本。

## 提示词生命周期

新增全局资源为 `workflow.prompt-resource@2`。schema1 资源及原 producer@1 不静默转为新语义，仍按精确版本选择及编辑。

- `per_request` 每次装配，必须为 `compaction=never`，不进入持久历史。
- `context_once` 在成功接纳时消费稳定来源 ID；是否可精简另外设置为 `never / allowed`。
- system 角色只允许 `never`；一次进入不意味着可以精简系统规则。
- 去重保留全部 `origin_item_ids`，消费账本不是按正文字符串判重。
- 精简、资源原条目编辑、分叉与重开都保留消费状态，不重新回填已精简正文。显式重新注入需建立新的条目身份，本期没有单独的“重置消费”命令。
- `middle depth` 仍要求有效历史锚点；精简后轮数不足会报 `context_history_anchor_missing`，不会自动减小深度。

工作台另提供 20 节点的上下文精简串行示例，A / B 分别有对象与策略，A 的本轮分析通过 `prompts.source@2` 以 user、per_request/never 传给 B。原 18 节点示例保持不变。新示例仍需显式配置供应商、模型、窗口及冷输入预算，不自动调用模型。

## 展示与限制

策略界面包括 token 阈值、保留深度、自定义摘要提示词和只读 `target_tokens=null`。模型源展示四项容量字段。运行历史按真实 executor 信息信封解包，区别开始、摘要响应已收到、工作视图已替换，显示前后容量与估算依据，并保留原始 usage 和事实证据。

- 当前 UTF-8 字节计数是保守估算，不是精确供应商 tokenizer。供应商缓存 hit / miss 仅按其实际返回字段记录；离线模拟 usage 不充当真实命中证据。
- 保护的工具协议会持续占用窗口，当前输入和固定材料也可能无法精简；此时按安全守卫停止，不承诺所有长会话都可无限继续。
- Agent 完成产物或 context 对象接纳失败时，可复用既有产物重试，不重跑业务或摘要。辅助模型响应成功但 outcome fact 写入失败时，停止、不自动重发、不采用未确认摘要；本轮不扩展宿主中途维护恢复协议。
- 同进程暂停继续已覆盖；活动内核跨进程恢复仍后置。完成后的持久历史冷重开是另一项能力，不能混为一谈。
- 没有新增手动精简命令、自动 overflow 重试、其他文本精简节点、货币费用门禁或 `target_tokens` 调度。
- DEMO-06 性能/正文/数据库增长测量、其他 COND / LATER 和暂停项仍未恢复。

## 第一批历史证据

按整批统一直接受影响验证，失败仅重测失败项及相关范围，不因 helper / 文档小改反复全量。

第一批集中验证为 **23 文件、562 项：506 通过 / 56 失败**，耗时 247.15 秒。不是后端全量，也不能称全部通过。新增三个专项文件的 **106 项全部通过**，已包含在 506 中，不与子代理的分片 / 追加测试重复累加：

| 新专项 | 集中验证结果 |
| --- | --- |
| `test_context_compaction_policy.py` | 45 通过 |
| `test_context_update.py` | 22 通过 |
| `test_runtime_compaction.py` | 39 通过 |

Agent 包与既有串行 Demo、原生运行 / 暂停 / 失败继续、公开事实与当前事实存储、宿主、既有上下文接纳等直接受影响范围也纳入同一合并验证。测试集合与复现入口如下，工作目录为 `backend`：

```powershell
& '..\.venv\Scripts\python.exe' -c "import sys, pytest; sys.path.insert(0, 'src'); raise SystemExit(pytest.main(sys.argv[1:]))" `
  tests/test_context_compaction_policy.py tests/test_context_update.py tests/test_runtime_compaction.py `
  tests/test_agent_package.py tests/test_agent_integration.py tests/test_serial_agent_demo_integration.py `
  tests/test_capability_packages.py tests/test_contracts_v2.py tests/test_execution_facts.py `
  tests/test_runtime.py tests/test_runtime_pause.py tests/test_runtime_recovery.py `
  tests/test_runtime_execution_facts.py tests/test_runtime_fact_store.py tests/test_runtime_content_boundaries.py `
  tests/test_runtime_tool_observation.py tests/test_runtime_unlimited.py tests/test_public_runtime_review.py `
  tests/test_runtime_hosting.py tests/test_runtime_hosting_integration.py tests/test_context_integration.py `
  tests/test_context_summary_integration.py tests/test_bound_context_integration.py `
  -q --basetemp=../.local/pytest-compaction-batch1-consolidated-20261007 `
  --junitxml=../.local/junit-compaction-batch1-consolidated-20261007.xml
```

56 项失败均位于未修改的两个旧测试文件：

- `test_execution_facts.py`：43 项夹具仍调用已移除的 `SqliteStore.save_bundle`，5 项调用已移除的 `get_record`。
- `test_runtime_content_boundaries.py`：8 项仍断言 `node_run` 为 schema 4，当前实现已使用 schema 5。

用 `git archive 18cc6a1 backend` 导出固定基线到忽略的 `.local/compaction-batch1-baseline-18cc6a1/`，独立进程明确从该副本的 `src` 导入，分别抽样复现三类失败：

```text
test_execution_facts.py::test_fact_history_durable_complete_detached_and_separate_from_history
test_execution_facts.py::test_explicit_v5_migration_preserves_old_records_without_inventing_facts[0]
test_runtime_content_boundaries.py::test_successful_reference_only_records_cannot_lose_or_forge_binding_evidence[empty-inputs]
```

抽样 3 项同样失败，1.89 秒，原始证据为 `.local/junit-compaction-baseline-failure-proof-20261007.xml`；未重复运行基线的整个 562 项范围。相关测试、`storage.py` 和 `graph_records.py` 本次无改动。残留用例登记为 BACKLOG 的 TEST-01，不恢复旧接口、不降低当前 schema，也不静默删除失败用例来制造绿色结果。

截至第一批完成时，`git diff --check` 与修改文档的本地链接检查通过；两个固定基线文件哈希保持开工值。原始测试 XML、临时库与基线副本仅留本地忽略目录。当时没有新收费、常驻切换、前端构建、浏览器或普通图新精简能力验收；后两批补齐情况如下。没有真实缓存命中证据；第一批结束时尚未提交或推送，本次统一提交不改写这段历史验证结果。

## 三批最终收口证据

完成日期：2026-10-07，Asia/Shanghai。沿用整批集中验证、失败仅重测直接范围的节奏，没有把每个 helper / 文档修改扩大为全量回归。

### 后端

最终集中范围为 **32 文件、620 项：619 通过 / 1 失败**，JUnit 记录耗时 388.422 秒。唯一失败为 `test_model_package.py::test_capability_adapter_returns_public_tool_response_and_keeps_failed_request_key`：新维护异常转换影响既有 binding1。已修正版本边界，binding1 仍抛 `ContractValidationError`，binding2 保持结构化 `ModelRequestError`；该失败项及维护 outcome 写入失败、冷输入预算两项一起复测 **3 通过**。

随后浏览器冷重开发现新容量 schema 从集合生成 `required`，不同进程的声明字段序可能不同。已改为稳定 tuple 生成声明、独立 frozenset 校验，未绕过或改写数据库契约证据。新增 `test_native_type_contracts_reopen_in_independent_processes_with_different_hash_seeds`，两个真实独立进程用不同 `PYTHONHASHSEED` 同库注册 / 冷重开，比较完整类型、节点、服务、信息源和包锁，最终 **1 通过**；与严格容量配置的先前 **2 通过** 重叠，不累加。另行交叉核对 42 个类型、65 个节点、4 个服务、4 个信息源、9 项包锁声明一致。

因此最终 620 个集中目标经失败范围修复复验已核对，另新增 1 个跨进程目标通过；这不是一次“621 项全绿”的运行，也不是后端全量通过。第一批的 56 项 TEST-01 残留仍未清理。

集中范围：

```text
test_context_compaction_policy.py  test_context_update.py  test_runtime_compaction.py
test_agent_package.py  test_agent_integration.py  test_serial_agent_demo_integration.py
test_capability_packages.py  test_contracts_v2.py  test_runtime.py  test_runtime_pause.py
test_runtime_recovery.py  test_runtime_execution_facts.py  test_runtime_fact_store.py
test_runtime_tool_observation.py  test_runtime_unlimited.py  test_public_runtime_review.py
test_runtime_hosting.py  test_runtime_hosting_integration.py  test_context_integration.py
test_context_summary_integration.py  test_bound_context_integration.py
test_context_native_integration.py  test_agent_native_execution.py  test_context_package.py
test_model_package.py  test_models_service_integration.py  test_prompt_package.py
test_prompt_current_resources.py  test_prompts_integration.py  test_prompt_lifecycle.py
test_compacting_serial_integration.py  test_agent_information_reader.py
```

原始证据：`.local/junit-compaction-final-20261007.xml`。新增案例覆盖两次精简后追加、真实策略 artifact 证明、一次消费账本、接纳重试不重复调用、稳定工具 ID、A/B 独立历史、摘要无效不写回和完成历史冷重开。

### 前端

首轮受影响 **8 文件、105 项：104 通过 / 1 失败**，失败是新增 SSR 标记断言；修正后复测该范围通过。后续生命周期交互、未配置容量草稿和信息信封展示新增用例分别通过，范围重叠不累加。最后浏览器发现 public fact 的正文不带独立 `schema_version`，已按 versioned executor 信息信封识别，保留未知 / 非法形状的原始展示；最终直接相关 **2 文件、6 项通过**。

最终 `vue-tsc --noEmit` 和 Vite 构建通过。隔离产物为 `.local/compaction-workbench-dist/`，JS `index-XnTawCF4.js` 442.95 kB / gzip 140.73 kB，CSS 38.99 kB / gzip 7.40 kB。没有全量前端绿色承诺，也没有重新安装依赖或切换产品部署。

### 隔离浏览器

实际使用本地 Python HTTP 服务、临时 SQLite、当前构建和纯离线 transport，端口为 `54553`；不是供应商网络调用。已完成：

| 场景 | 实际结果 |
| --- | --- |
| 工作台创建新 20 节点示例、配置窗口 / 冷输入预算和策略 | 保存并运行成功；一次性可精简背景实际替换为 checkpoint，正文没有被全量增量恢复 |
| 维护信息 | 开始、响应已收到、工作视图已精简分别展示；容量为 UTF-8 字节估算，未提供 usage 不冒称缓存命中；原始证据可展开 |
| 双 Agent 首轮与第二轮 | A / B 首轮各精简两次；第二轮正常续聊，已消费的一次背景不重注入 |
| 完成候选分叉后续聊 | 分叉由工作台创建，聊天前端完成后续轮；父会话仍为原来的 2 条展示引用，分叉为 4 条 |
| 暂停 / 继续 | 图调度闭合边界实际暂停，核实原请求回执后继续成功；不充当模型 HTTP 中途取消或活动内核跨进程恢复证据 |
| 受控传输异常 | 显示 `model_dispatch_unknown`，不自动重试；A / B 上下文与 frontend 对象都未推进，已完成的 3 轮历史保留 |
| 独立进程冷重开 | 最新声明下临时库重开，完成图、已精简历史和一次消费状态仍可读取 |

最终只读证据核对 **30/30**：无活动运行、双 Agent 两次有序精简、父 / 分叉历史隔离、一次消费状态、持久 assistant 正文及故障前后对象 revision 均核对。它是浏览器产物核对，不与单元测试相加。最后 harness 进程观察 20 次模拟调用，其中 4 次摘要；此前 UI 首轮在另一进程完成，计数不外推为整个验收总调用数。

本地证据为 `.local/compaction-browser-report-20261007.json`、`.local/compaction-browser-verification-20261007.json`、`.local/compaction-maintenance-20261007.jpg`、`.local/compaction-paused-20261007.jpg` 和 `.local/compaction-fault-20261007.jpg`。临时服务已关闭，测试库及截图只留本地忽略目录；常驻 `8765` 未触碰。

### 未覆盖与交付状态

- 真实供应商缓存命中 / miss、准确费用、精确 tokenizer 和长期规模 / 存储测量仍未验收。
- 手动精简、`target_tokens` 调度、自动 overflow 重试、其他文本精简节点、活动内核跨进程恢复及其他 COND / LATER 不在本期完成范围。
- 第一批已证明的 TEST-01 旧测试残留仍待另批处理；不恢复已删除旧接口来换取通过。
- `git diff --check`、修改文档本地链接 / 锚点检查通过，两个固定基线哈希保持开工值。本次提交统一收录三批实现、测试和收口文档；没有推送、版本变更、发布标签或常驻切换，仍承接 0.2.0 固定基线。
