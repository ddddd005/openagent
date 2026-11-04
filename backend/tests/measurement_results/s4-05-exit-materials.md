# S4-05 第 4 步整合复测与出口材料（2026-11-04）

> 任务：task-27（S4-05 整合复测与出口材料），负责人 protocol-analyst。
> 结论：**第 4 步行为复核通过、同口径复测完成、前后端回归全绿；无清理/优化引入的回归。** 清理只影响实现内部，不改变已批准语义。
> 复测结果供 S4-06（verifier V4）与第 5 步出口使用；所有数字为应用层口径（不含 HTTP/RTT/真实前端等待），原始数据见同目录 JSON。

## 0. 基线与快照

| 项 | 值 |
| --- | --- |
| 分支 / HEAD | `develop` / `8bd27176c6b1340782b1ae8c815699ccdb6eff97` |
| 工作树 | 脏树、**未提交**（第 1–5 步改动 + verifier 并行写入；本任务只新增测量/报告文件，未改业务实现） |
| 隔离库 | 每次测量使用新建空临时目录（`--workdir`），不接触产品库 |
| 授权边界 | 未启停服务、未装依赖、未调真实模型、未提交 |

代码快照（sha256 前 16，结果 JSON 的 `environment.code_snapshot_sha256_16` 已落盘；基线来自 `s4-00-baseline-2026-11-04-run2.json`）：

| 文件 | S4-00 基线 | S4-05 复测 |
| --- | --- | --- |
| `backend/src/phase1_agent/graph_contracts.py` | ea867de7f4be0f6b | ea867de7f4be0f6b（未变，契约 v2 不变） |
| `backend/src/phase1_agent/graph_service.py` | e1071366e56448bb | e1071366e56448bb（未变） |
| `backend/src/phase1_agent/graph_application.py` | 02df83de738c5b43 | 02df83de738c5b43（未变） |
| `backend/src/phase1_agent/workflow_modification_gate.py` | ef1ce8b564526e2c | ef1ce8b564526e2c（未变） |
| `backend/src/phase1_agent/workflow_configuration.py` | 3fad3c909c491d3b | 02dc2a8e1004792f（S4-02 调用者 `history_validated`） |
| `backend/src/phase1_agent/configuration_data.py` | b541ac25749c4388 | 5b53a934d67a68e8（S4-02 `history_validated` 分支） |
| `backend/src/phase1_agent/graph_store.py` | 843aa06d18040a53 | 937b765912e6f92c（S4-02 `max_definition_revision`） |
| `backend/src/phase1_agent/request_evidence.py` | 687371594f8691c2 | d5aafd89befb39c2（S4-01 窄读） |
| `backend/src/phase1_agent/graph_application_contracts.py` | 9eca013169599ac7 | 9111a4d905c542a7（S4-01 窄读 + owner 摘要） |
| `frontend/src/stores/workflowGraph.ts` | e156239f3a641e67 | 6675b14005106103（S4-03 缓存/摘要 + S5-02 冲突态） |
| `frontend/src/application/workflowConfiguration.ts` | 493ba30637669978 | bdeff3bd17d42660（S4-01 分层初始化） |

第 4 步新增/修改的其余文件（详见 `06-验收与执行记录.md §16.2–16.6`）：`graph_drafts.py`（S4-05a 删 `LocalDraftStore.save`/`validate_draft_acceptance`）、`context_host_service.py`/`context_package.py`（S4-05a 退役桩）、前端 `GraphHistory.vue`/`WorkflowWorkspace.vue`/`GraphRunControls.vue`/`workflowGraphApi.ts`/`workflowConfigurationCopy.ts`（S4-03/S4-05b）、`test_frontend_node_snapshots.py`/`frontend_node_test_support.py`（S4-05c）。测试文件快照：`test_history_second_pass_max.py` d06981d1aeb8b183、`test_evidence_narrow_read.py` 887d08c3d6d831f6、`test_context_adopt_retirement.py` 658626b24e83f5d3、`test_graph_drafts.py` 6d9559b443a6260a、`test_frontend_node_snapshots.py` 119307b6fa311465。

## 1. 清理后 Q07/Q08/Q09 行为复核

