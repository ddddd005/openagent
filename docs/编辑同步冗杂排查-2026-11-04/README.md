# 前端编辑 ↔ 后端异步同步：冗杂来源与卸除收益排查（只读）

- 日期：2026-11-04
- 范围：`frontend/src` 画布编辑保存链、`backend/src/phase1_agent` 的 `definition.update` / 原请求证据 / 运行入口
- 约束：只读。未改任何业务文件、未提交、未启停服务、未调用模型；数据库只以 `mode=ro` 只读查询；开发库 `.local/develop/workflow.sqlite`
- 方法：5 个只读子代理分区排查 + 主代理交叉复核与数据库取证；结论均挂 `file:line`；标注 **[代码]** / **[数据]** / **[推断]**
- 文档地图：本文件 = 分析正文；[00-结论与可信上下文.md](00-结论与可信上下文.md) = 结论、置信度分级、测量环境快照（含库 sha256）、方法与未确认边界；[01-其他交互与同步机制梳理.md](01-其他交互与同步机制梳理.md) = 同日第二轮简报（通道/锁、轮询、节点配置路径、草稿、死路径）；[附录-分区证据.md](附录-分区证据.md) = 五区逐条证据

---

## 0. 一句话结论

1. **冗杂成立，而且不是"画布乐观状态"造成的，而是"把每一次画布微编辑当成一次需要可恢复的持久化命令"造成的。** 单次编辑要 5 个串行 HTTP、约 6 行/28 KB 永久新增数据、2 次"返回该 owner 全部历史证据"的读取（成本随编辑次数线性增长、累计 O(n²)），开发库里证据/幂等两类表已占 67% 库容。
2. **主要产生地**：① 原请求证据 + 幂等 + 回执核实 + 恢复层（同一状态机在前端被独立实现 4 遍，前后端实现约 3,300 行、测试约 4,232 行）；② "服务端接纳副本 vs 本地可编辑副本 vs 已保存快照"的多副本仲裁（约 10 份前端真相、7 类后端记录、每次编辑约 30 个状态槽位）；③ 整份文档 PUT + 客户端重实现服务端保存语义（回执契约约 50 行、文档校验两套）；④ 双版本号 `revision`/`edit_sequence` + `display/adjustable/fixed` 分类 + 派生确认（因为"编辑可能发生在有会话/正在运行时"）。
3. **卸除后对"运行"的优化要分开看**：**执行循环本身没有收益**（run 只读 1 篇固定修订 + 1 行当前配置，证据表完全不在运行读路径上）；**但"编辑 → 运行"这段和运行入口有明确收益**：`run.start`/`run.control`/`session.create`/`event.submit` 也被包进同一套证据协议（一次运行命令 6 次证据调用 + 3 行额外持久化），保存事务与运行准备抢同一把 SQLite 写锁和同一把服务 `RLock`，且证据读取开销随编辑次数增长会拉长这个窗口。**运行正确性不依赖这层证据协议**，真正依赖的是会话固定修订、G21 运行期禁写、prepare 冻结节点配置、`expected_revision` CAS、运行 basis 冻结——这些都在后端，删证据层不动它们。

---

## 1. 先校正前提：生产路径上已经没有"乐观本地应用"

这是本次排查最重要的事实修正，决定"卸除"到底会损失什么。

- `acceptDraft` 在 `configurationApplication` 存在时只做 `void submitConfigurationDocument(id, next, "edit")` 然后 `return`，**不写本地 `entry.document`**：`frontend/src/stores/workflowGraph.ts:780-785`。
- 画布文档只在服务端回执被接纳后由 `projectAcceptedState` 整体替换：`workflowGraph.ts:388-423`（关键 `:415 source.document = accepted`、`:417 saved_document`、`:418 saved_revision`）。
- 生产装配必然安装配置应用（`frontend/src/App.vue:10` → `composables/useWorkflowFrontendHost.ts:22-23`），所以 `acceptDraft` 的本地应用分支（`workflowGraph.ts:786-791`）**生产不可达**。
- 测试直接固定该行为：在途时 `editorLocked === true`，紧接着的编辑返回 `null`（`stores/workflowConfigurationStore.test.ts:195-198`），回执与证据结案后文档才更新（`:209-211`）。

