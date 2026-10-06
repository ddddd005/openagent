# REPLACE-01/02 联合支持与处置矩阵

承接 [固定基线](BASELINE-2026-10-06.md)、[1.0 规划](PLAN-1.0.md)及 [历史校验与原型退役](REPLACE-HISTORY-RETIREMENT.md)。本轮按用户同意的合并方式，以三个并发子代理分别审计后端、前端、存储与测试，主代理交叉核对后合并旧节点、旧入口、旧数据的去向。

代码依据仍是 `00970f75fb52d741b694499a05f702600529fcdd` 之上的未提交工作区，而不是已发布的新版本。本页是下一步实施的支持范围与约束，**没有在本轮实现冻结、替换正式客户端或删除旧宿主**；首轮记录和固定基线不倒改。

状态：联合事项 1–3 的范围裁决及第 4 项核心覆盖盘点已完成，阶段 A 的盘点与范围裁决收口。REPLACE-01 处置矩阵完成；REPLACE-02 的实际独立路径闭环及阶段 B/C 仍待实施与验证。阶段 A 完成不等于正式旧入口已退役或 1.0 已完成。执行状态以 [BACKLOG](BACKLOG.md)为准。

## 1. 1.0 支持边界

| 范围 | 实施目标 | 当前实现状态 |
| --- | --- | --- |
| 当前独立包与符合当前 SDK 的可信扩展 | 保留精确已选择版本、声明、依赖、执行器、服务及前端扩展；完整依赖可用且不借旧宿主时执行 | 新串行及当前资源入口已有限定证据，但运行预检和共享导入仍依赖旧模块 |
| 旧固定 A/B、`workflow.compat@1.0.0` 图及旧迁移生成的 v2 图 | 整份定义和相应旧会话冻结只读；保留身份、版本、正文、锁、事实和来源；退出新执行与可用节点目录 | 正式旧客户端及执行入口仍存在，尚未落实冻结 |
| 缺包、未知声明、损坏或无法核实的记录 | 明确 blocked，保留原证据及可安全提供的只读/导出；不自动换包、降级或恢复执行 | 已有部分缺包/损坏阻断，尚无统一三态分类 |
| 新图的控制、复制、完成检查点分叉及后续聊 | 保留已有暂停/恢复/关闭和确定失败/接纳重试等当前能力及权限/CAS 边界 | 已有实现与定向证据，最终同版本交互验收仍待完成 |
| 旧专用重抽样、extend-budget、continue-workflow、pending-input continuation | 不搬入本期；旧请求、预算证据与结果仍保留，按需后置 | 尚有旧调用者；旧请求额度扩额依赖私有 Agent runtime，当前 Agent 没有协议等价扩额 |
| 旧在线迁移及兼容内容导入 | 停止接受新 `legacy.migrate` 和旧 `resource.import` 写入；保留已生成结果、provenance、资源及原回执只读核实 | 现有命令仍可调用，需先提供档案与回执读取 |
| 公共对象、存储、事务、类型、事实和算法 | 共用设施保留；按职责拆出纯声明/校验，不按旧文件名整包删除 | 已迁出旧 Agent 历史校验，其余共享依赖仍待拆分 |

冻结/阻断是记录级的能力判定，不改已存运行的 status。当前独立包中的 `agents.execute@1/2/3`、上下文多版本及既有显式摘要节点不因版本较小而冻结；保留它们也不启动 DEMO-05/06。

识别以记录家族、确切组件声明、包归属、依赖及**完整包锁**为依据，不只检查节点前缀或执行到的节点。即使全部节点为独立节点，只要原锁仍包含 `workflow.compat`，也按该旧锁保留并冻结，不静默剥除“未使用”的锁后运行。未知可信包或缺失精确版本不能一律判为旧架构，无法核实则 blocked。依据：[GraphCompiler](../backend/src/phase1_agent/graph_contracts.py)。

