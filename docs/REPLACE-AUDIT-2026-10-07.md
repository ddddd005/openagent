# REPLACE-01/02 首轮依赖盘点与退役准备

> 历史切片：下文的实现、节点版本、未推送状态和验证结果指登记时快照，不是当前入口。最新使用与工程说明见[文档索引](README.md)，当前退役范围见[节点目录](NODE-DIRECTORY-2026-10-09.md)；不按本页重建旧 API。

记录日期：2026-10-07，Asia/Shanghai。依据 [2026-10-06 基线](BASELINE-2026-10-06.md)与 [1.0 下一阶段规划](PLAN-1.0.md)，起点代码为 `00970f75fb52d741b694499a05f702600529fcdd`。本轮使用三个并发子代理分别只读盘点后端、前端、存储与测试，主代理核对关键链路、迁出测试夹具并执行定向离线验证。

状态：**REPLACE-01/02 已推进首轮盘点与局部验证，整体尚未完成；未退役产品实现，未完成 REPLACE-03–06，也未完成 1.0。**以下处置是实施依据和候选，不等于旧能力已经删除或未经裁决即可删除。

以上状态和下方行号是首轮历史记录。后续默认入口、聊天分派及处置进展以 [BACKLOG](BACKLOG.md)为准；旧 Agent 纯历史校验已迁出，孤立前端 mock 原型已按本表退役，见 [历史校验与原型退役](REPLACE-HISTORY-RETIREMENT.md)，不倒改首轮结论。

## 1. 本轮结论

- 当前 18 节点串行图使用 `tools.*`、`prompts.*`、`models.*`、`agents.execute@3`、`context.*`、`frontend.*`，不使用 `workflow.agent@2` 或旧固定 A/B 宿主。不需要为该示例先发明一个兼容基础包。
- 使用现有包配置接口显式禁用 `workflow.compat` 后，新串行图的多轮、分叉后续聊、复制隔离、关闭重开和再聊通过离线验证；同进程旧私有 `_native_runtime` 未创建。
- 这不证明旧加载退出：首次默认构造仍创建兼容 registry，默认及显式构造仍有兼容选择，服务入口、旧 UI、共享导入链和历史读取适配仍在。
- 新路径的主要缺口是入口与资源权威统一、旧定义/数据处置、缺包历史只读替代，不是把已完成的串行能力全部重做。
- 缺包降级已有两项测试保护历史只读；直接删除 fallback 并让服务启动失败会丢失这项能力。须先明确只读替代，不能仅依据“缺包应报错”删除。

## 2. 后端运行、加载与能力处置表

表内路径以 `backend/src/phase1_agent/` 为前缀；行号对应本轮未修改的产品源码。