即：**"异步"当前 = fire-and-forget 提交 + 事后接纳**。用户点下连线后，画布要等 5 个串行请求 + 证据结案才看到那条线；在途窗口里编辑器整体加锁（`configurationSubmitting` 在第一个 `await` 之前同步置位，`workflowGraph.ts:487`），第二次编辑被 `canEdit` 拒绝后**静默丢弃**（`workflowEditing.ts:15` 返回 `null`，`workflowGraph.ts:1163-1166` 只 catch 不提示）。

> 结论：**"为编辑体验做的异步"在当前实现里并没有换来即时反馈**，只换来了协议复杂度与更长的保存窗口。若"卸除"指改成阻塞保存 + 保存中明确禁用编辑，交互延迟基本不变（今天也要等回执），但可省掉大部分协议。

---

## 2. 一次画布编辑的真实代价

触发：连线 / 删除 / 拖完节点 / 撤销 / 改标题 / 表单 `@change`，全部走同一条 `editable → workflowEditing.edit → acceptDraft → submitConfigurationDocument`（`workflowGraph.ts:1163-1166, 769-793, 472-532`；`application/workflowEditing.ts:13-26`）。文本输入是 `@input` 只记本地、`@change` 才提交，所以不是逐键，但**每次语义编辑 = 一次完整提交，无 debounce、无合并、无队列**（`GraphSchemaFields.vue:63-79`）。

### 2.1 请求序列（正常一次编辑 = 5 个串行 POST）

| # | operation | 作用 | 证据 |
|---|---|---|---|
| 1 | `request-evidence.write`（命令） | 把**含整份 document** 的原请求写入 `pending_commands` + `idempotency` | `application/workflowConfiguration.ts:233-234` → `adapters/requestEvidenceApi.ts:132-142`；后端 `request_evidence.py:243-272` |
| 2 | `request-evidence.read`（查询） | 复核唯一 pending / digest / 正文一致 | `workflowConfiguration.ts:237` → `requestEvidenceApi.ts:101-110` |
| 3 | `definition.update`（命令） | 唯一业务写入（整份文档 + `expected_edit_sequence` + key） | `workflowConfiguration.ts:251` → `adapters/workflowConfigurationApi.ts:8-18` |
| 4 | `request-evidence.read`（查询） | 结案前再复核一次 | `workflowConfiguration.ts:178` → `requestEvidenceApi.ts:111-121` |
| 5 | `request-evidence.resolve`（命令） | 证据结案 `verified` | `workflowConfiguration.ts:179-180`；后端 `request_evidence.py:274-295` |

变体：首次打开/上次证据失败后再编辑 = 6 次（多一次 `initialize` 读，`workflowConfiguration.ts:141-174`）；被 409 拒绝仍走 5 次；业务成功但结案失败仍 5 次；用户确认"派生新工作流" = 10 次（`workflowConfiguration.ts:279-289`；`workflowGraph.ts:461-471`）；未知后只读核实最少 3 次（`workflowConfiguration.ts:290-303`）。

### 2.2 传输量

每个请求都带整份文档：**2 次上传 + 5 次下载**。只做业务写入则 1 传 1 回（请求数 1/5，字节约 1/3.5）[代码：`requestEvidenceApi.ts:79-95,101-121,132-152`；`domain/workflowConfiguration.ts:70-119` 要求回执含完整 `document`]。

### 2.3 持久化增长（开发库实测，只读）

