# 工作流接入 Agent：第一批操作、第二批操作与信息需求

2026-11-04 · 只读梳理（未改代码、未写数据库、未启停服务、未发起真实模型调用）
工作树：`develop`，HEAD `8bd2717`；本文全部结论来自当前工作树源码阅读，标注「推断」的条目为代码推断而非实测。
配套文档：[附录-操作与信息证据表.md](附录-操作与信息证据表.md)（逐条 operation / query / 字段 / 错误码证据）。

---

## 0. 一页结论

**判断顺序（为什么这样排）**：写依赖基线（CAS）→ 必须先读；保存不编译 → 必须先试算；运行依赖已保存定义和会话 → 必须先写后建会话；运行失败会占住"未完成运行"锁 → 必须先观测/关闭才能再改；有有效会话时改 fixed 配置等于派生新工作流 → 编辑与实验必须有边界。

**第一优先（P0，缺一项 agent 就无法完成一次"看懂→改→跑→看结果"闭环）**

| # | 操作 | 干什么 | 现状 |
|---|---|---|---|
| P0-1 | `catalog.node-types`（`protocol_version=2`）+ `platform` | 拿到全部节点契约：端口、类型、配置 schema、可调字段、包锁、数据类型 | 完整可用；缺"字段被前端扩展接管"清单（仅 UI 相关） |
| P0-2 | `definition.summaries` + `definition.read` | 找到目标工作流，拿到当前文档 + `revision` + `edit_sequence`（写入基线） | 可用 |
| P0-3 | `definition.inputs` | 保存前把整份文档抛给后端试编译，拿 `diagnostics` 与 `required_for_run` | **唯一** pre-save 编译入口，但不是正式 validate：无字段路径、无分类/派生预览 |
| P0-4 | `definition.update` | 唯一的结构写：整份文档 + `expected_edit_sequence` + 幂等键；也用于新建工作流 | 可用但语义复杂：G21 锁、派生/确认、display-only 不占修订 |
| P0-5 | `session.create` | 把会话钉在某一个 fixed `definition_revision` 上 | 可用 |
| P0-6 | `run.start` | 异步启动运行（202 立即返回 session 视图，后台线程执行） | 可用；准备期编译失败不留 run 记录 |
| P0-7 | `session.read` + `run.read` + `output.list/read` | 轮询状态、定位失败节点、取结果 | 数据齐全但缺时间戳/阶段/期望-实际类型 |
| P0-8 | `run.control`（至少 `close`） | 失败/暂停的链仍占 `active_chain_run_id`，不关就不能改图（G21） | 可用；这是 P0 的"解除阻塞"项，容易漏 |

**第二优先（P1，从"能用"到"好用/覆盖更多形态"）**：`definition.copy`（安全试验与派生基线）、`definition.sessions`/`session.summaries`（判断"现在改图会不会派生"）、`event.bindings`/`event.read`/`event.submit`（事件驱动图）、`run.control` 其余动作（pause/resume/retry_*）、`checkpoint.list/read`（失败重跑与分叉定位）、`output.history`/`output.artifact.read`（多轮对比与产物）、`object.list/read`、`information.read`、`manifest.read`（运行内数据面）。

**明确不由 agent 直接操作**：`request-evidence.write/read/resolve`、`receipt.read`（前端 outbox 的"原请求证据"协议；后端命令本就不强制走它）、`draft.*`/`consumer.*`（前端草稿层，`draft.save` 已退役）、`packages.configure`（全局包安装）、`definition.archive/delete`、`session.archive/delete`（破坏性生命周期）、前端轮询/刷新/坐标缓存/UI 锁。

**agent 分析判断最需要的信息（L0，缺了会做错决定）**：① 当前基线（`revision` + `edit_sequence` + 文档）；② 节点契约（端口/类型/配置 schema/可调字段）；③ "这次改动合法吗、会不会派生/被锁"（编译诊断 + 分类预览 + 有效会话）；④ 运行到哪、哪个节点失败、输入是什么；⑤ 输入从哪来、输出在哪、怎么读。

**最大的三个缺口**：① 没有正式的"保存前试算"接口（只有 `definition.inputs` 兼职）；② 文档契约（schema 3 精确键集、版本号语义、保存协议、错误码）只存在于代码与单一示例，不可机器读、诊断无字段路径；③ agent 的工具层不存在——`workflow_tool_catalog.py:6-17` 只有 `inspect_text`/`final_answer` 两个工具，仓内也没有 MCP/harness 接入代码。

---

## 1. 前提、场景与证据边界

### 1.1 本文假设的接入场景

- agent（代码 agent / 全能 agent）作为 **management 调用方**，直接调用图应用命令层：`POST /api/graph/commands`、`POST /api/graph/queries`，参数形如 `{"operation": "...", "parameters": {...}}`（`backend/src/phase1_agent/graph_http.py:62-76`）；同一套操作也可进程内直调（`graph_application.py` 的 `command`/`query`，`graph_application.py:105-127`）。
- **不覆盖**：沙箱内编写新节点代码、安装新包、以及"智能管理者"这一产品能力的入口/任务编排。这些在既有勘察里明确尚无产品闭环（`docs/中期检查2/63-项目整体架构当前代码完全未涉及部分-2026-10-16.md` §5-6：代码沙箱环境、把成果提交工作流的入口都不存在）。
- 本文不把任何"建议新增接口"写成已批准设计——见 §7 与 §11，需要你决定后再进入契约/实施。