| 范围 | 实际依赖 / 证据 | 处置与前置 |
| --- | --- | --- |
| 旧服务宿主 | `server.py:898` 创建 `WorkflowHost`；`workflow_host.py:33` 非资源白名单调用懒加载 `WorkflowService` | 退役旧宿主；先裁决旧路由/静态客户端，明确新启动及必要资源接口 |
| 旧业务 HTTP | `server.py:457` 起 session、输入、提示词、控制及专用重抽样等；新图在 `server.py:417` 单独派发 | 旧聊天/控制路由退役；资源接口须先统一，不能随路由一起静默删除 |
| 兼容包默认构造及强加 | `graph_service.py:94/103`、`graph_nodes.py:340` | 退出默认兼容加载；先确定普通旧版本图与保存锁支持范围，不直接改写旧定义 |
| 缺已保存包的降级 | `graph_service.py:108` 起诊断后加载兼容包 | 替换为明确的缺包与只读处置；不得无提示改用旧执行、改包锁、删除历史或丢失历史读取 |
| 旧普通图基础节点 | `graph_nodes.py:258` 起文本、输入、提示词、转换、变量、shared data、输出及对象 I/O | 必要声明迁出，或明确旧版本图执行后置；`tools.*` 等虽有等价能力，端口/schema/状态权威不同，不能直接改名替换 |
| 图内私有 Agent | `graph_nodes.py:329` → `graph_agent_nodes.py:46` → `graph_agent_host.py:322` → `graph_agent_runtime.py` | 退役旧执行；先处置 `workflow.agent@2`、旧 model-provider/context/assembly 定义与历史 |
| 旧 Agent 历史校验 | `graph_records.py:174/185` 对旧 `node_run.agent` 调用 `graph_agent_runtime` 纯校验 | 校验迁出并保留历史读取；不能删除执行器后使已存档事实不可读 |
| 旧图迁移接口 | `graph_http.py:116`、`graph_application_contracts.py:39`、`graph_agent_host.py:399` | 保留/离线化/退役待裁决；当前目标是 `workflow.agent@2`，不是 `agents.execute@3` 的通用转换 |
| 共享 Agent 内核与契约 | `agent_executor.py:22` 使用 `runtime.SnapshotKernel`；`contracts_v2.py:28` 引入 operation 纯校验 | 共用保留；按职责拆出必要校验，不能将 `runtime`、`contracts_v2`、`workflow_control` 整文件认作旧专用 |
| 旧 prepared 校验导入链 | `runtime.py:31/667` → `prepared_context.py` → bindings、prompt preparation、Lorebook | 抽出共享最小校验后退出旧准备链；当前不使用旧准备内容不等于导入依赖已经消失 |
| 小工具目录、正则等算法 | `agent_executor.py:23`、`builtin_packages.py:6/26` 依赖 `workflow_tool_catalog`；新包使用 `prompt_regex` | 共用保留，不按名称退役；生产工具管理与复杂 Lorebook 继续后置 |
| smolagents 适配与初版内核 | `tools.py:175` 按需适配；`kernel.AgentKernel` 的调用者主要为旧测试 | 旧适配/初版内核为退役候选；先裁决兼容支持并迁入必要协议回归，保留第三方来源与许可记录 |

## 3. 前端与客户端处置表

表内前端路径以 `frontend/src/` 为前缀；静态聊天路径以 `backend/src/phase1_agent/static/` 为前缀。

| 范围 | 实际依赖 / 证据 | 处置与前置 |
| --- | --- | --- |
| 新安装默认入口 | `stores/workspace.ts:36` 默认固定 A/B 示例；`fixtures/workflows.ts:6`；`App.vue:93` 新旧激活分支 | 迁入普通图默认入口；先保留/隔离旧浏览器存档和未知请求，不能清空存档 |
| 根页面双分支 | `App.vue:213/263/286` 的会话、控制、历史和画布分支 | 核心能力迁入普通图后退役旧 UI；目前 App 无条件创建多个旧 store，不等于普通图 store 已无旧 HTTP 就能删全部旧 store |
| 新图供应商侧栏缺口 | `App.vue:216` 固定使用旧 `ProviderPanel`；`stores/modelConfiguration.ts:124/190` 走旧 provider API；新 `CurrentProviderPanel` 仅通过 `plugins/modelFrontendPackage.ts:7` 注册面板 | 优先补普通图主导航的 current provider 入口，与模型节点共享同一 controller；旧面板、旧记录与 pending 请求先隔离，不双写 |
| 模型源当前资源 | `components/ModelSourceFields.vue:12`、`adapters/workflowResourcesApi.ts:9/27` | 新权威保留；旧侧栏保存不自动成为模型源可选 current provider |
| v2 全局内容编辑缺口 | `components/GraphNodeConfiguration.vue:18/28/37` 不区分 v1/v2、读取旧 `globalContent`；后端 `graph_prompt_nodes.py:107/109` 分别读取旧归档和 current resource | 补 v2 当前内容管理与选择，显式区分两个目录；不能让 v2 选择旧 ID 后假定新资源存在 |
| 旧内容库 | `components/ContentLibrary.vue:7` → `stores/globalContent.ts:75/143/160` →旧 `/api/global-content` | 必要内容管理迁入 current resource；旧档案只读/显式导入去向待定，不启动自动双写 |
| 普通图画布、保存、会话和运行 | `components/GraphWorkbench.vue:75/120`、`application/workflowManagement.ts:60/70/97`、`adapters/workflowApplicationApi.ts:42` | 新权威保留；原请求坐标中的 `/api/graph/...` 不代表旧固定流程，不能按字符串删除 |
| 新控制、历史、复制与分叉 | `components/GraphRunControls.vue:35`、`GraphHistory.vue:35/54`、`application/workflowEditing.ts` | 已有核心能力保留，迁入必要回归后退役旧控制/history 分支；不扩展更广失败重发 |
| 本机文档与总持久化 | `stores/workbenchPersistence.ts:150/163/179` 仍验证/创建旧 store；`adapters/unifiedWorkbenchDocument.ts:46/113` 保存统一文档并剔除重复图正文 | 拆纯新图存档及旧记录只读；当前 legacy 投影不是无条件双份持久图权威，不可把投影一概当双写删除 |
| 旧准备程序与迁移 UI | `components/PreparationWorkbench.vue:44`、`adapters/workbenchContext.ts:542`、`domain/legacyGraphMigration.ts:80/107/168` | 按能力迁入/后置；保留明确拒绝模糊历史来源和隐式写入漏迁，通用转换不自动纳入 1.0 |
| schema、位置字段、通知和包宿主 | `GraphSchemaFields`、`RolePlacementFields`、`workbenchNotices`、`application/workflowFrontendPackage.ts:8` | 共用保留并按职责迁出；可信包的声明/版本检查保留，不为了“统一”绕过扩展契约 |
| 普通图聊天 | `graph-chat-core.js:528/535/764` 使用 discovery、consumer queries/commands | 保留新消费者权限边界，不授予管理端私有读取；已有工作台分叉入口，不代表聊天端必须重复实现 |
| 聊天默认分派与旧聊天 | `chat-entry.js:5` 无图身份时加载 `app.js`；`app.js:1005/1409/797/733` 为旧会话、输入、重抽样和分支 | 改为明确新入口/缺身份状态后退役旧默认；`adapters/legacyUi.ts:22` 的共同地址配置不能仅因名称而删除 |
| 最早 mock 原型岛 | `EditorCanvas`、`WorkflowNode`、`InspectorPanel`、`RunView`、`HistoryView` 及 editor/runtime/mock；主要仅岛内和 `stores/stores.test.ts` 引用 | 退役候选；先核对共用函数及回归，不将其中原型打组当现有受支持能力，不恢复暂停项 |