| 项 | 数值 | 来源 |
|---|---|---|
| 单次保存新增行 | 6 行左右：`pending_commands` 1 + 证据 write 幂等 1 + resolve 幂等 1 + `definition.update` 幂等 1 + `graph.application.command-identity.v1` 1~3 | `request_evidence.py:211-241,243-272`；`graph_application_identity.py:134-153`；`graph_store.py:131-169` |
| 单次保存新增字节 | 约 28 KB（证据请求 ~5.6 KB + write 5.96 KB + resolve 5.96 KB + update 6.52 KB + identity 0.53 KB） | 库内 197/197/177/563 行均值 |
| 库总况 | 7.41 MB；`idempotency` 1,200 行 / 3.94 MB + `pending_commands` 1.03 MB ≈ **67% 库容**；真正的定义历史（`records` 两表）只有 0.48 MB | 只读 sqlite |
| 单工作流编辑史 | 44 篇固定修订、`edit_sequence=103` → **59 次（57%）保存只改 display/adjustable、不产生修订**；`pending_commands` 该工作流 103 条 / 636,717 B；0 条 session | 只读 sqlite |
| 清理 | 证据行只把 `resolution` 改成 verified，**全后端无删除/清理代码** | `request_evidence.py:203-209,274-295` |

### 2.4 每次编辑要读多少历史（随编辑次数增长，累计 O(n²)）

`read_evidence` 是 `SELECT * FROM pending_commands WHERE owner_kind=? AND owner_id=? ORDER BY rowid`，**不带 resolution 过滤、不分页、逐条 `validate_evidence_record` 解析 request_json**：`backend/src/phase1_agent/request_evidence.py:203-209`、`:194-201`。前端同样全量强校验（`requestEvidenceApi.ts:122-131`）。

实测（只读，对 103 条记录的工作流）：**728,411 B JSON、`_record` 校验 80.9 ms、序列化 7.4 ms**；每次保存读 2 次、每次打开工作流再读 1 次。即今天编辑这个工作流，每保存一次要额外下载/解析约 1.5 MB 证据历史；编辑到 300 次时约 4 MB/次。

### 2.5 其它静态开销

- 保存事务内**两次全量遍历全部历史修订并逐篇 `validate_graph_document`**：`workflow_configuration.py:410`（结果只用于一个布尔判断）、`configuration_data.py:98`（只为了取 max revision）。实测 44 篇 ×2 ≈ **49 ms** 纯解析（下界，不含 SQLite/HTTP）。
- 一次保存合计约 **8 个 `BEGIN IMMEDIATE` 写事务**（每个 HTTP 请求开 store 就一次：`storage.py:44-105`；证据写/结案各 1；业务 1）。
- 同一份文档在保存路径上：后端 `validate_graph_document` **4 次**（`workflow_configuration.py:395`、`configuration_data.py:94`、`graph_records.py:167`、`graph_store.py:225-254`）；前端请求校验 2 次、回执校验 **4 次**（`workflowConfigurationApi.ts:9,13`；`workflowConfiguration.ts:217,189,114,68`）；`graphClone` / `graphJsonSignature` 每次编辑 ≥12 / ≥10 次。

---

## 3. 冗杂到底在哪里产生（按贡献排序）

### R1 原请求证据 / 幂等 / 回执核实 / 恢复层 —— 最大来源