复核方式 = 读当前实现（file:line）+ 运行对应测试 + 同口径测量；三类证据均通过。

### 1.1 Q07 证据协议窄查询 + 分层初始化（S4-01，未被清理破坏）

| 检查项 | 结论与证据 |
| --- | --- |
| 窄读契约保留 | `request-evidence.read` 仍有可选 `operation, idempotency_key, resolution`（`graph_application_contracts.py:139-140`；`describe()` 实测 optional 字段为 `idempotency_key/operation/resolution`）；实现 `request_evidence.py:229`（`_read`）、`:280`（`read_evidence`）。`request-evidence.pending` owner 过滤仍在（`graph_application_contracts.py:137-138`，`request_evidence.py:38-110`） |
| 分层初始化 | `workflowConfiguration.ts:156-196` 先 `readPendingEvidence`（`requestEvidenceApi.ts:152-185`）：无 pending 直接完成、不读历史；有 pending 才 `readPoint` 取完整正文并校验 identity/digest/resolution 后恢复 |
| 五步确认链保留 | 保存仍为 write→确认读→definition.update→确认读→resolve；测量 `chain` 场景 **5 调用、up/down 字节与基线逐字节相同**（§2.1）；前端测试 "keeps the five-step save chain with point reads for both confirm steps" 通过 |
| 未知不重放 | 未知业务结果仍只读 `receipt.read` 核实；前端测试 "never replays a lost business response and keeps unknown verification on the receipt route" 通过；fault 场景 `unknown-result receipt.read verification` 状态 matched（只读，无业务重试） |
| 摘要异常不当无 pending | 摘要字段/唯一性异常显式 500 `storage_contract_violation`；后端 `test_evidence_narrow_read.py` 7 passed（含损坏摘要、唯一性、点查跨 owner 409、参数校验）；前端测试 "treats an untrustworthy summary as an explicit failure, never as no pending" 通过 |
| 全量读保留 | 不带窄参数的 `request-evidence.read` 全量路径保留（测试覆盖"全量读取仍可用且窄读零写入"）；前端旧端口自动退回全量语义（`requestEvidenceApi.ts:142-150` 能力选择） |

### 1.2 Q08 修订历史第二遍 MAX（S4-02）

| 检查项 | 结论与证据 |
| --- | --- |
| 第二遍 MAX | `graph_store.py:86 max_definition_revision`：SQL `MAX(json_extract(payload,'$.revision'))`，过滤 `record_type + json_valid + execution_model='graph' + workflow_definition_id`，精确复刻原 `rows()` 跳过非法 JSON/非 graph 负载的统计范围；空历史返回 0，坏 revision 报 `storage_contract_violation`。`configuration_data.py:83-112`：`history_validated=True` 走 MAX，否则原全量扫描 |
| 第一遍不动 | `workflow_configuration.py:439 definition_revisions(workflow_definition_id)` 全量读取+逐篇校验仍在每次保存最前；测量 display-only/unchanged 的 `definition_revisions_calls` 前后均为 1（§2.3） |
| 坏记录行为不变 | 测量 `q08` 场景四类损坏：`invalid_json`/`valid_json_not_graph` 仍被第一遍跳过并正常保存（同 revision/outcome/写入）；`graph_record_bad_fields` 仍 500 `storage_contract_violation`、零写入；`graph_record_bad_document` 仍 400 `graph_invalid_document`、零写入；错误消息/错误码/回滚前后一致（fixed 案例仅"第二遍扫描调用"由 2→1，属预期优化，不是坏记录行为变化） |
| 不满足前提的调用者保留原实现 | `graph_service.py:566` legacy `_save_definition` 适配器不传 `history_validated`（默认 False，保留原全量扫描）；derived 新目标 `workflow_configuration.py:493` 默认 False；新 workflow `:460` 与 copy 目标 `:600` 传 True，但同事务内已先有第一遍校验（`:439` 读本 identity 空历史；copy 的 `:572 definition_revisions(target)` 读/校验目标空历史） |
| 回归/专项 | `test_history_second_pass_max.py` 10 passed；测量见 §2.3（fixed：扫描 2→1 次、校验 200→100 行、save 43.37→24.48ms @100 修订） |