### 1.2 证据等级

| 等级 | 含义 | 本文中的使用 |
|---|---|---|
| 代码事实 | 直接读到的实现、常量、行号 | 绝大多数结论 |
| 数据事实 | 读开发库/样例文件得到 | 样例工作流规模、节点覆盖 |
| 推断 | 由代码行为推导、未实测 | 数据量级、agent 调用体感 |

未运行测试、未启动服务（本轮只读约束）。数据量级若来自开发库，均为只读 `mode=ro` 读取。

---

## 2. 为什么是"这个顺序"（六条硬约束）

这六条决定了 agent 的操作优先级，不是偏好问题：

1. **写是 CAS，必须带基线**：`definition.update` 要求 `expected_edit_sequence` 与当前值相等，否则 409 `stale_edit_sequence`；文档 `revision` 还必须等于当前 `fixed_definition_revision`，否则 409 `stale_revision`（`workflow_configuration.py:408-409`、`:424-425`）。所以 agent 每次写前都必须先读（P0-2）。
2. **保存不编译**：`definition.update` 只做结构校验、对象登记/预加载、策略与分类，**不跑 `GraphCompiler`**，也不校验节点 `config_schema` 完整语义（`workflow_configuration.py:390-416`；编译器只在 `graph_service.py:921-924`、`:1469-1472`、`graph_input_declarations.py:87`、`graph_public.py:128` 使用）。所以"写成功"不等于"能跑"，agent 必须先试算（P0-3）。
3. **运行依赖会话，会话钉修订**：`session.create` 要求 `definition_revision` 等于当前 fixed 修订（`graph_service.py:677-687`），`run.start` 又要求 `expected_revision` 等于会话 `revision`（`graph_service.py:891-896`；CAS 在 `:867`）。所以顺序只能是 写 → 建会话 → 跑（P0-4→P0-5→P0-6）。
4. **失败运行会占锁**：只要工作流定义下有会话的 `active_chain_run_id` 非空，任何配置保存都被 409 `configuration_run_locked` 拒绝（G21，`workflow_configuration.py:374-388`）；而 `active_chain_run_id` **只在运行成功时清空**（`graph_service.py:1534-1535`），失败/归档失败/暂停都继续占着。所以 agent 跑挂后想改图，必须先 `run.control` → `close`（P0-8）。
5. **有会话时改 fixed = 派生新工作流**：存在未归档会话，且本次改动含 fixed 类字段时，不确认就返回 `confirmation_required` 且零写入；`confirm_derivation=true` 时后端**新建一个 workflow_definition_id**（`workflow_configuration.py:413-414,429-449`）。所以 agent 必须知道"改的是哪份定义、新 id 是什么"，不能假设原地保存（P0-4 的语义 + P1 的 `definition.sessions`）。
6. **运行期间完全不能改图**：G21 连改 name/title/position（display 类）都拒绝（`workflow_configuration.py:374-388`，校验在分类之前 `:407`）。agent 不能"边跑边改"，只能"跑完/关掉再改"。

> 附带结论：agent 路径天然绕开了前两轮排查里的前端异步机制——它不需要 5 段式请求证据链（`request-evidence.write`→`read`→`definition.update`→`read`→`resolve`），也不需要 350ms 轮询与草稿层；一次整文档提交就能批量改多处结构。前端那套是为"人编辑体验 + 未知结果恢复"服务的（见 [01-其他交互与同步机制梳理](../编辑同步冗杂排查-2026-11-04/01-其他交互与同步机制梳理.md)）。

---

## 3. 第一优先（P0）：八个操作

### P0-1 读目录与契约：`catalog.node-types{protocol_version:2}` + `platform`

- **为什么第一**：agent 要生成合法文档，先得知道"存在哪些节点类型、每个端口的 `data_type`/`data_schema_version`/`accepted_types`、`config_schema`、哪些字段属于可调（`adjustable_configuration`）、默认策略（`default_configuration_policy`）、对象访问声明（`object_accesses`）、节点间配置引用（`config_node_references`）"。这些恰好都在目录里。
- **现状**：v2 返回 `node_types` + `platform` 全量（`graph_service.py:470-473`；节点字段见 `graph_contracts.py:394-426`；platform 见 `graph_platform.py:157-181`：`package_lock`、`execution_package_lock`、`data_types`（含 `schema`/`default_value`）、`executors`、`pause_support`、`services`、`frontend_extensions`、`package_diagnostics`、`local_package_sources`）。
- **缺口**：`configurationFields`（哪些配置字段被前端扩展编辑器接管）只存在于前端包实现（如 `frontend/src/domain/workflowFrontendPackage.ts`、`workbenchFrontendHost.ts`），后端目录不暴露；这只影响工作台 UI，不影响 agent 保存合法性，但 agent 若要通过画布字段名对齐描述会读不到。
- **agent 侧建议**：目录与 platform 一次性载入并作为只读上下文缓存；改动包/节点版本假设时才重拉。

### P0-2 读定义与基线：`definition.summaries` + `definition.read`