- 目的：浏览器可能拿不到回执（断网、关页、5xx），于是把"精确原请求 + key"先落库，刷新后按坐标找回，**只读核实回执、绝不重发**（`workflowConfiguration.ts:290-303`；`graph_receipts.py:58-106`）。
- 规模：前端实现约 1,980 行（`workflowConfiguration.ts` 342、`requestEvidenceApi.ts` 153、`resourceOutbox.ts` 199、`workflowManagementRequests.ts` 186、`workflowConfigurationCopy.ts` 153、`resourcePendingStorage.ts` 61、`providerCredentialEvidence.ts` 98、`static/frontend-node-client.js` 555 + store 装配/恢复约 230），后端约 1,339 行（`request_evidence.py` 295、`graph_application_identity.py` 207、`graph_receipts.py` 619、`frontend_node_evidence.py` 218 + 分派/契约约 120）；专职测试 18 文件约 4,232 行。
- **同一状态机在前端被独立实现 4 遍**：`resourceOutbox.ts`（通用）、`workflowConfiguration.ts`（内联手写，画布保存走这条）、`providerCredentialEvidence.ts`（Key 通道）、`static/frontend-node-client.js`（iframe 第二前端）。
- **它自己制造了比它要修的故障更糟的状态**：证据写成功但业务未发出/回执未达 → 该工作流 pending 且无回执；配置/复制/管理三个通道的端口**没有** `archivePending`，因此没有 `resourceOutbox.abandon` 那种"放弃并解锁"出口（`resourceOutbox.ts:157-182` 仅资源/凭据通道可用；`workflowConfiguration.ts:24-29`、`workflowManagementRequests.ts:16-25` 未提供）→ 编辑器、运行、归档、删除全部锁死，唯一出路是刷新，若证据确实落库则刷新后仍锁死（测试固定该锁定状态：`application/workflowConfiguration.test.ts:234-246`、`stores/workflowConfigurationStore.test.ts:240-259`）。
- 后端自己也不强制它：`workflow_capability.py:357-372` 注释写明 "Only on the frontend management write outbox path; backend commands are not forced through request evidence"；后端测试直接 `service.update_definition(...)` 不写证据（`tests/test_configuration_run_lock.py:52-55`）。只读回执端点 `graph_receipts.py` 全程不读证据表 → **"未知结果核实"并不需要这层证据**，只需要 key + 原请求 + `receipts/read`。

### R2 多副本仲裁（同一份定义 ~10 份前端真相 + 7 类后端记录）

前端：`entries.document`（可编辑）、`saved_document`（保存快照）、`saved_revision`、`latest_published_revision`、`session_document`（当前只删不写，死字段）、`entries.pending`（旧整图请求，生产只写 null）、`history.past/future`（整份快照 ×60）、`configurationApplication.documents` Map、`accepted()`/`current`、workspace 目录行计数器（`fixedDefinitionRevision/editSequence/state`）。后端：固定修订记录（含整篇 document + edges + bindings）、每节点 `node_binding`（config 再存一份）、`workflow_current_configuration` 覆盖行（presentation + adjustable，读时再合成）、session/chain 的 `definition_revision`、`idempotency`、`pending_commands`、`node_definition`。逐项位置见附录 A。
- 一次 `connect` 净变更只有 1 条边，但需要同时正确维护约 **30 个状态槽位**，其中约一半只为"请求在途/证据/重放"存在（附录 A 表）。
- 产物：接纳状态机 `accepted/duplicate/stale/conflict/fixed/invalid`（`workflowConfiguration.ts:39-94`）、投影函数 `projectAcceptedState/projectConfigurationResult/projectChangedAcceptance`（`workflowGraph.ts:388-460`）、在途门锁链 `configurationSubmitting → activeConfigurationLocked → mutationLocked → locked → editorLocked`（`workflowGraph.ts:149-188`）。

### R3 整份文档 PUT + 客户端重实现服务端语义

- 每次编辑上传/下载整篇文档（开发库单篇约 12.7 KB），后端整篇校验并在 fixed 分类下重写修订 + 全部 node_binding（`configuration_data.py:83-118`）；`display` 分类也重写**全部节点**的 presentation（`workflow_configuration.py:326-339`）。
- 客户端把服务端保存语义再实现一遍：`domain/workflowConfiguration.ts:70-119`（约 50 行，逐条校验 outcome/`revision`/`edit_sequence` 算术/`node_id_map`/派生与确认规则）；`domain/workflowGraph.ts` 的 `isGraphDocument` 与后端 `validate_graph_document` 两套规则（前端在 schema 3 下**不做**同步校验：`application/workflowEditing.ts:18` 只对 schema 2 校验）。
- 两边已有不一致：前端 `session_preloads` 校验弱于后端（前端 `workflowGraph.ts` 内 `isGraphDocument` 只查同 key 绑定/无 default/registrar 为空；后端还要类型登记准入、变量已注册、禁特殊类型 `session_preloads.py:64-156`）→ 前端放行、后端拒绝，只表现成异步通用错误（`workflowGraph.ts:780-783` report）。