冻结数据只允许受原作用域、owner 和公开字段限制的读取/导出。禁止旧编辑保存、启动、恢复、重试、关闭执行、复制/分叉续聊、迁移和资源刷新；允许查看原定义、完成历史、原状态与未决请求。不用当前 head、近似会话或新默认资源替代原依据。通用 resource.save/delete 对冻结旧类型的写入及对象/数据/候选命令对冻结会话的修改同样阻断，不能靠换到公共 API 绕过冻结。

## 2. 30 个兼容节点的处置

下面列出全部 30 个确切节点身份。共同处置为保留原声明及历史，退出新执行；“对应”表示保留必要业务，不表示端口、算法、资源或状态协议可直接互换。

| 旧节点身份 | 必要业务与当前对应 | 差异及处置 |
| --- | --- | --- |
| `workflow.text@1`、`workflow.current-input@1`、`workflow.output@1` | `tools.text@1`、`tools.current-input@1`、`tools.output@1` | 旧 TEXT/PROMPT schema1 与新 schema2 不同；新输出还支持 JSON，旧连线不改名 |
| `workflow.regex@1`、`workflow.text-to-prompt@1`、`workflow.prompt-to-text@1`、`workflow.json-to-text@1` | 同名 `tools.*@1` | 内容结构、处理范围、装配投影及 JSON 转文本有差异；新图使用当前协议 |
| `workflow.prompt-item@1`、`workflow.prompt-group@1`、`workflow.prompt-source@1`、`workflow.prompt-summary@1`、`workflow.prompt-summary@2` | `prompts.item@1`、`prompts.group@1`、`prompts.source@1`、`prompts.summary@1` | 配置身份、来源、内容 schema、排序/去重及旧 summary 两版算法不同；保留旧材料 |
| `workflow.tool@1`、`workflow.tool-summary@1` | `prompts.tool@1`、`prompts.tool-summary@1` | 旧私有 runtime 目录与新注入目录不同；保留工具材料，不新增生产工具管理 |
| `workflow.global-content@1`、`workflow.global-content@2` | `prompts.global-reference@1`、`prompts.global-resolve@1` 及当前提示词管理 | 旧 v1 读旧修订，v2 引用 `workflow.global-content` current；独立提示词类型为 `workflow.prompt-resource`，不自动转换 |
| `workflow.variable-register@1`、`workflow.variable-assign@1`、`workflow.variable-replace@1` | `tools.variable-register@1`、`tools.variable-assign@1`、`tools.variable-replace-all@1`、`tools.variable-replace-selected@1`、`tools.variable-to-content@1` | 旧 program state/TEXT 输出与新授权变量对象不同；新注册/赋值无输出，赋值端口为 JSON schema2，不直接替换旧节点 |
| `workflow.session-data-read@1`、`workflow.session-data-write@1` | 旧 namespaced shared-data 节点语义没有独立默认包的协议等价节点 | 本期不重建旧 authoring 节点；保留旧定义、值、默认、修订；当前对象设施不冒充自动替代 |
| `workflow.object-read@1`、`workflow.object-write@1`、`workflow.object-delete@1` | 公共对象基础设施保留，但没有直接对应的独立默认节点 | 旧使用 JSON schema1；保留 SessionObjectStore/API、授权绑定、类型、CAS、回执、检查点与墓碑，旧节点能力按需后置 |
| `workflow.agent@1`、`workflow.model-provider@1` | 不可执行的占位声明，不承担基线串行运行 | 只保留历史识别，退出可用目录，不新增执行器 |
| `workflow.model-provider@2` | `models.source@1` 及公共模型服务 | 旧 MODEL_RESOURCE/修订供应商不同于新 MODEL_BINDING/current provider，不互换端口或资源权威 |
| `workflow.agent@2` | `agents.execute@1/2/3`；基线串行为 `@3` | 旧私有 head、额度、TEXT/output_json/context_delta 不等于新 result/context 和对象写回；退出旧执行，保留纯历史校验 |
| `workflow.context@1`、`workflow.prompt-assembly@1` | 基线 `context.output@2`、`context.assembly@3`、`context.merge@2` | 旧冻结 turn/host 与新每 Agent 对象、精确视图不同；`prompts.assembly@1` 不是完整上下文历史的等价替代 |