- **summaries**：每定义 8 字段——`workflow_definition_id`/`display_name`/`fixed_definition_revision`/`edit_sequence`/`archived`/`node_count`/`session_count`/`has_current_configuration`，无分页（`workflow_directory.py:43-66`；operation 声明 `graph_application_contracts.py:152`）。用它做"找到目标 + 粗看基线"。
- **read**：不带 `revision` 返回**当前合成文档**（schema 3 全字段 + `name` + `edit_sequence` + `revision`）；带 `revision` 返回该固定修订原文，**没有 `edit_sequence`**、也没有当前 title/position 与 adjustable 值（`configuration_service.py:47-55`）。这份"当前文档 + `revision` + `edit_sequence`"就是 agent 唯一的写基线。
- **缺口**：没有"修订列表/历史/差异"查询——`definition.read` 只能按 number 猜读某个修订（见 §6-L1）。

### P0-3 保存前试算：`definition.inputs`

- **为什么必须**：因为保存不编译（约束 2），而 `run.start` 编译失败时**整个 prepare 事务回滚、不产生任何 chain/node_run 记录**（`graph_service.py:898-962`、`:1013-1029`），错误只在 HTTP 响应体里，事后无法从运行历史复读。若 agent 直接保存再跑，会把"图不合法"变成"运行没记录、只有一次性 4xx"。
- **现状**：`POST /api/graph/queries` `{"operation":"definition.inputs","parameters":{"document":…}}`（声明 `graph_application_contracts.py:154`；实现在 `graph_application.py:137-141` → `graph_input_declarations.py:80-96`）。行为：先 `validate_graph_document`（结构错误 → 4xx，`error.diagnostics`），再 `GraphCompiler.compile`，编译错误收进 **200 响应的 `diagnostics[]`**，并返回 `inputs`/`required_for_run`（编译失败时 `required_for_run=null`）；不落库。
- **缺口**：不是正式 validate——诊断无字段路径（只有 `code/message/node_id/port_id/edge_id/dependent_outputs`，`contract_errors.py:10-28`）；不是"保存预演"：不返回 `changed_categories`、不预览"是否需要 `confirm_derivation`"、不在校验里包含包可用性与 `run.start` 的 preflight（`graph_service.py:925-936`）。
- **agent 侧建议**：每次写前都先跑一次；把它当作"编译闸门"，而不是"保存结果预测"。

### P0-4 写：`definition.update`

- **契约**：必填 `workflow_definition_id`、`document`、`expected_edit_sequence`、`idempotency_key`；可选 `confirm_derivation`（`graph_application_contracts.py:51-53`）。**只收完整文档，没有结构补丁**——增删节点/连线、改 `config`/`title`/`position` 都只能整文档提交；唯一的窄改通道是前端节点专用的 `patch_input`，只允许 `tools.editable-input@1.text`（`configuration_service.py:36-45`、`configuration_mutations.py:26-38`）。
- **文档要求**：`schema_version` 必须为 3；`workflow_definition_id` 必须等于参数；`revision` 必须等于当前 fixed 修订；带 `edit_sequence` 时必须等于 `expected_edit_sequence`（`workflow_configuration.py:394-404,424-425`）；整文档 ≤ 4,000,000 字节、节点 ≤ 512、边 ≤ 4096（`graph_contracts.py:28-31`、`:119,150`）。
- **四种 outcome**（`workflow_configuration.py:429-459`）：
  - `confirmation_required`：有有效会话 + 含 fixed 改动 + 未确认 → **零写入**，回执里带 `changed_categories` 与 `source_has_effective_session`；
  - `derived`：确认后 → 换新 `workflow_definition_id`、`revision=1`、`edit_sequence=1`；
  - `saved`：原地保存；含 fixed 时 `revision += 1`、`edit_sequence += 1`；display-only 只推进 `edit_sequence`、不产生新修订；
  - `unchanged`：没有实际变化，`edit_sequence` 不变。
  - 回执返回 `outcome`/`workflow_definition_id`/`definition_revision`/`edit_sequence`/`changed_categories`/`node_id_map`/`session_id`/`document`（`workflow_configuration.py:461-469`）。
- **新建工作流**：用新的 UUID4、`revision=1`、不带 `edit_sequence`、`expected_edit_sequence=0`（`examples/workflows/agent-context-chat/README.md:38-44`；新建分支在 `workflow_configuration.py:418-423`）。
- **agent 必须处理的坑**：G21 运行锁；`expected_edit_sequence` 与 `revision` 双 CAS；派生后 id 变化（后续 `definition.read`/`session.create` 都要换新 id）；幂等键重放会返回旧回执（`graph_store.py:131-145`）。
- **缺口**：无 dry-run、无结构补丁、错误无字段路径；agent 要么整文档回传（对 500+ 节点图很重），要么接受"改一条边也要传全量"。

### P0-5 建会话：`session.create`

- 必填 `workflow_definition_id`、`definition_revision`、`idempotency_key`（`graph_application_contracts.py:62-63`）；`definition_revision` 必须等于当前 fixed 修订，否则 409 `stale_revision`（`graph_service.py:677-687`）；201 返回完整 session 视图（含 `workflow_session_id`、`revision`）。
- **agent 注意事项**：会话一旦建立，"改 fixed 就要派生"就生效（约束 5）。所以 agent 应先把图改到满意，再建会话；如果必须在已有会话上继续改，准备好接受新定义 id。