### R4 双版本号 + 分类 + 派生确认

存在理由：编辑可能发生在"已有会话/正在运行"时，为了让改标题/改可调提示词**不打断会话**，把文档拆成固定修订 + 当前覆盖层，于是有了 `revision` 与 `edit_sequence` 双号、`display/adjustable/fixed` 分类（`workflow_configuration.py:244-265`）、固定部分变更时的 `confirmation_required`/`derived`（`:429-449`）与前端 `window.confirm` 二次提交（`workflowGraph.ts:461-471`；`workflowConfiguration.ts:279-289`）。
- 现实分布：开发库 103 次保存里 59 次只改 display/adjustable；**0 条 session**，所以派生/确认分支在该库从未执行 [数据]。
- 这是"编辑期与运行期解耦"的产品机制，不是纯异步产物；但它的成本（每次 display 编辑也全量重写 + 全量校验 + 2×历史扫描）与"是否逐次保存"强相关。

### R5 在途门锁与静默丢弃（体验负收益）

`configurationSubmitting` 在第一个 await 前同步置位（`workflowGraph.ts:487`），编入 `locked`（`:149-163`），`canEdit` 再检查它（`:774`）→ 保存的整个 5 请求窗口内，后续编辑**静默 return null**，无提示、无排队（`workflowEditing.ts:15`；`workflowGraph.ts:1163-1166`）。异步设计没有换来并发，只换来"点一下等一长串、再点一下被吞"。

### R6 历史遗留叠加

`saveWorkflow` 已退役（`workflowManagement.ts:149-151`）、`workbenchPersistence.save` blocked、`localDraftsApi.saveLocalDraft` 抛已退役（`workbenchPersistence.ts:13,79-92`；`adapters/localDraftsApi.ts:91-95`）、`session_document` 只删不写、`entries.pending` 旧恢复路径、`upgradeGraph` 只升到 schema 2 而生产只收 schema 3。这些让 store（1,754 行）同时承载编辑、管理、复制、配置、会话、运行六套事务，读起来远比必要复杂。

---

## 4. 成因链（为什么会滚成这样）

1. 需求：画布要"改一下就存"，不需要用户点保存。
2. 于是每次微编辑变成一个服务端持久化命令（`definition.update`，整份文档）。
3. 浏览器无法判定"命令是否已生效"，于是引入"写前落盘原请求 + 只读核实回执 + 不重发"的可靠性协议。
4. 因为命令要幂等/可追溯，又叠加 `idempotency_key`、`application command identity`、回执摘要校验、接纳仲裁。
5. 因为编辑可能发生在有会话/运行时，文档被拆成固定修订 + 当前覆盖层，引入双版本号与分类；固定部分变更升级为"派生新工作流"两阶段确认。
6. 因为前端要保持可编辑副本，就需要"本地文档 vs 已接纳文档"的仲裁与投影，再叠加历史、选中、workspace 目录投影。
7. 每一层都合理，叠起来的结果是：**一次 1 条边的编辑 = 5 个请求 / 约 6 行 28 KB 数据 / 约 30 个状态槽位 / 两次全历史读取**，而用户看到的仍是"等回执才显示"。

---

## 5. 卸除后会不会优化"整个运行"

### 5.1 直接执行路径：几乎没有收益

- 运行请求**不带前端文档**：只有 `expected_revision` + `inputs` + key（`graph_application_contracts.py:66`；`application/workflowManagement.ts:92-94`）。
- 运行准备只读 **1 篇固定修订**（`graph_service.py:905-906` → `configuration_data.py:49-56`）+ 1 行当前配置（`:907-913`）→ 编译一次；执行阶段再读同一篇修订、再编译一次（`:1435-1472`）。证据表/幂等表**不在运行读路径上**，`pending_commands`/`idempotency` 不出现在 `_prepare_run`/`_execute`/`_settle`。
- 所以：删掉编辑期的 5 请求/证据层，**不会让执行循环变快**。

### 5.2 但对"运行入口"和"编辑→运行"有真实收益