### 1.3 Q09 前端冻结定义复用 + 摘要读取（S4-03）

| 检查项 | 结论与证据 |
| --- | --- |
| 按身份复用 | `workflowGraph.ts:1737-1752 readFrozenDefinition` 以 `identity@r{revision}` 为键缓存成功结果（graphClone 副本、LRU 上限 8、dispose 清理）；`GraphHistory.vue:50-64` 保留 `run.read` 触发，identity/revision 不变不清本地值、变化才重读 |
| 失败不缓存 / 显示缺失 | 成功读取后才写缓存（`:1745-1751`）；`GraphHistory.vue:65-72` 读取失败置 `definitionError`、`frozenDefinition=null`、提供显式重试，不用当前编辑图替代；`GraphHistory.test.ts` 3 例通过 |
| summary 最新值 | `workflowGraph.ts:1754-1770 refreshPublishedRevision` 每次都发 `definitionSummaries(true)`，用 `row.fixed_definition_revision` 更新；摘要行缺失抛 `unavailable`（不拿本地旧摘要作答） |
| 轮询频率与守卫不变 | `workflowGraph.ts:2291-2310` 仍 350ms、选中会话每拍 2 查询；守卫 `viewGeneration/sessionSelectionGeneration/mutationGeneration/refreshGenerations` 与 `workspace.isCurrentView(viewToken)`（`:2273-2276`、`:2299-2310`）未变；测量每拍字节与基线一致（idle 2129 / completed 14367 / running 4469） |
| 回归/指标 | `GraphHistory.test.ts`、`workflowRevisions.test.ts`、`workflowGraph.test.ts` 定向全绿；测量：一轮 4 节点 chain 推进 9 次 → 冻结定义读 **9→1** 次/轮（§2.5） |

### 1.4 Q10 清理（S4-05a/b/c，不改变行为）

- `LocalDraftStore.save`、`validate_draft_acceptance` 已删除；仅保留 `read/list`（`graph_drafts.py:189-233`；`graph_editing_host.py:41,57` 仍使用），`test_graph_drafts.py` 通过。
- `adopt_context_view` 无 live 实现，仅保留在 `RETIRED_RECEIPT_COMMANDS`（`graph_application_contracts.py:86-122`，`:103`）供历史回执解码；`test_context_adopt_retirement.py` 通过。
- 前端死路径清理（旧 pending 簇、rejected_copy/retry、no-op、3 个死 adapter）后 `vue-tsc`/全量测试/build 全绿；S4-05b 的 16 个测试文件乱码事故已由 owner 恢复，本复测全量通过（乱码语义强度属 V4 抽查项，本任务不重写测试）。
- S4-05c：`test_frontend_node_snapshots.py` 保留保存期 ACL 校验实现、测试改种子并新增正向拒绝用例；本轮聚焦批 **235 passed（含该文件 9 passed）**，此前既有 2 失败不再出现。

## 2. 前后对照（同口径应用层）

对照组：`s4-00-baseline-2026-11-04-run2.json`（22:52，`--repeat 5`，第 1–3 步代码）。
复测：`s4-05-exit-2026-11-04.json`（23:39，`--repeat 3`，第 4 步清理后代码）。
提醒：绝对耗时受机器并行负载影响（verifier 同时运行），结构性指标（调用数/字节/扫描次数/行数）是确定值，逐字节可比。

### 2.1 正常保存（无历史 owner）

| 节点 | 调用数 前→后 | 上传字节 前→后 | 下载字节 前→后 | 后端中位秒 前→后 |
| --- | --- | --- | --- | --- |
| 1 | 5→5 | 1,701→1,701 | 6,692→6,692 | 0.0335→0.0389 |
| 20 | 5→5 | 19,157→19,157 | 53,296→53,296 | 0.0528→0.0573 |
| 100 | 5→5 | 92,907→92,907 | 250,151→250,151 | 0.1328→0.1284 |

结论：清理未改变保存链结构与传输量（`chain` 场景前后逐字节相同；`validate_graph_document` 6/9/5 次不变，`replay_probe_calls`=1 不变）。S4-01 分层初始化在无历史时的新增读见下（`chain-narrow`，额外 1 次 owner pending 摘要）：