旧声明依据：[graph_nodes](../backend/src/phase1_agent/graph_nodes.py)、[graph_prompt_nodes](../backend/src/phase1_agent/graph_prompt_nodes.py)、[graph_agent_nodes](../backend/src/phase1_agent/graph_agent_nodes.py)、[graph_object_nodes](../backend/src/phase1_agent/graph_object_nodes.py)。独立声明依据：[tool_package](../backend/src/phase1_agent/tool_package.py)、[prompt_package](../backend/src/phase1_agent/prompt_package.py)、[agent_package](../backend/src/phase1_agent/agent_package.py)、[context_package](../backend/src/phase1_agent/context_package.py)。

### 类型与目录

`workflow.compat@1.0.0` manifest 为 schema1、host protocol1、无 dependencies、`exports={}`；注册后的实际目录含上述 30 节点及下列 3 类型，没有 executor/pause/service/frontend exports。不能把空 manifest exports 写成“没有实际导出”。依据：[capability_packages](../backend/src/phase1_agent/capability_packages.py)。

| 实际类型 | 处置 |
| --- | --- |
| content `GLOBAL_RESOURCE_REF@1` | 与独立 `workflow.content` 精确共享的身份传输类型，共用保留 |
| global `workflow.global-content@1` | 保留旧 current 数据及冻结类型校验；不改义为 `workflow.prompt-resource@1` |
| session `workflow.json-object@1` | 保留旧通用 JSON 对象声明及历史；旧 authoring 节点退出不删除对象设施 |

registry 的 TEXT/PROMPT/JSON/MODEL_RESOURCE schema1 四个基底传输类型被 compat 注册显式跳过，不计入上述三个实际类型。

## 3. 正式入口与实现去向

表内前端路径以 `frontend/src/` 为前缀，后端路径以 `backend/src/phase1_agent/` 为前缀。

| 入口/实现 | 处置 | 退出前置 |
| --- | --- | --- |
| 当前 Graph 工作台、应用命令/查询、独立 Provider/Prompt 面板与 SDK 插件 | 保留，统一为当前可执行入口 | current/frozen/blocked 分类；当前写入不改冻存行 |
| `App.vue` 正式旧会话/控制/历史/准备分支，ProviderSidebar/ContentSidebar 的旧分支 | 换成只读档案入口后退役旧活动组件 | 精确旧 session/history/resource 只读桥；不激活旧 runtime |
| `GraphNodeConfiguration.vue` 的旧资源 stores 与自动加载 | 冻结节点只呈现原配置；退出旧刷新/选择逻辑 | 按声明识别，不仅按 component ID 特判；普通图也不得隐式发旧 HTTP |
| `WorkbenchCanvas`、`PreparationWorkbench`、`WorkbenchHistory`、`WorkbenchSessions`、`WorkbenchRunControls`、`MainFlowNode`、`PreparationNode`、`ModelProviderNode`、`ProviderPanel`、`ContentLibrary`、`ExposureDialog`、`NodeObservationDialog`、`SessionVariablesPanel`、`IncrementalNodeFields` | 正式旧 UI 退役候选，不是本轮已删除清单 | 必要档案读取、纯字段/契约与当前调用者先拆出 |
| `workbenchRuntime`、`preparation`、`modelConfiguration`、`globalContent`、`exposures`、`workbenchContext`、`workbenchVariables` stores 及旧 sessions/context/variables adapters | 退出旧网络和活动执行；保留纯 archive contracts | schema1–6、pending、原身份与修订保留；共享 transport 不删除 |
| `stores/workbenchPersistence.ts`、`stores/workspace.ts`、`adapters/unifiedWorkbenchDocument.ts` | 拆当前编辑态与冻结 passthrough，不整模块删除 | 不恢复旧默认壳或旧执行 stores；CAS、损坏/容量阻断、目录身份保留 |
| 静态 `app.js` 与显式旧 session 聊天分派 | 换只读档案/未决状态展示后退役旧可执行客户端 | 旧静态 localStorage 及 opaque key 读取；旧聊天不再提交或控制 |
| `server.py` / `workflow_host.py` 旧宿主、旧 mutation 路由 | 退役活动入口；必要读取迁到数据门面 | 无启动 bootstrap、旧恢复或旧内核加载；原回执核实先到位 |
| `GraphAgentHost` / `graph_agent_runtime` | 迁出公共资源 preflight/frame 与纯历史投影后退役 | 当前 start/host_call 不再调用该宿主；迁移档案不再导入旧 workflow/prepared 实现 |
| 公共 `runtime.SnapshotKernel`、DeepSeekAdapter、工具/正则、纯契约、共享存储/事实/变量状态 | 共用保留，按职责解耦 | 不因模块名字含 workflow/prepared 而整文件删除 |
| `kernel.AgentKernel`、旧 smolagents 适配、旧探测脚本及专属测试 | 必要协议回归迁出后退役候选 | 核实 native tools 调用者和安装/测试依赖，保留第三方许可 |
| 已退役 mock 原型岛 | 保留此前记录，不重复统计工作或测试 | 14 源码及 3 专属测试已在上一切片退出，20 旧案例不计作通过 |