### P0-6 运行：`run.start`

- 必填 `expected_revision`、`idempotency_key`；可选 `inputs`（`graph_application_contracts.py:66`）。前置：`expected_revision` 等于会话 `revision`、无 active run、包可用、`inputs` 为 dict 且无保留键、编译与实现校验通过（`graph_service.py:867-869,903-926`）。
- **语义**：202 立即返回 session 视图（prepared + `active_chain_run_id` + `revision+1`），执行在后台线程（`graph_service.py:964-983`）。agent 必须把它当异步任务，轮询 `session.read`/`run.read` 直到链终态。
- **缺口**：`inputs` 的字段要求只能靠 `definition.inputs` 的 `required_for_run` 推（编译失败时为 null）；运行输入与 external input 声明的对应关系没有一份"运行契约"查询。

### P0-7 观测：`session.read` + `run.read` + `output.list/read`

- **session.read**：完整视图——session 头/metadata/`data_revision`/`head`/`status`/`can_submit`/`available_actions` + 节点（status/diagnostic/run outputs payload）+ 全部 chains + 全部 outputs + 会话 data/objects（`graph_service.py:792-859`）。**无裁剪、无分页**，会话长了会很大（推断：payload 上限 4MB）。
- **run.read**：`chain`（status/ordered_nodes/completed_nodes/next_node_index/inputs/outputs/diagnostic）+ `node_runs`（含重试尝试展平：status/input_values/input_refs/output_refs/reads/effects/config/diagnostic）+ 该链全部 outputs + `runtime_facts` + start/end manifests（`graph_service.py:723-737`；结构 `graph_records.py:32-37`）。定位失败节点靠 `chain.next_node_index`/`ordered_nodes` 与 `node_run.diagnostic.node_id`。
- **output.list/read**：只覆盖节点显式声明的 `public_outputs` 端口，带 `availability`/`reason_code`/`run source`；可查历史（`graph_public.py:201-242`）。想拿"任意中间输出"要走 `run.read.outputs` 或 `session.read.outputs`。
- **缺口**：诊断无字段路径、无期望/实际类型、无时间戳/耗时（`created_at` 列未进 payload）；诊断键不统一（节点 `code` vs 存储层 `reason_code`）；多错只报首个。

### P0-8 解除阻塞：`run.control`（至少 `close`）

- 动作集：`pause`/`resume`/`close`/`retry_archive`/`retry_acceptance`/`retry_failed_node`（`graph_service.py:1769-1772`）。
- **为什么算 P0**：G21 只看 `active_chain_run_id`，而它**只在 succeeded 时清空**（`graph_service.py:1534-1535`）——失败的运行会一直占着，agent 不改图就永远是 409 `configuration_run_locked`。也就是说：没有 `close`，agent 的"失败→改→重跑"闭环在失败场景直接断掉。
- **建议档位**：把 `close` 视为 P0；`pause`/`resume`/`retry_*` 归 P1（只在需要人工/半自动干预时用）。

> **P0 依赖关系**：P0-2 → P0-4 → P0-5 → P0-6 → P0-7，P0-3 在 P0-4 前每次执行，P0-1 一次性载入，P0-8 在失败后再回到 P0-4 之前执行。

---

## 4. 第二优先（P1）：从"能用"到"覆盖更多形态"

| 操作 | 解决的问题 | 现状与注意点 |
|---|---|---|
| `definition.copy` | 安全试验：把现有定义复制成新定义再改，不污染正式定义；也是派生确认之外的备选 | 前置 `expected_source_definition_revision`/`expected_source_edit_sequence` 匹配；目标三处均无记录；返回新 id、`revision=1`、`node_id_map`、文档（`configuration_service.py:105-132`；`workflow_configuration.py:471-555`） |
| `definition.sessions` / `session.summaries` | 判断"现在改图会不会派生"；列出该定义的会话 | `definition.sessions` 返回每个会话的**完整视图**，无分页、很重（`graph_service.py:715-721`）；`session.summaries` 是 8 字段窄投影（`workflow_directory.py:104-129`）——agent 优先后者 |
| `event.bindings` / `event.read` / `event.submit` | 事件驱动工作流：查看声明事件、读事件链、提交事件 | `event.bindings` 返回 `can_submit`（= 无 active chain）与事件声明（`graph_events.py:65-79`）；`submit` 要求会话归属精确 definition+revision、事件在固定修订 `event_bindings` 中声明（`graph_events.py:52-60,81-110`）。**注意：没有"节点正在等待事件"的链状态**——`event.submit` 是开新事件链，不是推进挂起的节点（见附录 §D.4） |
| `run.control`（pause/resume/retry_*） | 长任务中断续跑、归档失败重试、失败节点重试 | 动作与状态机见 `graph_service.py:1632-1685` |
| `checkpoint.list/read` | 失败后从哪个点重跑/分叉 | `checkpoint.read` 只有 6 个 header 字段，**没有状态/内容**，也没有 checkpoint→snapshot 映射（`graph_service.py:739-790`）——目前只能"定位"，不能"读状态" |
| `output.history` / `output.artifact.read` | 多轮运行结果对比、导出产物 | 只覆盖 `public_outputs` 声明端口；未声明 403（`graph_public.py:50-63,201-277`） |
| `object.list/read`、`information.read`、`manifest.read` | 运行内持久数据（会话对象/data/信息事实） | 对象读需要 `node_id` 授权（`graph_platform.py:218-231`；`session_objects.py:106-120`）；`information.read` 有分页与字节上限（`graph_information.py:164-203`）；`manifest.read` 需 snapshot 或 chain+boundary（`graph_platform.py:271-294`） |
| `platform` 的 `package_diagnostics` | 解释"为什么节点类型不可用/包没装上" | 已在 P0-1 一次性载入；单独按需查询也可 |

