# REPLACE-03/05 旧 Agent 历史校验解耦与 mock 原型退役

承接 [聊天入口分派](REPLACE-CHAT-ENTRY.md)、[首轮处置表](REPLACE-AUDIT-2026-10-07.md)及 [1.0 规划](PLAN-1.0.md)。本轮三个并发子代理分别负责后端纯校验迁移、独立历史回归、前端原型退役；主代理负责范围裁决、代码复核、合并验证及文档记录。

HEAD 仍为 `00970f75fb52d741b694499a05f702600529fcdd`，记录对应其上的未提交工作区，固定 [2026-10-06 基线](BASELINE-2026-10-06.md)不修改。本轮只推进 REPLACE-03 的历史读取依赖和 REPLACE-05 的孤立原型子项，REPLACE-01/02 仍在进行，旧宿主与正式兼容客户端尚未退出，1.0 未完成。

## 1. 范围裁决

| 范围 | 本轮处置 | 保留边界 |
| --- | --- | --- |
| 旧 `node_run.agent` 历史校验 | 从旧 Agent 执行器迁入纯契约模块，旧运行路径保留同名导出 | 不改历史 JSON、身份、schema、事实、限额、异常或数据库；历史可读不等于活动执行可恢复 |
| 最早前端 mock 原型岛 | 核实没有正式入口或动态加载调用者后退役 | 不是当前普通图或固定 A/B 工作台，不以此宣称正式兼容客户端已退出 |
| 共用存储接口 | 原样迁出 `DraftStorage` 的 `getItem/setItem` 两方法 | 当前持久化逻辑、CAS、原请求和档案格式不改 |
| 原型浏览器键 | `workflow-workbench:local-draft:v1` 原样保留 | 当前初始化不读取、删除、转换或覆盖其数据 |
| 通用对象节点声明 | 本轮不迁包、不改端口 | `workflow.compat@1.0.0` 的确切目录、包锁及 `JSON@1` inline 协议保持原样 |
| 共用 Agent 内核、prepared-context 链、旧宿主和正式客户端 | 本轮不退役 | 默认新包仍使用共用内核；其共享旧导入链和显式兼容调用者继续待处置 |

通用对象节点已经复用 SessionObjectStore 的权限、类型、CAS、幂等回执及事务接纳，不是另一套旧变量数据权威。将声明迁包会涉及同 ID 重复注册和原精确兼容包目录；不能保留旧 `@1` 身份却改用当前内容 schema2，也不能批量替换已保存定义和包锁。这个裁决留在 REPLACE-01，不增加本期能力。

## 2. 实现范围

后端产品文件：

- 新增 [graph_agent_contracts.py](../backend/src/phase1_agent/graph_agent_contracts.py)，承载常量、`GraphAgentIdentity`、accepted/facts/limits/capacity 和 UUID 的纯校验。
- [graph_agent_runtime.py](../backend/src/phase1_agent/graph_agent_runtime.py) 保留同名导入与原运行调用者，回调、快照构造、适配器、workspace 和执行均留在原处。
- [graph_records.py](../backend/src/phase1_agent/graph_records.py) 的 Agent 历史校验改从纯模块导入，不借执行器读取冻结证据。

保留 component UUID `7be319b8-30bd-4674-b7bf-d1cf54a1a101`、`graph-2`、输出 schema、所有归属和事实因果检查、请求容量检查及 detached 返回。扩额后的 limits 仍可大于快照中的原始额度，不新增两者相等的限制。

前端完整退役清单，共 14 个源码文件：

```text
components/EditorCanvas.vue
components/WorkflowNode.vue
components/InspectorPanel.vue
components/RunView.vue
components/HistoryView.vue
stores/editor.ts
stores/runtime.ts
adapters/mock.ts
adapters/ports.ts
adapters/localDraft.ts
fixtures/catalog.ts
fixtures/runs.ts
domain/draft.ts
domain/graph.ts
```

路径以 `frontend/src/` 为前缀。三份专属测试 `stores/stores.test.ts`、`domain/graph.test.ts`、`adapters/localDraft.test.ts` 共 20 个旧案例同步退役，**不计作当前回归通过数**。

新增 [browserStorage.ts](../frontend/src/adapters/browserStorage.ts) 原样承载 `DraftStorage`；当前 adapter/store 两处仅更换 type import。新增初始化回归验证原型旧键存在时，新建及重开只访问当前工作区键。

没有改动 `main.ts`、CSS、依赖或锁文件。`.icon-button` 仍被正式会话变量面板使用；minimap 样式及依赖另行裁决。当前 `workbenchRuntime`、`workflowGraph`、准备流程、历史兼容、模型/内容入口和原请求保护均保留。

## 3. 定向验证

主代理确认迁出的 7 段定义及 2 个常量与 HEAD 的 AST 完全相同；九个旧路径符号与纯模块仍为同一对象。当前源码中没有原型岛的剩余调用者或动态加载入口，旧键只出现于新增隔离回归。