当前根 App 仍创建旧 stores；当前 GraphNodeConfiguration 仍会加载旧资源。仅隐藏固定工作台分支不足以退出旧 HTTP。依据：[App](../frontend/src/App.vue)、[GraphNodeConfiguration](../frontend/src/components/GraphNodeConfiguration.vue)、[workbenchPersistence store](../frontend/src/stores/workbenchPersistence.ts)。

共享前端纯契约明确保留：`clonePreparation`、`RolePlacement`、`RolePlacementFields`、`GraphSchemaFields`，以及 sessionWire/preparationProgram/model/exposure 的纯校验。旧 UI/store 退出前先按职责拆分，不因退役 IncrementalNodeFields 等旧组件连带删除当前 schema 编辑器仍使用的字段和校验。

### 旧 HTTP 家族

`S` 表示 `/api/sessions/{sid}`。这些是待退出的正式业务调用，不包括当前 outbox 中保留的 `/api/graph/...` 原请求坐标。

| 旧调用家族 | 当前覆盖/缺口及处置 |
| --- | --- |
| `/api/health`、`/api/sessions`、`S`、`/api/active-session` | 新 definition.sessions/session.read/session.create 已有；退出 health 的旧 mode/旧 host 调用链，不自动删除通用健康端点；旧全局 active-session 和旧列表初始化退出，冻结历史另读 |
| `S/inputs`、`S/runs/{rid}/{interrupt,resume,extend_budget,retry-archive,retry-publish}` | 新 run.start/consumer.run.start/run.control 提供当前控制；旧 extend_budget 没有当前 Agent 等价能力，后置；不自动改写旧请求，旧 publish 不冒称新 acceptance |
| `S/candidates/{cid}/select`、`S/chains/{cid}/reroll`、`S/branches`、`S/branches/switch` | 新候选 select/fork 已有；旧按消息角色分叉、专用 reroll/branch-switch 不宣称等价覆盖 |
| `S/chains/{cid}/{close_execution,continue_workflow}`、`S/pending-inputs/{iid}/continue` | 旧专用继续/结算退出，不给冻结活动事实新增 close/resume/retry |
| `S/data/revision`、`S/copy`、`S/variables/{read,write}`、`S/nodes/{nid}/context/{read,preview}` | 新 session.copy/session.data.update/对象与上下文路径保留；旧 shared-data、A/B prepared scope 不自动迁入 |
| `/api/prompt-configs/...`、`/api/model-configurations/...`、`/api/global-content...`、`/api/session-data/definitions` | 新提示词/provider 业务只写 current 权威；旧修订/声明精确读取另迁，旧写入退出 |
| `/api/exposure-configurations...`、`S/exposures/...`、`S/outputs/public` | 当前公开展示/可信 consumer 已有；旧 exposure、结果端口和投递证据保留作用域受限的档案读取 |
| `/api/graph` 中 `legacy.migrate`、`resource.import` | 虽在当前命令边界中，语义仍是旧目标；关闭新写入，原 outbox 与回执不删 |