**P1 的取舍标准**：只在对应形态出现时才需要——没有事件节点就不用 `event.*`；不判断派生就不用 `definition.sessions`；不读中间数据就不用 `object.*`。P0 则是任何形态都缺不了。

---

## 5. 明确不由 agent 直接操作（前端适配残留与破坏性动作）

| 机制 | 归属 | 为什么 agent 不需要 | 证据 |
|---|---|---|---|
| `request-evidence.write` / `.read` / `.resolve` | 前端 management 写 outbox | 为"用户编辑请求在断线/未知结果后可恢复"设计；后端命令**本就不强制**走它（capability 里 condition 明说 "backend commands are not forced through request evidence"） | `workflow_capability.py:357-372`；声明 `graph_application_contracts.py:38-41` |
| `receipt.read` | 前端回执核对 | 需要原请求参数才能读；agent 直接用命令返回值即可 | `graph_application_contracts.py:148`；`graph_receipts.py:58-106` |
| `draft.save` / `draft.*` / `consumer.*` | 前端草稿层 | 管理侧 `draft.save` 已退役进 `RETIRED_RECEIPT_COMMANDS`；consumer 草稿是消费者前端通道 | `graph_application_contracts.py:117-118`、`:83-123` |
| `packages.configure` | 全局包安装 | 属产品配置治理，不是"编辑一张图"；会改全局能力集合 | `graph_application_contracts.py:78`；`workflow_capability.py:176` |
| `definition.archive` / `delete`、`session.archive` / `delete` | 生命周期 | 破坏性、不可逆；agent 不应自主删工作流 | `graph_application_contracts.py:58-61,64-65`；`is_lifecycle` 判定 `:212-222` |
| `checkpoint.fork`、`object.write` | 会话分叉/对象写 | 前端生产零调用；分叉需要 4 个 revision 同时匹配、对象写需要节点授权与 CAS，属运行内数据动作 | `graph_service.py:689-706`；`graph_platform.py:233-264` |
| 前端轮询（`schedulePoll`）/`refresh`/接受仲裁/坐标与句柄缓存/UI 锁 | 前端呈现层 | agent 没有画布，不需要 350ms 轮询与乐观视图；它对"网络慢"的容忍度问题只影响前端 | 见 [01-其他交互与同步机制梳理](../编辑同步冗杂排查-2026-11-04/01-其他交互与同步机制梳理.md) |

**一个易混点**：`profileMode`/"全能 agent"若指 **工作流内的 Agent 节点**（`agents.execute@10`），它当前的工具体系里只有 `inspect_text` 与 `final_answer`（`workflow_tool_catalog.py:6-17`），**没有任何图编辑工具**。若路线是"让工作流内 Agent 也能改图"，则缺口不是接口而是工具注册面（属新架构，需单独决策）。

---

## 6. Agent 分析判断最需要的信息

按"缺了会怎样"分级，而不是按接口多少分级：

### 6.1 L0：缺了会做错决定（必须随上下文或每次入参）

| 信息 | agent 要回答的问题 | 现有来源 | 缺口 |
|---|---|---|---|
| 当前基线（文档 + `revision` + `edit_sequence` + `name`） | "我在改哪份、改完写回哪、CAS 用哪个数" | `definition.read`（`configuration_service.py:47-55`）、`definition.summaries`（`workflow_directory.py:43-66`） | 修订历史只能猜号读 |
| 节点契约（类型/端口/`data_type`/`data_schema_version`/`accepted_types`/`port_modes`/`config_schema`/`adjustable_configuration`/`default_configuration_policy`/`object_accesses`/`config_node_references`） | "要搭这个功能该用哪些节点、端口怎么连、配置字段是什么" | `catalog.node-types` v2（`graph_service.py:470-473`；字段 `graph_contracts.py:394-426`） | 无字段 label/示例（推断：schema 多为裸 JSON Schema）；无前端字段接管表 |
| 数据类型契约（`schema`/`default_value`/`checkpoint_admission`） | "端口传的数据长什么样" | `platform.data_types`（`graph_platform.py:157-175`） | 无 |
| 保存规则与后果（schema 3 精确键集、`revision`/`edit_sequence` 语义、display/adjustable/fixed 分类、G21、派生/确认） | "这次改动会被接受、还是派生、还是被锁" | 只在代码里：`graph_contracts.py:99-232`、`workflow_configuration.py:244-265,374-388,390-469`；回执返回 `changed_categories`/`outcome` | **无预查询**：只能保存后从回执反推；无分类预览接口 |
| 保存前合法性诊断（结构 + 编译 + 必填输入） | "我这份文档能跑吗、错在哪" | `definition.inputs`（`graph_input_declarations.py:80-96`） | 无字段路径、无分类/派生预览、不含包可用性与 run preflight |
| 运行状态与节点诊断（phase/节点/错误/输入/冻结 config） | "跑到哪、哪个节点失败、为什么、它的输入是什么" | `session.read`（`:792-859`）、`run.read`（`:723-737`）、`runtime_facts` | 无时间戳/耗时、无 phase、无期望 vs 实际类型、多错只报首个、`code`/`reason_code` 不统一 |
| 输入与输出（`required_for_run`、`inputs`、`outputs`、public_outputs） | "跑之前要喂什么、跑完结果在哪" | `definition.inputs`、`run.start.inputs`、`run.read.outputs`、`session.read.outputs`、`output.list/read`（`graph_public.py:201-242`） | 运行输入契约无独立查询（依赖 inputs 编译成功） |
| 幂等/重放语义（`idempotency_key` 必填、重放返回旧回执、`idempotency_conflict`） | "我重试会不会重复写、拿到的是新结果还是旧回执" | `graph_application_contracts.py:51-53`；`graph_store.py:131-145` | 无文档化协议；agent 需自行生成稳定 key |