后端在 `backend/` 执行两个互不重叠的定向范围：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_graph_agent_runtime.py tests/test_graph_agent_service.py tests/test_graph_prompt.py tests/test_graph_package_selection.py tests/test_execution_facts.py tests/test_contract_graph.py tests/test_contracts_v2.py tests/test_serial_agent_demo_integration.py tests/test_capability_packages.py::test_compatibility_package_keeps_existing_node_declarations_and_versioned_global_types -q --basetemp ..\.local\pytest-history-retirement-6c2b43e1
..\.venv\Scripts\python.exe -m pytest tests/test_graph_agent_contracts.py -q --basetemp ..\.local\pytest-history-contracts-final-59345dce
```

- 既有范围 **9 个文件，304 passed，122.53 秒，退出码 0**。兼容包测试仅选上述一项，不是整个文件或全量；覆盖旧运行/暂停/扩额/归档重试、包选择与缺包只读、共用契约和当前串行图。
- 新增 [test_graph_agent_contracts.py](../backend/tests/test_graph_agent_contracts.py) **95 passed，5.24 秒，退出码 0**。纯夹具基于已有 `success.json` 和事实结构，不借旧 runtime Harness；覆盖完整 accepted、0–14 条事实前缀、扩额不改冻结配置、schema2/3/4、归属/因果/结果篡改、容量、detached 及 frozen transition。
- 三个 fresh subprocess 在导入产品模块之前禁止旧 Agent runtime/host、adapter、prepared context、workflow 及公共 runtime 的加载；通过正式 record/bundle 校验、GraphRecordStore 事务写入、测试 SQLite 重开和原回执核实，原定义包锁及记录保持不变，回执不重放写入。

两批共 **10 个文件、399 个不重复案例**。新增回归的子代理初次 94 项及补 alias 后 95 项均通过，与主代理最终 95 项重叠不累加。服务级缺原 compat 安装的只读重开沿用本次已重跑的包选择回归；纯夹具隔离不证明显式 compat 目录加载也已摆脱旧执行器。

前端在 `frontend/` 执行：

```powershell
npm test -- src/adapters/workbenchPersistence.test.ts src/adapters/unifiedWorkbenchDocument.test.ts src/stores/workbenchInitialization.test.ts src/stores/workbenchPersistence.test.ts src/stores/unifiedWorkbench.test.ts src/stores/workflowGraphRegressions.test.ts src/components/ChatInterfaceHandoff.test.ts src/components/GraphV2Wiring.test.ts
npm run build
```

主代理最终 **8 个文件，60 passed，3.75 秒，退出码 0**；全 src 类型检查与构建通过，Vite 阶段 1.47 秒。既有大于 500 kB 单 chunk 提示保留，不启动 LATER-09。

保留首轮过程：8 个文件，58 passed / 2 failed，4.65 秒。两项失败来自新增旧键回归把初始化的存储读取误断言为一次；现行首次保存包含 CAS 再读，两次都访问当前键。只修正测试预期，不为测试改产品行为。修正后子代理 60 项通过，与主代理最终范围重叠不累加；退役的 20 个原型案例不计为通过或能力验收。

未执行全量、浏览器操作、真实模型请求、用户库或损坏数据库检查、常驻服务变更及完整基础使用联合验收。全部新存储证据来自隔离测试库，不外推真实用户混合库或活动内核恢复。

## 4. 剩余工作

- 旧 `GraphAgentHost`、图内私有 Agent、正式旧工作台/聊天及专用 store 仍有调用者，尚未退役。
- 当前默认共享 `agent_executor -> runtime -> prepared_context` 导入链没有因纯历史校验迁出而消失。
- 旧普通图版本、通用对象声明、资源修订与已迁移档案的最终支持范围仍需逐项裁决；不自动转换、清空或重发。
- generic/旧 runtime 的 pending 核实生命周期、缺包活动链及常驻/浏览器联合验收仍待 1.0 安全收口。
- DEMO-05/06、COND、LATER 继续后置；节点打组及历史展示正文编辑/删除继续暂停。

本轮未发起真实模型请求，未变更用户数据库、重启服务、开展全面故障专项、提交或推送。既有预览保留。没有正式页面排布修改，不以此前截图冒充本轮新的浏览器验收。

固定基线 SHA256 仍为 `0690253BEC23A2E377245E6F570B2814C3320CEA2C384A4DE927534678C6FCC4`。主要产品源码 SHA256：

```text
8E7AE53FEDD96D462115E8E7E2459FD4F140FEDA761F7216BC5BF97AD4C472A6  backend/src/phase1_agent/graph_agent_contracts.py
F4E0CA7EAA93E8951C46D7C9E7755B514710E8854665AED0DEE075AB99D3512C  backend/src/phase1_agent/graph_agent_runtime.py
205E3BF59EEF868BF9CC41265B2F50690FC607BDF2E61DF0249998ABC946C823  backend/src/phase1_agent/graph_records.py
4EA654E6D90934C57416B2C0AA02A84F0322C2E0D68B96398D97B6E2B2AA6825  frontend/src/adapters/browserStorage.ts
FF767021F29B76B4A60F99D41D15EB088977D35955A8959E7CBC424361D6FA4F  frontend/src/adapters/workbenchPersistence.ts
81260056053858F6B849780F18BEBD2FE1EEF4462C24D498227A8478A48F8C91  frontend/src/stores/workbenchPersistence.ts
```

本轮六份文档的 64 个本地行内 Markdown 链接、结尾换行、上述六个源码校验值及固定基线检查通过。`git diff --check` 通过，仅保留已有换行格式提示。