1. **运行命令本身也走证据协议**：`run.start` / `run.control` / `event.submit` / `session.create` / `session.archive|delete` / `definition.archive|delete` 全在管理原请求通道里（`application/workflowManagementRequests.ts:26-28`），`dispatch` → `outbox.send` → 证据写 + 2 次确认读 + 业务 + 结案（`resourceOutbox.ts:74-151`；`workflowManagementRequests.ts:86-140`）≈ **6 次证据调用**；首次运行还要先自动 `createSession`（`workflowManagement.ts:86`），也就是两轮 ≈ 12+ 次额外 HTTP。若去掉证据层，run.start 回到 1 个业务请求 + 1 次会话读。
   - 注意 [数据]：开发库 `pending_commands` 里**没有** `run.start` 记录（只有 2 条 `graph.run.start` 幂等行），说明该库的两次运行不是从工作台这条通道发起的（可能来自消费端/聊天路径）。因此"运行命令被证据化"是有代码证据的**潜在**成本，尚未在本库实测到。
2. **写锁争用**：保存全程持单写者 SQLite（`graph_store.py:131-169`）+ 服务级 `RLock`（`graph_service.py:455`），并做两次全历史校验；`run.start` 的准备事务（`:1009-1023`）与运行期节点事件（`:1479` 起）要抢同一把锁。保存越重越久，运行入口排队越久；`storage.py:47 timeout=30` 决定最坏等待。证据层每次保存多 4 个写事务，是这条队列的主要加重项。
3. **库容/目录读取**：证据+幂等已占 67% 库容且永不清理；目录类读取本来就要遍历全部修订（`configuration_directory`/`list_configuration_definitions` 走 `all_definition_revisions()`，`configuration_service.py:57-79`），库越大越慢。属于编辑期成本，但最终反噬运行前的目录/会话查询。
4. **O(n²) 会惩罚 agent 驱动**：人手动编辑一天几十次问题不明显；代码 agent 若每次改动都触发一次编辑，证据历史读取成本按编辑次数线性上涨、累计平方上涨，同时锁窗口变长——这正好是"转为 agent 编辑"后最不该保留的结构。

### 5.3 运行正确性真正依赖什么（删证据层不动这些）

| 依赖 | 位置 | 必要性 |
|---|---|---|
| 会话创建绑定**当前**固定修订 | `graph_service.py:677-687` | 必需（后端契约） |
| 运行只读会话绑定的固定修订 + 条件合成当前配置 | `graph_service.py:905-913` | 必需 |
| G21：任一会话有未结束运行 → 拒绝一切配置保存 | `workflow_configuration.py:374-388,406` | 必需（运行期冻结的保证） |
| prepare 把节点 config 冻结进 `node_run`，执行复用 | `graph_service.py:939-949,1441-1445` | 必需 |
| `expected_revision` CAS + `_stable` 会话校验 | `graph_service.py:861-889`；`graph_application_contracts.py:66` | 必需 |
| 运行 basis 冻结（registry / definition catalog） | `graph_service.py:404-416,938,1421` | 必需 |
| 前端 `locked`/`saved_document`/`saved_revision` 门 | `workflowGraph.ts:1569-1571`；`workflowManagement.ts:72-91` | **非必需**，是"运行当前编辑"的 UX 守卫 |

**额外发现（可顺手修）**：业务保存已成功、仅证据结案失败时，配置通道 `locked` 会一直挡住运行（`workflowConfiguration.ts:132,199-213`；`workflowGraph.ts:149-154`）。这是纯损失——数据已落库，却因为一条审计记录不能运行。

> 结论：**卸除/精简这层不是"拿运行性能换编辑体验"，而是"去掉一个既不提供即时反馈、又拖慢运行入口和编辑串行度、还自带永久锁风险的中介层"。** 收益集中在运行入口延迟、锁争用、库容与编辑吞吐；执行循环本身不变。

---

## 6. 三个卸除/精简方案与代价

> 以下为方案与估算，不是已批准的实施计划；涉及产品语义（是否保留原请求证据政策、是否仍支持多标签并发）需先决策。