| 节点 | 调用数 | 上传字节 | 下载字节 | 后端中位秒 |
| --- | --- | --- | --- | --- |
| 1 | 6 | 1,978 | 6,782 | 0.0316 |
| 20 | 6 | 19,434 | 53,386 | 0.0587 |
| 100 | 6 | 93,184 | 250,241 | 0.1378 |

（多出的是约 90 B / 约 1 ms 的 owner 摘要查询；第 2 次保存起不再有历史读放大。）

### 2.2 owner 已有 100 条历史证据时的一次保存（读取方式对照）

同一次测量内对照两种保存流程（同一份最终代码、同一份数据）：`baseline`=初始化+两次确认都全量读 owner 历史；`narrow`=分层初始化+两次点查确认。

| 节点 | 下载字节 baseline→narrow | 后端秒 baseline→narrow | 改善 |
| --- | --- | --- | --- |
| 1 | 272,559→6,848 | 0.0699→0.0368 | 字节 39.8× / 耗时 1.9× |
| 20 | 2,937,563→53,452 | 0.3048→0.0586 | 55× / 5.2× |
| 100 | 14,196,918→250,307 | 1.2813→0.1472 | **56.7× / 8.7×** |

逐调用（100 节点）：baseline 3 次全量读各约 4.65 MB / 0.42–0.48 s；narrow 为 90 B 摘要 + 2 次 46.5 KB 点查（各约 8 ms）。保存传输/耗时**不再随该 owner 历史条数增长**，仍随当前文档大小线性（点查返回完整原请求正文，是 Q07 要求的正文核对成本）。

### 2.3 Q08 历史扫描（修订历史第二遍 MAX）

| 固定修订数 | fixed 扫描调用 前→后 | 校验历史行 前→后 | 扫描校验毫秒 前→后 | fixed 保存总毫秒 前→后 |
| --- | --- | --- | --- | --- |
| 1 | 2→1 | 2→1 | 0.33→0.14 | 10.03→10.08 |
| 10 | 2→1 | 20→10 | 3.41→1.36 | 16.03→11.61 |
| 44 | 2→1 | 88→44 | 9.89→6.25 | 22.03→18.81 |
| 100 | 2→1 | 200→100 | 23.98→11.57 | **43.37→24.48** |

display-only/unchanged 的扫描调用前后均 1 次、行数不变（第一遍保留）。历史 fixture 构建时间 100 修订 2.42→1.63s（构建本身逐次保存，随之下降）。

### 2.4 证据读取（0/50/200/600 行 owner 历史）

| owner 行数 | 旧全量读 | 新 owner 摘要（bodyless） | 新点查（1 条正文）/ pending 窄读 |
| --- | --- | --- | --- |
| 0 | 4.0–5.1 ms / 2 B | 0.9–1.2 ms / 90 B | 2 B（0 行） |
| 50 | 8.1–9.1 ms / 44.3–45.2 KB | 0.86–1.10 ms / 90 B | 0.94 KB（1 行） |
| 200 | 20.6–22.2 ms / 177.2–178.1 KB | 0.99–1.04 ms / 90 B | 0.94 KB |
| 600 | 61.8–62.5 ms / 531.6–532.5 KB | 1.1–1.4 ms / 90–375 B | 0.94 KB |

### 2.5 轮询 / Q09

| 指标 | 前 | 后 |
| --- | --- | --- |
| poll 每拍查询数/字节 | idle 2/2,129；无选中 1/257；completed 2/14,367；running 2/4,469 | 完全相同（频率/守卫未变） |
| 单次冻结定义读 | 2,264 B / 4.6 ms | 2,264 B / 4.2–4.4 ms |
| 一轮 4 节点 chain 推进 9 次 → 冻结定义读 | 9 次 | **1 次**（按 identity+revision 复用；少 8 次、约 18 KB、37–46 ms 应用层） |
| `refreshPublishedRevision` | 整份 current：2,282 B / 5.0 ms | `definition.summaries`：**224 B / 4.8 ms**（需要最新值仍实时读摘要） |

### 2.6 preview / 契约不变