调用依据：[workbenchRuntime](../frontend/src/stores/workbenchRuntime.ts)、[workbenchContext](../frontend/src/adapters/workbenchContext.ts)、[旧静态客户端](../backend/src/phase1_agent/static/app.js)、[应用操作声明](../backend/src/phase1_agent/graph_application_contracts.py)。共享 [workbenchApi](../frontend/src/adapters/workbenchApi.ts) transport/error 分类和 [workflowApplicationApi](../frontend/src/adapters/workflowApplicationApi.ts) 当前命令边界保留。

## 4. 数据与未决请求

| 数据组 | 最小保留与读取策略 | 尚需实现 |
| --- | --- | --- |
| SQLite schema0–13、records、idempotency 及新旧混合库 | 保留原行、身份和 schema 意义；不删表/行、不降版本、不重建未知版本 | 当前执行选择与历史精确选择分开；代码接受 v13 不等于已检查用户数据库 |
| 固定流程 session、Head/commit/snapshot/turn、候选、可见消息、输出与投递 | 精确读取已存事实及原消息增量，保留 ancestry 和归属校验 | 不实例化会 bootstrap/恢复写入的 WorkflowService；自定义旧组件无法纯投影时保留 raw 并明确 unsupported |
| compat 图及已迁移档案 | 保留 document/revision、完整锁、选中历史、provenance 和冻结 legacy_archives | 纯闭合 Turn/InputSnapshot/NodeInput 投影；不能读取源会话后来新增的 turn |
| 遗留未完成运行、未知工具结果、未完成投递、active ownership | 原 status、revision、manifest、facts、`active_chain_run_id` 及初始/扩额预算证据保留；仅派生 runtime-lost/read-only 诊断 | 启动恢复跳过冻结数据，不伪造完成/失败或自动解除占用；当前独立新图已有安全恢复/关闭仍保留 |
| session objects、变量状态、授权 bindings、heads、receipts、墓碑、type digests、manifests | 保留平台权威及不可变证据；缺包不把同版本类型重注册成别的含义 | 类型历史读取与当前可写 catalog 隔离，不削弱当前权限/CAS/事务 |
| 旧 prompt/model/content/exposure/shared-data 修订 | 精确 revision/head/receipt/定义读取或授权导出；停止旧路由和通用 resource.save/delete 对旧类型的新写入 | 保护已导入旧 current 正文不被删除置空；迁出资源/数据纯校验及 CORE_SESSION_NOTE，旧模块先原样 re-export |
| 浏览器 schema1–5 固定存档、schema6 混合行 | 保留 raw、原 catalog/document、compatibility、runtime、exposures、registrations 和 pending；schema1–5 的 models 及 schema6 的 modelPending/compatibility.model 按原格式保留 | graph 客户端元数据、session binding、saved_revision/saved_document、migration_request/provenance 也不丢失；当前行保存不改冻存行，不重建旧可编辑投影 |
| 原型/内容草稿/静态聊天键 | 保留 `workflow-workbench:local-draft:v1`、`workflow-workbench:content-drafts:v1`、`workflow-user-ui:v1:{sid}` schema1/2、`workflow-user-exposure:v1:{sid}` | 只读记录桥；不自动覆盖、转换或删除。静态 control/budget/branch 的内存 pending 不冒称重载可恢复 |
| 已退役操作的 pending 和 migration_request | 保留完整原 path/body/key/action、CAS/owner/目标及模型/exposure pending；原 key 可为有界 opaque string | 原请求 receipt-only 查询；缺回执、不匹配、404/410、CAS 或身份冲突均保持 unresolved，不重发 POST、不清原请求 |