### 6.2 L1：缺了会降低效率/需要试错（可按需查询）

| 信息 | 用途 | 现有来源 | 缺口 |
|---|---|---|---|
| 有效会话存在性（决定是否派生） | "我现在改图会不会产生新定义" | `definition.sessions`（重）、`session.summaries`（窄） | "effective session"（未归档）判定未单独暴露，只能按列表推断（`workflow_configuration.py:413-414`） |
| 包/节点可用性与诊断 | "这个节点为什么不出现/包为什么不可用" | `platform.package_diagnostics`/`local_package_sources`/`package_publication`（`graph_platform.py:168-175`） | 保存时不校验包可用，运行才报 |
| 定义目录口径（node_count/session_count/archived） | 批量管理多张图 | `definition.summaries` | 无分页（推断：定义多了会大） |
| 事件与可提交性 | 事件型工作流"现在能不能发事件" | `event.bindings.can_submit`（`graph_events.py:65-79`）、`session.actions`（`graph_application.py:143-153`，**不含事件/active chain id**） | 需同时读 `session.read` 才知 active chain |
| 会话视图裁剪 | 会话长时避免拉全量 | `session.summaries` | `session.read` 无裁剪/无分页 |
| 运行历史/多轮对比 | 判断"这版比上版好在哪" | `session.read.chains`、`output.history` | 无跨会话对比、无摘要统计 |

### 6.3 L2：可后置（特定分析才需要）

| 信息 | 用途 | 现状 |
|---|---|---|
| 修订历史/差异/审计 | 回退、看谁改了什么 | **完全缺失**：只能 `definition.read(revision=N)` 逐号猜；无 `definition.revisions`、无 diff（`configuration_service.py:47-55` 只支持单修订读） |
| checkpoint 状态/内容/snapshot 映射 | "能否从这个点恢复/分叉、状态是什么" | `checkpoint.read` 只有 header；无 checkpoint→snapshot 映射（`graph_service.py:739-790`） |
| 会话对象/数据/信息事实 | 深入运行内数据 | `object.*`、`information.read`、`manifest.read` 可用但需要 node 授权/分页/边界参数 |
| 资源与全局内容（resource/content） | 跨工作流资源治理 | `resource.*`、`content.metadata.*` 可用，与单图编辑关系弱 |
| 前端扩展字段接管 | 只在要"对齐画布显示"时需要 | 后端无此信息 |

### 6.4 一句话版本

agent 判断链是：**"我在改哪份（基线）→ 能用什么（节点契约）→ 这样写合法吗（结构+编译）→ 会被接受还是派生（分类/会话/锁）→ 跑成什么样（运行诊断）→ 输入输出在哪（数据面）"**。现在这条链上，"合法吗"和"会被接受还是派生"两环最弱——都是保存后才知道；这正是 §7.1 要补的。

---

## 7. 现有缺口与最小设计建议（只描述，未实施，待决定）

### 7.1 正式化"保存前试算"：`definition.validate`

建议把 `definition.inputs` 的能力升级/新增为一个正式查询（仍不落库）：

```
POST /api/graph/queries
{"operation":"definition.validate",
 "parameters":{"workflow_definition_id": "...", "document": {...}, "expected_edit_sequence": N}}
```

返回建议：`{ok, diagnostics:[{code,message,path,node_id,port_id,edge_id,dependent_outputs}], categories_preview:{display:[],adjustable:[],fixed:[]}, derived_preview:{required:bool, source_has_effective_session:bool}, required_inputs:[...], package_available:bool}`。

- 复用：`validate_graph_document`（`graph_contracts.py:99-232`）+ `GraphCompiler`（`graph_contracts.py:819-1048`）+ `classify_configuration_changes`（`workflow_configuration.py:244-265`）+ `_require_no_unfinished_run`/有效会话判定（`:374-388,413-414`）。
- 补：诊断加 JSON Pointer `path`；保留被丢弃的内部 diagnostics（`graph_contracts.py:941-949` 目前把内层诊断丢了）。
- 收益：agent 每次写前一次调用即可拿到"合法 + 分类 + 派生 + 输入"四件事，避免"保存成功但跑不起来"和"意外派生新定义"。