## 4. 数据、schema、安装与测试处置表

表内后端源码路径以 `backend/src/phase1_agent/` 为前缀。

| 范围 | 实际依赖 / 证据 | 处置与前置 |
| --- | --- | --- |
| SQLite 根设施、records、回执 | `storage.py:104`、`graph_store.py:11/30/43`；新旧记录以 `execution_model=graph` 区分 | 共用保留；不得随旧宿主删除事务/身份/回执设施或混合库中的记录 |
| schema v0→v13 | `storage.py:115/116/298` 新连接仍初始化旧表 | 先设计新安装初始化与旧库兼容策略；不删旧迁移、不降版本、不把未知新版本重建为新库 |
| 程序变量状态、heads、回执和绑定 | `storage.py:254`、`graph_service.py:294/306/320/545`、`graph_candidates.py:97` | 共用保留；新图 checkpoint/copy/恢复与失败控制仍依赖 |
| 旧准备输出缓存 | `program_variable_store.py:119/154` 的 cached/save_outputs，仅旧 `workflow.py:4042/4087` 调用 | 旧执行退出后退役方法/写入；既存表与数据保留，不牵连变量状态设施 |
| Workbench 内容与数据纯声明 | `workbench_resources.py:25/56/88/121/155`；`global_resources.py:15`、`graph_nodes.py:20` | 迁出共用校验与 `CORE_SESSION_NOTE`，不能整模块删除 |
| 旧资源、模型修订和会话 owner | `workbench_resources.py:163/169`、`graph_agent_host.py:20/28/114/123/147` | v1 图/旧公开配置退出后才退役专用入口；资源修订不是新 current 资源的自动桥接 |
| 新 current 资源 | `global_resources.py:137/153/330`、`graph_platform.py:245/271` | 保留新权威；现有显式单项导入不是通用 COND-03，不在启动时自动转换 |
| 旧 prompt/variable/result-port/exposure 表 | `storage.py:153/182/214/221/243`、`workflow.py:713/970` | 旧专用候选；调用者退出后停止新初始化/写入的策略需明确，保留已有数据 |
| execution facts | `storage.py:1423/1475` 的旧表；`agent_executor.py:19` 仍使用共享 `ExecutionFactHistory` | 表和纯事实算法分别处置，不能一并删除 |
| 新对象、manifest、包配置与类型契约 | `session_objects.py:18/123/433`、`type_contract_store.py:19` | 保留，不是可清理缓存；关系到成员资格、对象修订、包锁及 schema 不可重定义 |
| 新事实与信息绑定建表 | `runtime_fact_store.py:14`、`graph_information.py:20` | 保留，后续初始化收口需保护新旧库重开；目前建表分散，不假定都由 user_version 管理 |
| 已迁移旧档案 | `graph_agent_host.py:170/374` 引用冻结 turn/snapshot/run | 旧档案仍是迁移图历史依据，不删 records；只读去向和执行退出分别裁决 |
| 核心及兼容安装 | `backend/pyproject.toml:10/18` 核心三依赖、smolagents 可选；`backend/requirements-dev.txt:2/3` 仍安装 vendor/compat | 核心安装保留；兼容测试与 extra 分开处置，不宣称默认全目录收集已独立 |
| 顶层兼容测试 | `test_tools.py:7`、`test_g3_flow.py:8`、`test_g3_http_flow.py:4`、`test_g4_boundaries.py:7` 导入 smolagents Tool | 迁入必要协议回归后隔离或退役，不能删 vendor 内部导入片段来凑独立安装 |
| 新测试借用旧夹具 | 五个图/资源测试原从 `test_workbench_resources` 导入纯 `resource()`，间接加载旧宿主及旧测试 | 本轮已迁到独立 `resource_fixtures.py`，旧测试也复用同一函数；未宣称所有测试依赖已解耦 |
| 前端 fake server 与迁移回归 | `frontend/src/testUtils/graphApplicationServer.ts:5/18`、`workflowGraphRegressions.test.ts:4`、`workflowGraphMigration.test.ts:7` | 保留必要未知请求、撤销、copy 回归后退出旧夹具；fake server 不能替代真实服务和浏览器验收 |