数据依据：[storage](../backend/src/phase1_agent/storage.py)、[GraphRecordStore](../backend/src/phase1_agent/graph_store.py)、[旧 context view](../backend/src/phase1_agent/workflow_context_view.py)、[SessionObjectStore/manifest](../backend/src/phase1_agent/session_objects.py)、[TypeContractStore](../backend/src/phase1_agent/type_contract_store.py)、[浏览器持久化 adapter](../frontend/src/adapters/workbenchPersistence.ts)。

### 回执核实边界

新增查询仅核对既存回执的 operation、scope/target、key、原请求指纹与结果身份，不执行 mutation callback，不加载旧 host。以原请求中的 CAS 字段核对指纹，不重新套用当前 Head/revision；已验证的历史回执不因当前状态前进而失效，也不能用旧结果覆盖当前视图。表中的 CAS 冲突是核实或本机回执保存失败，无可信原回执时保留 unresolved。旧存储中的 content digest 不冒充完整 request digest；证据不足就保留 unknown。无幂等键的旧创建会话不能凭列表近似匹配销项。

当前旧 runtime 和 graph 的 replay/reconcile 实际会重新 POST；[dispatchGraphCommand](../frontend/src/application/workflowCommands.ts)及旧 mutate 在部分 rejected 分支会清 pending，[workbenchApi](../frontend/src/adapters/workbenchApi.ts)将 404/410 分类为 rejected。这不是退役后的安全核实接口。先落实 receipt-only 与冻结 UI，再关闭旧路由。

当前独立 provider/prompt outbox 保持隔离；当前 graph 已 unknown 后的核实拒绝也需定向审计，不将初次确定拒绝与后来无法核实混为一谈。初始化保留 pending 的既有测试不证明完整交互生命周期安全。

### 迁移与窄导入

现有 [legacyGraphMigration](../frontend/src/domain/legacyGraphMigration.ts)/[GraphAgentHost](../backend/src/phase1_agent/graph_agent_host.py)生成 `workflow.agent@2`、`workflow.model-provider@2`，不是迁到独立 Agent/model 包。停止新迁移，已生成结果和冻结引用照旧读取。

现有 [import_legacy_current](../backend/src/phase1_agent/global_resources.py)只显式导入一个旧 content 当前 head，写到 `workflow.global-content`，不会生成 `workflow.prompt-resource`。因此本期不把它保留为独立提示词迁移入口；新 `resource.import` 写入退出，已导入资源与回执保留。这不删除通用 current resource 管理，也不开发 COND-03 通用转换。

## 5. 必须先解决的架构依赖

| 缺口 | 当前事实 | 必要实施边界 |
| --- | --- | --- |
| 混合库的执行包选择 | graph_project_packages 只有 `project` 单例；缺已存包全局阻止 graph mutation；任意 graph session 活动链阻止 configure | 保留历史精确选择/锁证据，另立当前执行配置边界及显式切换；冻结旧活动链不能阻塞同库新图，不清 active、不覆盖旧选择，也不增加通用多项目能力 |
| 新运行仍必经旧 host | graph_service start 调 GraphAgentHost._preflight_capabilities，后者无条件导入旧 Agent runtime/WorkbenchResourceStore；current 资源 host_call 仍借旧 mixin | 拆公共 current 资源声明、依赖预检、身份继承及 frozen frame；保留运行前拒绝和资源授权，不只改为懒导入 |
| 公共请求的 prepared 导入链 | runtime 每次请求都调用 check_prepared_request_capacity，顶层依赖 prepared_context 及旧 preparation 算法 | 轻量纯 gate 先校验 snapshot/确切 descriptor；非 prepared 拒绝非空 preparation evidence，prepared 才按需调用原完整校验；容量语义与异常不变 |
| 历史读取仍借旧执行实现 | GraphAgentHost._legacy_archive 导入 workflow、PreparedPromptContext、BasicContext；旧服务构造和 graph _recover 会写入 | 纯投影与精确 provenance/owner 门面；只读入口不 bootstrap、不补 facts、不恢复冻结日志 |
| 未决请求核实与客户端保存 | 正式旧客户端仍可 POST；混合 schema6 仍恢复编辑型旧投影 | receipt-only 和 frozen passthrough 先行，再替换 App/侧栏/节点配置/静态聊天；通过定向保护后才退役旧代码 |