### 方案 A（低风险，推荐先做）：只摘"画布保存 + 管理命令"的证据链

- 做法：保存改为**一次阻塞 `definition.update`**（自带 `idempotency_key`）；`unknown` 时用已有**只读** `receipts/read` 核实（`graph_application.py` `receipt.read`、`graph_receipts.py:58-106`，不依赖证据表）；需要"写前留底"就放**客户端单槽**（key + body，localStorage/IndexedDB），不再写服务端 `pending_commands`。
- 删除/简化：前端约 **250–320 行**（`workflowConfiguration.ts` 的证据编排/结案/`reconcile` 约 130–150 行；`workflowGraph.ts` 的 `reconcileConfiguration`/`configurationPendingRequests`/`projectChangedAcceptance`/遗留发现约 120 行；`workflowConfigurationApi.readReceipt` 等约 20 行）；后端仅收窄 workflow-configuration / workflow-management 两类 owner 的证据分支（约 300 行量级），**不动** `graph_receipts`、其它 owner、表结构。测试需重写/缩水约 6 个前端文件、6 个后端文件。
- 收益：每次编辑 **5 → 1** 请求；每次保存新增 **6 行/28 KB → 1–2 行**；消除两次全历史读取（O(n²) 消失）；消除永久锁；`run.start` 回到 1 个业务请求。运行正确性不变。
- 风险：失去"刷新后自动核实并显示已保存"（退化为"报未知 + 刷新即真相"，数据不丢，因为后端有 CAS 与回执）；失去跨标签互斥的**预检**（退化为提交后被 `stale_edit_sequence` 拒绝）；与 `docs/DEVELOPMENT.md:155,175` 的"保留原请求证据"政策冲突，需要政策决策。

### 方案 B（中风险）：A + 合并重复状态与重复校验

- 合并 `saved_document`/`saved_revision` 与 accepted 副本（配置模式下两者恒等，`workflowGraph.ts:415-417`）；删死字段 `session_document`、旧 `entries.pending` 路径；回执校验 4→1、请求校验 2→1；删 schema 1/2 升级分支；保存前历史校验改为一次 `MAX(revision)` SQL（去掉 `workflow_configuration.py:410` 与 `configuration_data.py:98` 的两次全表扫描，约 49 ms/次下界）。
- 估算再省前端约 **120–200 行**、后端约 **40–80 行**；保存路径 CPU 显著下降（不动契约，可独立先做）。
- 风险：调用点很多（管理/事件/前端节点/变量登记都用 `saved_revision`），需逐项回归；旧 schema 支持涉及历史数据与旧测试。

### 方案 C（彻底，需产品决策）：编辑改为"一次性提交/agent 写接口"

- 面向"编辑逐渐交给代码 agent/全能 agent"：画布不再逐编辑自动保存，改为显式一次提交（或 agent 直接调一个 replace 命令）；取消每次编辑的保存、取消 undo 重发整图、取消 display 覆盖层的逐次落库（或并入显式提交）。
- 可去：in-flight 锁链、history 重放保存、`edit_sequence` 如不再需要逐次 CAS 可与 `revision` 合并、`display` 分类的大部分（取决于是否还要"运行中改标题"）。
- 收益最大：编辑路径可缩到"构造文档 → 提交 → 重载"三步；store 六套事务可拆。
- 风险最高：改变交互语义（用户是否还能边看边改、是否有未保存态、多端并发如何处理），且需重写大量测试。**必须由用户先确认产品方向。**

---

## 7. 需要用户决策的点