- `definition.preview`：2/20/100 节点 request 1,012/9,327/46,443 B、response 1,567/2,270/5,391 B **与基线逐字节相同**；`read_only=true`、`reserves_save_right=false`、records/idempotency/pending 写入增量均 0（零写入）。
- `document.contract` v2：**47,033 字节不变**（`graph_contracts.py` 哈希与基线相同）；`describe()` 总量 15,628 B（S4-01 起窄读 optional 字段进入发现契约，属已批准契约变化）。
- config/session/fault 场景 outcome 与基线一致：initial/display/fixed saved、unchanged、invalid_request、stale_edit_sequence、workflow_modification_busy；`confirmation_required` 零写入→显式 derived；G21 409 含 occupying_sessions；五步链参考 5 调用/6,692 B 逐字节不变；fault 场景错误码/只读核实一致。

### 2.7 管理/复制通道（保留现状）

管理命令仍为 **6 证据调用 + 1 业务调用**（前后一致：7 调用、约 4.9–5.9 KB 下载）；copy 通道仍全量读 owner 证据。Q07 本批只把配置通道接入窄读，管理/copy 未切换（见 §4.3 限制）。

## 3. 回归结果（真实退出码）

| 检查 | 范围 | 结果 |
| --- | --- | --- |
| 后端聚焦批 A | 19 文件：history_second_pass_max、evidence_narrow_read、context_adopt_retirement、graph_drafts、frontend_node_snapshots、application_service_classification、legacy_management_write_retirement、configuration_evidence、graph_application(+identity/receipt_custody)、receipt_http/roundtrip、lifecycle_receipts、legacy_context_receipt_projection、provider/request_evidence 系列 | **235 passed，exit 0**（含 S4-05c 修复的 snapshots 9 passed） |
| 后端广批 B | 57 文件：configuration*/execution/lifecycle/frontend_node*/contract*/preview/document_contract/graph_service | **1273 passed, 2 skipped，exit 0** |
| 前端定向 | adapters 全目录 + workflowGraph/Configuration(Store)/Copy/Revisions + GraphHistory/WorkflowWorkspace + workflowConfiguration(应用) | **35 文件 / 679 passed，exit 0** |
| 前端全量 | `npm run test` | **138 文件 / 1661 用例 passed，exit 0** |
| 前端类型检查 | `npx vue-tsc --noEmit` | exit 0 |
| 前端构建 | `npm run build` | exit 0（dist 722.88 kB / gzip 216.30 kB，较清理前 723.83 kB 略降） |
| 测量 | `--scenario all --repeat 3` | exit 0（12 场景） |

既有失败区分：**本轮无既有失败、无本次引入失败**。第 3 步遗留的 `test_frontend_node_snapshots.py` 2 项失败已由 S4-05c 修复（保留实现 + 测试改种子 + 正向覆盖），本轮批 A 通过。2 skipped 为需要浏览器/外部环境的既有跳过项（未安装、未启动）。

## 4. 第 4 步出口材料

### 4.1 实际能力清单（Q07–Q10 精简后）

- **保存/编辑**：五步证据确认链（写证据→确认→业务→确认→结案）、修改互斥与 G21、CAS（edit_sequence/revision）、结构/引用硬约束（保存与编译双入口）、诊断 phase/path/expected/actual、只读保存预演、`document.contract` v2 发现、未知结果只读回执核实不重放。
- **证据协议**：`request-evidence.read` 窄读（点查 / owner+resolution=pending）、`request-evidence.pending` owner bodyless 摘要、全量读保留、摘要/唯一性异常显式报错、分层初始化（无 pending 不读历史、有 pending 点查正文）。
- **历史读取**：第一遍全量读取+逐篇校验保留；满足前提的调用者（primary 新/既有、copy 目标）第二遍改 SQL MAX；bad record 行为与错误码不变；不满足前提（derived 新目标、legacy `_save_definition`）保留原实现。
- **历史面板/轮询**：冻结定义按 (workflow_definition_id, definition_revision) 复用（LRU 8、失败不缓存、缺失显示+重试、run.read 仍为触发）；`refreshPublishedRevision` 摘要最新值；轮询频率/守卫不变。
- **清理后保留**：LocalDraftStore 只读能力、历史回执/证据契约解码、外部 Agent 所需查询接口与 store 导出面；退役写入口与前端死路径已删。