依据：[graph_service](../backend/src/phase1_agent/graph_service.py)、[graph_platform](../backend/src/phase1_agent/graph_platform.py)、[GraphAgentHost](../backend/src/phase1_agent/graph_agent_host.py)、[GraphRuntimeHost](../backend/src/phase1_agent/graph_runtime_host.py)、[runtime](../backend/src/phase1_agent/runtime.py)、[prepared_context](../backend/src/phase1_agent/prepared_context.py)。

纯 prepared gate 不是重写算法或上下文维护：保留 projection2、冻结 S0/续接 observations、配置/parent/binding、变量事务/seed/rederivation、Lorebook 冻结验证、完整消息与 tools canonical 字符容量、消息上限和 dispatch 前拒绝。仅将 import 移进 runtime 调用点仍会让当前 Agent 加载旧链，不能据此宣称解耦。DEMO-05/06 继续后置。

共用资源纯声明包括 `resource_error/resource_id/resource_revision/workflow_identity`、content/data-definition/value/session-data 校验及 `CORE_SESSION_NOTE`。迁出时原异常码、限制、声明和旧同名导出保持，不能借拆模块改变数据协议。

## 6. 第 4 项核心覆盖与收口

以下是既有证据与本轮静态缺口映射，不是本轮重新执行的测试结果。阶段 A 核实的是必要能力是否已有当前实现、旧专用差异的处置，以及真实缺口的归属和完成条件；右列是阶段 B 的独立路径证明与阶段 C 的最终验收，不要求在阶段 A 内完成全部退役。

| 基线必要能力 | 可复用依据 | 阶段 B/C 的完成条件 |
| --- | --- | --- |
| 当前定义编辑/保存、provider/prompt 配置、精确引用 | [供应商切片](REPLACE-PROVIDER-2026-10-07.md)、[提示词切片](REPLACE-PROMPTS-2026-10-07.md)已有定向与限定浏览器证据 | 退出旧 App/store 后只走新路径；冻结行无网络/写入 |
| 当前串行、多轮、独立上下文、变量与对象写回 | 基线 1/2/5、[首轮无 compat 串行](REPLACE-AUDIT-2026-10-07.md)、[历史退役合并回归](REPLACE-HISTORY-RETIREMENT.md) | 新进程禁止旧执行链导入时实际 start/Agent 请求/资源读取仍可用，不仅证明 private runtime 未创建 |
| 完成历史、复制、检查点分叉及后续聊 | 首轮当前串行回归与当前 application 测试源码 | 冻结分类与同库包边界实施后的精确历史、作用域及新图操作；旧专用 reroll/角色分叉不算等价覆盖 |
| 当前暂停/恢复/关闭、确定失败及接纳重试 | 基线和受影响控制测试源码 | 当前运行边界不借旧 host；旧未知事实不自动重发或结算；旧扩额预算只保留历史，不新增当前扩额能力 |
| 当前默认入口、保存重载、精确图聊天分派 | [默认切片](REPLACE-DEFAULTS.md)、[聊天切片](REPLACE-CHAT-ENTRY.md) | 显式旧会话改只读后，旧静态 pending 保留且不提交；缺/失效身份继续不 fallback |
| 旧冻结历史与数据保留 | 95 项纯旧 Agent 契约/新进程/测试库重开、旧迁移与缺包测试源码 | 完整固定会话及 migrated archives 的无旧 host 数据端点；原 owner、消息增量、锁与 scope 不变 |
| 混合库、旧活动事实与当前新图共用 | 现有共存、缺包、类型及对象测试源码 | 临时混合库中旧选择/锁/status/active/facts/pending 不变，同时当前新图可配置、保存和运行 |
| 未决请求全生命周期 | 初始化 raw/pending 保留、当前 provider/prompt 安全用例 | 旧 receipt-only 缺失/不匹配/404/410/CAS 不清除、不 POST；当前 graph 后续核实拒绝的安全检查 |
| 安装、目录、受影响回归及正式交互 | 先前切片类型/构建与限定交互 | 目标代码的独立安装、目录及旧模块退出证明；常驻和完整浏览器联合验收仍归 REPLACE-06/VERIFY |