## 5. 新路径覆盖核实

| 能力 | 本轮依据 / 结果 | 未覆盖边界 |
| --- | --- | --- |
| 定义、会话、资源、对象及运行控制 | `graph_application_contracts.py` / `GraphApplication` 已有公开管理与消费者操作 | 根 App 与旧资源入口未统一，不宣称整个客户端已独立 |
| 最新串行、独立上下文及公开展示 | 原基线已有实现；本轮 default / without-compat 两种配置下的离线回归通过 | 没有新真实调用、浏览器或长期规模证据 |
| 完成历史、分叉后续聊、复制隔离、重开再聊 | 本轮两种包配置验证；重开保持无兼容包选择，未创建旧私有 runtime | 不等于活动现场跨进程恢复，不改变旧定义包锁 |
| 新模型供应商配置 | current provider controller、模型源字段已有 | 普通图主导航侧栏仍写旧 provider，需补入口 |
| 全局内容 | 后端 v2 current 引用与显式导入已有 | 内容库及 v2 选择器仍用旧目录，需补 current 管理接线 |
| 缺包历史可读 | `test_graph_session_objects.py:291`、`test_plan13b_frontend_packages.py:244` 已有保护 | 本轮未改 fallback，以上两个用例未重跑，不称其已取得新版验收 |
| 配置/编辑、控制、多轮联合使用 | 已有相应新组件和定向测试，详见基线 | VERIFY-01–04 仍待架构收口后的常驻与浏览器验收 |

## 6. 本轮实际修改

产品源码、schema、数据库迁移、默认包选择和前端交互均未改变。

- 新增 `backend/tests/resource_fixtures.py`，原样迁入 `resource()`；仅使用标准库，不导入服务或测试模块。
- 修改五个新图/资源测试的导入：`test_global_resources.py`、`test_graph_application.py`、`test_graph_current_resources.py`、`test_graph_prompt.py`、`test_graph_resource_dependencies.py`。
- `test_workbench_resources.py` 改为导入相同构造器，保留旧测试资源契约。
- 新增 `test_resource_fixtures.py`，验证默认/自定义正文、UUID 身份及可变成员隔离。
- `test_serial_agent_demo_integration.py` 的两个既有案例增加 default / without-compat 参数，验证项目包选择、旧私有 Agent 不可用及私有 runtime 未创建；未改变业务实现。