1. **"卸除异步"的目标**：(a) 删证据/恢复层、保留单次阻塞保存；(b) 保留异步但恢复真正的乐观本地应用（复杂度反而上升）；(c) 走方案 C 改为 agent 一次性提交。三者取舍完全不同。
2. **"原请求证据"是否仍是政策要求**（`docs/DEVELOPMENT.md:155,175`、`docs/QUICKSTART.md:74` 明确写了保留）。若不是，方案 A 收益立竿见影；若是，只能做"客户端单槽 + 只读回执核实"的等价替代。
3. **是否仍需要多标签页/多客户端并发编辑同一工作流**：决定是否保留服务端 pending 唯一性（否则由 `stale_edit_sequence` 兜底即可）。
4. **"运行中改标题/可调提示词"是否要保留**：决定 `display/adjustable/fixed` 分类与当前覆盖层能否合并（方案 C 的前提）。
5. **是否还要支持 schema 1/2 历史定义**：决定能否删升级分支与旧校验。

---

## 8. 可独立先做的低风险项（不改契约）

1. 保存前两次全历史扫描改为 `MAX(revision)` 查询（`workflow_configuration.py:410`、`configuration_data.py:98`）——纯性能，不动语义。
2. 证据读取加 `resolution='pending'` 过滤或分页（`request_evidence.py:203-209`）：当前"全历史"返回在协议不变的前提下也是浪费；但确认读需要历史记录做一致性校验，改动要同步前端（`requestEvidenceApi.ts:101-121`）。
3. 业务已保存、证据结案失败时**不应继续锁运行**（`workflowConfiguration.ts:132,199-213`；`workflowGraph.ts:149-154`）：至少把运行门与"证据完整性"解耦。
4. 保存期间的第二次编辑至少给出提示/队列，而不是静默 `null`（`workflowEditing.ts:15`；`workflowGraph.ts:1163-1166`）。
5. 每次保存开 8 个 `BEGIN IMMEDIATE`（`storage.py:44-105` 每请求都跑全量 DDL + 版本检查）与编辑协议无关，但同一个保存里叠 5 次，值得单独收拾。

---

## 9. 证据索引

- 保存链路：`frontend/src/components/GraphWorkbench.vue:181-189,221-223`；`stores/workflowGraph.ts:472-532,533-588,769-793,1163-1166,1209-1276`；`application/workflowEditing.ts:13-26`；`application/workflowConfiguration.ts:112-123,141-186,187-215,216-272,290-303`；`adapters/workflowConfigurationApi.ts:7-19`；`adapters/requestEvidenceApi.ts:96-152`；`adapters/workflowApplicationApi.ts:56-86`；`domain/workflowConfiguration.ts:44-119`。
- 后端保存：`graph_application.py:58-96,105-123`；`configuration_service.py:20-34`；`workflow_configuration.py:286-339,390-469`；`configuration_data.py:24-31,49-56,83-121`；`graph_store.py:60-72,84-123,131-169`；`graph_records.py:164-172,225-254`；`graph_application_identity.py:110-207`；`request_evidence.py:45-84,186-295`；`graph_receipts.py:58-106`；`storage.py:44-105`。
- 运行：`application/workflowManagement.ts:61-95`；`stores/workflowGraph.ts:735-758,1481-1522,1565-1588`；`application/workflowManagementRequests.ts:26-28,86-167`；`application/resourceOutbox.ts:74-151`；`graph_service.py:404-416,677-687,891-983,1005-1029,1407-1506`；`graph_application_contracts.py:62-66`；`graph_input_declarations.py:80-96`；`checkpoint_repository.py:96-101`。
- 生产装配前提：`frontend/src/App.vue:10`；`composables/useWorkflowFrontendHost.ts:22-23,41-46`。
- 锁定/死锁证据：`application/workflowConfiguration.test.ts:234-246`；`stores/workflowConfigurationStore.test.ts:195-211,240-259`；`stores/workflowGraph.ts:709-733`；`application/resourceOutbox.ts:157-182`。
- 数据库取证（只读，`.local/develop/workflow.sqlite`）：`pending_commands` 197 行全 verified；`idempotency` 1,200 行/3.94 MB；44 修订/`edit_sequence=103`/0 session；evidence 读写各 197、update 177、command-identity 563。
- 附录：[附录-分区证据.md](附录-分区证据.md)
- 结论与可信上下文（置信度、测量环境快照、未确认边界）：[00-结论与可信上下文.md](00-结论与可信上下文.md)