已存在但本轮未运行的相关源码包括：[包选择](../backend/tests/test_graph_package_selection.py)、[混合 graph service](../backend/tests/test_graph_service.py)、[旧迁移](../backend/tests/test_graph_legacy_migration.py)、[对象](../backend/tests/test_graph_session_objects.py)、[类型契约](../backend/tests/test_type_contract_store.py)、[初始化](../frontend/src/stores/workbenchInitialization.test.ts)、[统一文档](../frontend/src/adapters/unifiedWorkbenchDocument.test.ts)、[当前 application](../frontend/src/stores/workflowApplication.test.ts)。

首轮“未创建旧私有 runtime”、缺包只读、纯 Agent 历史校验和新默认包证据各自有效，但均不证明完整旧加载链已经退出。必要配置、编辑保存、当前串行/多轮、完成历史/复制/分叉及当前控制已有实现和相应限定证据；旧 authoring/专用控制差异已明确后置，不新增当前节点作为阶段 A 门槛。

阶段 A 的处置与核心覆盖盘点据此收口，真实缺口落入阶段 B 的加载/配置/纯读取/客户端隔离及阶段 C 验收。REPLACE-02 的实际独立性仍待闭环，不能把本轮审计称为端到端运行通过。

## 7. 后续实施顺序

1. 先落记录级 current/frozen/blocked 分类及历史/当前执行包配置边界，保护完整锁和旧活动事实，解除同库新图阻断。
2. 拆公共资源纯契约与 preflight/frame、纯 prepared gate；保持旧兼容导出直到旧调用者退出，验证当前运行不加载旧执行链。
3. 提供纯档案投影与受作用域限制的 receipt-only 查询；启动恢复跳过冻结记录。
4. 落浏览器 schema1–6 冻结 passthrough 和旧静态记录读取，替换 App/侧栏/节点配置/聊天旧活动入口，核实不 POST、不清 pending。
5. 在临时混合库与新进程取得定向证据后，退役旧 host/runtime/路由/专用 UI/store/测试；迁入必要回归与安装说明。
6. 按 REPLACE-06 与 VERIFY 完成同版本独立性及基础使用验收，另立 1.0 收口基线。

这些是 REPLACE-01–06 的依赖分解，不另增主线，也不承诺剩余只有一个代码切片。阶段 A 的盘点裁决、阶段 B 的实际退出、阶段 C 的最终验收分别记录；冻结、配置分流、纯读取及正式入口退役未落实，不能关闭阶段 B 或发布 1.0。

## 8. 本轮操作与验证边界

本轮仅源码读取、三代理审计合并及文档编辑。没有产品代码修改、测试执行、浏览器操作、真实模型调用、用户库访问、服务变更、提交或推送。此前 399 个后端案例、60 个前端案例及构建证据仍只归上一切片，不作为本轮新增通过数。

固定基线 SHA256：`0690253BEC23A2E377245E6F570B2814C3320CEA2C384A4DE927534678C6FCC4`。本轮验证：

- 五份新增/更新文档的 111 个本地行内 Markdown 链接、结尾换行、空白及冲突标记检查通过。
- 表内 30 个确切兼容节点身份无重复或缺项，已由后端子代理对照源码复核；三个子代理交叉复核已区分阶段 A/B、旧扩额、schema6 精确字段及通用写入口冻结边界。
- 固定基线及上一切片记录的六个产品源码 SHA256 均保持不变。
- `git diff --check` 通过，既有换行格式提示保留；不以文档检查冒充架构实现验收。

DEMO-05/06、COND、LATER 继续在 1.0 后按需安排；节点打组、历史展示正文编辑/删除继续暂停。