**为什么要新增而不是改现有**：`definition.inputs` 语义是"取编辑期外部输入声明"，被前端编辑面板使用；直接扩它会改变前端契约（需评估）。这是需要你决定的边界。

### 7.2 让文档契约可机器读

现在 schema 3 的精确键集、上限、版本号语义、保存协议、错误码只存在于代码与一个 384 行样例里：

- 无 GraphDocument JSON Schema（`backend/schemas/` 只有 `contracts-v2` / `run-events-v1`）。
- 唯一完整样例 `examples/workflows/agent-context-chat/workflow.json`（384 行，11 节点/11 边/2 object_bindings/10 package_lock），**不含** `session_preloads`/`event_bindings`/`control_edges`（`workflow.json:1-384`）。
- 建议：发布 JSON Schema + 一页"保存协议"（`revision`/`edit_sequence`/CAS/幂等键/派生确认/display-only）+ 错误码字典 + 1-2 个覆盖 `session_preloads`/`event_bindings` 的样例。这样 agent 的上下文可以只放 schema + 一页协议，而不是整段代码。

### 7.3 agent 的工具层/身份（最大的结构性缺口）

- 现状：工作流内 Agent 的工具目录只有 `inspect_text` + `final_answer`（`workflow_tool_catalog.py:6-17`）；仓内没有 MCP/harness 接入代码（后端 `src` 搜索 `harness`/`mcp` 无命中）；命令层只有 HTTP 路由（`graph_http.py:62-130`）与进程内 `GraphApplication.command/query`（`graph_application.py:105-127`）。
- 需要你决定的三件事：① 接入形态（进程内工具 / HTTP / MCP / 沙箱副本）；② 权限面（直接拿全量 management scope，还是收窄到 `definition.*` + `run.*`）；③ 是否允许直接改正式定义，还是只在派生副本上改、由人确认后合入。
- 桥接建议（不改现有契约）：命令层已是 `{operation, parameters}` 的统一面，工具层只需把 P0 八个操作映射为工具，并在工具内部处理 `expected_edit_sequence` 重读、`idempotency_key` 生成、`confirm_derivation` 策略、失败后 `run.control close`。

### 7.4 运行诊断统一（可选但价值高）

- 统一失败 envelope：`phase`（compile/prepare/node/settle/storage）+ 稳定 `code`/`message` + 位置；`run.start` 错误体与 `run.read.diagnostic` 同构（当前 prepare 失败不留记录、键名 `code` vs `reason_code` 并存）。
- 补 `config_path`/`expected`/`actual`（配置 schema 只保留 jsonschema message，`graph_contracts.py:733-739`；类型不匹配只报 "Linked content types differ"，`:968-970`）。
- `run.read` 补 `failure_phase`、attempts、时间戳/耗时。

### 7.5 修订历史/差异（可选，长期）

`definition.revisions` + `definition.diff`。目前 agent 无法回答"上一版是什么、能不能回退"，只能按 revision 号逐个读。

---

## 8. 一次完整闭环的时序（含硬约束）

```
[一次性] catalog.node-types(v2) + platform            # 节点契约/包/类型
   │
   ▼
definition.summaries ──► definition.read               # 目标 + 基线(revision, edit_sequence)
   │
   ▼
[本地构造文档] schema3 + revision不变 + 带edit_sequence + position自布局 + package_lock匹配
   │
   ▼
definition.inputs(document)  ── 结构错4xx/编译错200+diagnostics ── 失败则回到构造
   │
   ▼
definition.update(document, expected_edit_sequence, idempotency_key[, confirm_derivation])
   │   outcome = saved | unchanged | derived | confirmation_required
   │   derived → 记住新的 workflow_definition_id / revision=1
   ▼
session.create(workflow_definition_id, definition_revision)   # 必须=当前 fixed
   │
   ▼
run.start(expected_revision=session.revision, inputs, idempotency_key)  → 202
   │
   ▼
轮询 session.read / run.read  直到 status ∈ {succeeded, failed, archive_failed, recovery_unavailable, closed}
   │
   ├── succeeded ──► output.list/read（public）或 run.read.outputs（任意中间输出）
   └── failed ─────► run.read 定位 node_run.diagnostic
                     │
                     └── run.control(close)   # 释放 active_chain_run_id，否则 G21 锁死
                     └── 回到构造/definition.update
```

硬约束清单（每条都会真实挡住 agent）：
1. 运行未结束（含失败/暂停）→ 保存被 409 `configuration_run_locked`（G21）。
2. 有有效会话 + 改 fixed 字段 → 必须 `confirm_derivation`，否则零写入；确认后 id 变化。
3. 每次写必须带正确 `expected_edit_sequence` 与 `revision`，否则 409。
4. 保存不编译；编译失败只在 `run.start` 响应里（无 run 记录）。
5. 新建定义必须 `revision=1` + `expected_edit_sequence=0`。
6. `run.start` 只接受 `inputs` 为 dict 且无保留键；输入名以 `definition.inputs` 的 `required_for_run` 为准。
7. `event.submit` 只在该会话无 active chain 时可用（不是推进挂起节点）。