### 4.2 保留接口与语义（S5 前置，外部 Agent 可复用）

- **入口**：现有 HTTP `describe / command / query`（本地可信调用边界）；`document.contract` v2、节点目录/`definition.inputs`、`definition.preview`、`receipt.read`、`request-evidence.read/pending`（含窄参数）、`definition.read/summaries/list`、`session.*`、`run.read`、management 命令族。
- **语义**：幂等键 + `request_digest` + 五步证据闭环；`unknown` 只读回执核实、不自动重放；同工作流修改互斥/G21；CAS `expected_*`；`run.start` 返回≠运行成功；`close` 显式调用；`retry_*` 显式控制；读取/分析/预演不占编辑锁，写才占。
- **S5 状态**：S5-01 已确认现有接口覆盖 Q11-2 全部要求（22 命令/45 查询，无新增业务代码），S5-02 已加外部版本变化/本地冲突识别；本轮复测给出其入口与语义的第 4 步基线，最终验收由 V4/S5-04 负责。

### 4.3 仍存在限制（未消除成本与未覆盖）

1. **Q08 第一遍保留**：仍随修订历史线性（100 修订 display 约 13–17 ms、fixed 约 20–25 ms 应用层）；本批明确不优化第一遍。
2. **Q07 点查正文**：每次确认返回完整原请求 = 当前文档大小（100 节点 46.5 KB），属正文核对必要成本；保存总传输仍随文档大小线性。
3. **R1 只读回放探测**：每次新修改 1 次只读查询（1–1.7 ms，未变）。
4. **轮询频率未变**：350ms、选中会话 2 查询/拍；`event.bindings` 精简未做（Q09 保留决定）。
5. **管理/复制通道**：仍全量读 owner 证据（6 证据调用+1 业务），Q07 仅配置通道接入窄读。
6. **历史面板缓存**：LRU 上限 8（实现选择）、按视图生命周期清空；失败不缓存但下次触发才重试。
7. **窄读能力选择**：外部自建端口需同时提供 `readPending`/`readPoint` 才启用优化，否则退回全量语义。
8. **测量口径**：应用层，无 HTTP/RTT/真实前端等待/真实浏览器；浏览器类后端测试未运行（2 skipped 为既有环境跳过）；真实模型未调用。
9. **S4-05b 恢复文件**：16 个测试文件经 GBK 事故恢复，本轮全量通过；乱码残留的语义强度由 V4 独立抽查（本任务不重写测试）。

## 5. 未覆盖项

- 真实浏览器 E2E（S4-06/V4 与第 5 步验收范围）；真实 HTTP 网络往返/服务调度/前端锁窗口计时。
- management/copy 通道切窄读后的成本（本批未改）；`event.bindings` 精简（未批准执行）。
- S4-05b 恢复测试文件的逐断言语义复核（V4 抽查项）。
- 大文档/真实规模绝对耗时外推（合成小文档，仅结构性指标可外推）。

## 6. 交付物与复现

| 文件 | 说明 |
| --- | --- |
| `s4-05-exit-2026-11-04.json` | 第 4 步清理后全量测量（12 场景，`--repeat 3`，含代码快照哈希/limitations） |
| `s4-05-compare-2026-11-04.json` | 与 `s4-00-baseline-2026-11-04-run2.json` 的机读对照（save/q08/q09/evidence/preview/contract） |
| `s4-05-exit-materials.md` | 本文件（第 4 步出口材料） |
| `measurement_harness.py` | 本轮新增 `chain-narrow`/`chain-history`、evidence 窄读列、`definition.summaries` 与 Q09 复用行、代码快照扩充 |

复现（`backend/`）：

```powershell
..\.venv\Scripts\python.exe tests\measurement_harness.py --workdir <新空临时目录> `
  --scenario all --repeat 3 --json tests\measurement_results\s4-05-exit-2026-11-04.json
..\.venv\Scripts\python.exe -m pytest -q tests/test_history_second_pass_max.py tests/test_evidence_narrow_read.py `
  tests/test_context_adopt_retirement.py tests/test_graph_drafts.py tests/test_frontend_node_snapshots.py --basetemp <临时目录>
```