复核保留的测试依赖：`test_graph_candidates.py:16` 仍顶层导入旧 `WorkflowService`，checkpoint/history 两个文件借用该夹具；`test_graph_server.py:21` 的字符串 monkeypatch 会导入旧模块，只能证明未实例化；`test_graph_prompt.py:19` 仍借用旧普通图 Agent v2 的 harness。新资源夹具的纯导入由源码核实，新增两项测试主要保护形状与对象隔离，不宣称已验证所有新测试完全不导入旧模块。

## 7. 本轮验证记录

环境：现有 `openagent/.venv`，每批使用新建隔离临时目录，没有读写原运行库、秘密或历史原始输出。

在 `backend/` 执行以下定向范围，实际命令另指定本轮唯一 `--basetemp`：

```powershell
..\.venv\Scripts\python.exe -m pytest -q `
  tests/test_resource_fixtures.py `
  tests/test_global_resources.py `
  tests/test_graph_application.py `
  tests/test_graph_current_resources.py `
  tests/test_graph_prompt.py `
  tests/test_graph_resource_dependencies.py `
  tests/test_serial_agent_demo_integration.py `
  tests/test_workbench_resources.py::test_resource_http_cas_revision_receipt_delete_and_missing `
  tests/test_workbench_resources.py::test_global_three_rounds_latest_new_run_frozen_history_reopen
```

结果：**73 passed，73.75 秒，退出码 0**。共八个测试文件，其中旧资源测试只选上述两个受影响案例；串行文件四个参数案例包含在 73 项中，不另加数量。

三个子代理只读复核后，在首个服务完成多轮/分叉、关闭前补上包选择及私有 runtime 断言。随后单独复测 `tests/test_serial_agent_demo_integration.py`：**4 passed，39.22 秒，退出码 0**。四项与上述批次重叠，不累加为 77 项；最终串行断言以该复测为准。

未执行：前后端全量、前端类型/构建、浏览器人工、真实模型调用、常驻部署变更、全面进程/存储故障专项。未扩大 DEMO-05/06、COND、LATER 或暂停项。

## 8. 未完成裁决与下一切片

| 顺序 | 必须明确的事项 | 后续动作 |
| --- | --- | --- |
| 1 | 1.0 受支持普通图版本：仅当前包，还是继续执行 `workflow.*@1/2` | 逐节点注明迁入、冻结只读、后置或退役；不批量改 ID/版本/包锁 |
| 2 | 缺包时历史只读的提供方式、何时允许重新执行 | 保留诊断、原选择及档案；设计显式只读/缺包拒绝方案后再去兼容 fallback |
| 3 | 旧浏览器存档、未知 outbox、已迁移档案的去向 | 新入口优先但不清空旧记录，不能将旧来源静默换为当前状态 |
| 4 | 旧模型/内容修订向 current 资源的显式入口 | 统一新业务写入；是否提供有限导入另裁决，通用转换继续后置 |
| 5 | 可选 smolagents 适配、旧初版内核、旧迁移入口的支持范围 | 保留必要协议/历史回归，明确兼容测试隔离或退役，保留许可记录 |
| 6 | 新安装初始化与既存混合库策略 | 不删旧数据，不将初始化清理等同数据库迁移完成 |

优先的下一代码切片是补 REPLACE-02 的普通图供应商侧栏：复用 current panel/controller 和确切包声明，让主导航与 `models.source` 指向同一资源权威；临时隔离旧工作流面板及 pending，不双写。随后补 v2 全局内容 current 管理/选择，再按处置表推进运行加载、客户端入口和专用实现退出。

以上六项裁决仍是 REPLACE-01 的未完成部分；核心使用入口及最终验收仍是 REPLACE-02 的未完成部分。不是要求开展通用旧数据转换、生产工具、长上下文或其他 1.0 后置开发。