---

## 9. 与已交付两轮文档的关系

- **前端异步同步那套对 agent 不适用，而且是负担**：每次画布编辑 5 个串行 POST（证据写/读/`definition.update`/读/解决），约 6 行 28KB 写入、8 次 `BEGIN IMMEDIATE`、两次全历史扫描（约 49ms）——见 [编辑同步冗杂排查 README](../编辑同步冗杂排查-2026-11-04/README.md)。agent 直接调命令层可以一次整文档提交完成多结构修改，不需要证据链、轮询、草稿层；只剩"整文档重传"这一项成本（详见 §3-P0-4 缺口）。
- **O(n²) 的编辑历史只惩罚人机前端路径**：run/session 读取不碰证据/幂等表，agent 的运行观测不受影响；但 agent 若走前端 HTTP 路径（而非命令层）就会继承这层成本。
- **删除/连线那轮的结论不受影响**：那是画布渲染与 Vue Flow 句柄问题，与 agent 操作面正交。

---

## 10. 未确认点

1. **接入形态未定**：进程内工具 / HTTP / MCP / 沙箱副本。本文按"直接调命令层"假设；若走 MCP 或沙箱，§7.3 的设计分叉会变。
2. **是否允许 agent 直接写正式定义**未定。若只允许派生副本，则 `definition.copy` 从 P1 升为 P0，`confirm_derivation` 的策略也要改（当前是"确认后派生新定义"）。
3. **`confirm_derivation` 在无人可问时的默认策略**未定：自动派生 / 返回失败要求上层确认 / 禁止在有会话时改 fixed。
4. `event.submit`/事件型工作流是否属于目标场景未定——若不涉及，`event.*` 可整体延后。
5. `budget_exhausted` 状态在源码无写入点（疑遗留），与 agent 无关但记录在此。
6. 数据量级均为代码上限推断：开发库当前 44 修订、`edit_sequence=103`、0 会话/0 运行/0 输出（数据事实），因此 `session.read`/`run.read` 的真实体量无法实测。
7. 目录规模的精确数字未测（需启动服务拉 `/node-types/v2` 才能统计）；本文未据此给上下文预算结论。

---

## 11. 需要你决定的四个分叉

1. **接入形态**：agent 以什么身份调用（进程内 / HTTP / MCP / 沙箱）？这决定 §7.3 的工具层形态。
2. **写入权限边界**：允许直接改正式定义，还是只在派生副本上改？这决定 `definition.copy` 是否 P0、`confirm_derivation` 是否保留人工确认。
3. **是否先做 `definition.validate`**：这是 §7.1 的核心建议，也是 agent 闭环最大缺口；做与不做决定 agent 是否需要"保存后靠 run.start 试错"。
4. **是否把"文档契约机器可读"作为前置**（§7.2）：不做的话，agent 需要把 `graph_contracts.py:99-232` 的规则以自然语言/示例形式塞进提示词，可靠性依赖模型。

---

## 12. 证据索引（关键锚点）

| 主题 | 位置 |
|---|---|
| 命令/查询声明 | `backend/src/phase1_agent/graph_application_contracts.py:37-78`（COMMANDS）、`:83-123`（退役）、`:132-187`（QUERIES）、`:197-222`（authority 分档） |
| HTTP 路由 | `backend/src/phase1_agent/graph_http.py:62-76`（commands/queries）、`:80-106`（node-types/definitions/sessions）、`:108-130`（REST 包装） |
| 命令/查询入口 | `backend/src/phase1_agent/graph_application.py:105-127`（command/query）、`:58-88`（_invoke）、`:137-141`（_editor_inputs） |
| 能力声明与权限 | `backend/src/phase1_agent/workflow_capability.py:174-189`（management/execution/lifecycle 操作组）、`:357-372`（证据层仅前端 outbox） |
| 读定义/目录 | `configuration_service.py:47-55,57-79,81-90`；`graph_service.py:470-473,494-495,715-721,723-737,739-790,792-859` |
| 保存与分类 | `workflow_configuration.py:244-265`（classify）、`:374-388`（G21）、`:390-469`（save）、`:95-166`（策略/可调声明）、`:471-555`（copy） |
| 文档与编译校验 | `graph_contracts.py:28-31`（上限）、`:99-232`（validate_graph_document）、`:394-426`（NodeDefinition.to_dict）、`:819-1048`（GraphCompiler） |
| 试算 | `graph_input_declarations.py:80-96`（editor_input_declarations）、`:49-54`（diagnostics 转换） |
| 运行链 | `graph_service.py:677-687`（session.create）、`:891-896,964-983`（start）、`:898-962`（prepare/compile）、`:1632-1685,1769-1850`（control）、`:1534-1535`（succeeded 清 active） |
| 输出与数据 | `graph_public.py:201-277`（output.*）、`graph_platform.py:218-231,271-294`（object/manifest）、`graph_information.py:164-203`（information.read） |
| 事件 | `graph_events.py:52-60,65-79,81-110,112-139` |
| 错误与幂等 | `contract_errors.py:10-28`（DiagnosticError）、`graph_store.py:131-145`（幂等回放） |
| 样例 | `examples/workflows/agent-context-chat/workflow.json`（384 行）、`README.md:38-44`（导入协议） |
