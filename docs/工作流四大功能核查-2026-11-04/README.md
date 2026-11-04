# 工作流四大功能核查：创建节点 / 节点列表与区分 / 配置编辑 / 运行

2026-11-04 · 只读核查（未改代码、未写数据库、未启停服务、未发起真实模型调用）
工作树：`develop`，HEAD `8bd2717`。证据以当前工作树源码为准；标注「推断」者为代码推断而非实测。
配套：[附录-测试清单与校验矩阵.md](附录-测试清单与校验矩阵.md)。

---

## 0. 核查结论

你的判断——**"框架大致都有，但具体执行顺序和代码各自有各自逻辑"**——**成立**。四项功能都能在代码里找到完整实现，但它们不是同一个框架的四个入口，而是**四条独立管线**，各有自己的身份、版本、写入通道、校验时机和失败表现：

| 你归纳的功能 | 现状 | 实际所属管线 | 主要问题 |
|---|---|---|---|
| ① 创建自定义节点并加载 | 有（包目录 + 清单 + 注册 + 发布） | 能力包管线（package → loader → registry → publish） | 无节点级试跑；`default_config` 与节点配置**保存期不校验**；启用要"放目录 + 重启 + 全量选择" |
| ② 获取节点列表并区分相同节点 | 部分（能取到，但**没有节点列表接口**；同类型同标题不可区分） | 定义文档（`definition.read`）+ 类型目录 + 会话状态，前端三源拼装 | 无 `node.list`；`title` 可重复可空；复制不区分；历史视图按标题 |
| ③ 节点配置编辑（配置 / 内容 / 绑定） | 有，但三者是**三种不同载体** | 配置文档管线 + 全局资源层 + 会话对象层 | 校验时机分裂：保存只验结构，`config_schema`/引用/绑定/端口全推迟到运行；写入通道 6 类 |
| ④ 运行工作流 | 有，链路完整 | 执行管线（prepare → execute → settle） | 所有编译级校验集中在 `run.prepare`；无单节点 / 局部 / dry-run |

三条量化事实（均可复核，见附录）：

1. **测试规模**：后端 264 个测试文件 / 2320 个用例 / 约 74k 行；前端 128 个测试文件 / 约 760 个用例。其中与"包/节点"相关约 **44 个后端文件 / 414 个用例 / 约 1.3 万行**，另有前端 20 个文件 / 133 个用例。而真正支撑"节点能跑、输入输出正确"的**最小集约 5–8 个用例**。这与你说的"节点创建后的测试流程过于冗杂"直接对应。
2. **没有"试跑一个节点"的入口**：命令表里只有 `run.start`（整张图）、`event.submit`、`run.control`，没有 `node.test`/`dry-run`（`graph_application_contracts.py:37-78`）。唯一"只编译不运行"的是查询 `definition.inputs`，且它要求整图可编译、必须连通到输出节点（`graph_contracts.py:886-893`）。
3. **"保存成功"不等于"能跑"**：`definition.update` 只校验文档结构与节点类型存在（`workflow_configuration.py:390-469`），`config_schema`、`config_node_references`、端口/类型/必填输入/环、包可用性全部在 `run.prepare` 的编译里才检查（`graph_service.py:921-926`；`graph_contracts.py:819-1048`）。这就是"创建看着没问题、一跑才报"的结构性原因，也说明"创建后跑一次验证"应该成为标准动作——而现在恰恰没有这个入口。

四项功能与既有产品文档的对照：`docs/USER-GUIDE.md:23-32` 把工作流分为"定义维护 / 配置 / 数据管理 / 执行"四类。你的分法把"定义维护"拆成了 ①②，把"数据管理"并进了 ③④。**核查发现"数据管理"（会话/会话对象/初值）仍然是独立的第三条数据面**，在 ② 的运行状态合并与 ③ 的"内容=会话对象"里都会出现，建议重构时显式保留它，不要并掉。

---

## 1. 方法与边界

- 四分区并行只读核查：①包与节点创建/加载/验证、②节点列表与实例区分、③配置/内容/绑定的编辑通道、④运行链路与校验时机；本报告做交叉汇总。
- 未运行测试、未启动服务、未写库；数据规模统计来自仓库文件计数，不是测试运行结果。
- 「证据等级」：代码事实（直接读到实现/常量）、计数事实（文件/用例计数）、推断（由代码行为推导）。
- 与既有三轮文档的关系见 §8。

---

## 2. 功能①：创建自定义节点并加载

### 2.1 现状链路（四段，每段落在不同模块）

**第 1 段 · 包目录与清单**（作者侧）
- 目录形态：`packages/workflow/<package>-<version>/openagent-package.json` + Python 包（`<pkg>/__init__.py`、`<pkg>/package.py`）。默认包根是主 SQLite 所在目录下的 `packages/workflow/`（`workflow_host.py:52-54`；`server.py:595-603` 可用 `--workflow-package-root` 改）。
- 清单外层只有 3 个键：`schema_version`、`entrypoint`（`"module:attr"` 工厂）、`manifest`；`manifest` 复用包声明格式（`package_id`/`version`/`host_protocol_version`/`dependencies`/`exports`），`exports` 按 schema 版本允许 `nodes`/`data_types`/`frontend_extensions`（+ 后续 schema 的 `executors`/`pause_support`/`services`/`information_sources`）（`capability_packages.py:62-122`；示例 `examples/workflow-packages/text-metrics-1.0.0/openagent-package.json:1-20`）。
- 三个示例包都只导出 `nodes`（`text-metrics:13-18`、`numeric-clamp:14-18`、`workspace-demo:13-17`）。

**第 2 段 · 注册**（作者代码里）
- `entrypoint` 返回 `CapabilityPackage`，其 manifest 必须与 JSON 声明一致（`local_workflow_packages.py:67-107`，比对 `:86-88`）。
- `register(host)` 内 `host.register_node(NodeDefinition(...))`：声明 `component_id`/`component_version`/`display_name`/`category`/`default_config`/`config_schema`/`inputs`/`outputs`（`NodePort`：`data_type`/`data_schema_version`/`required`/`multiple`/`accepted_types`）/`capabilities`/`object_accesses` 等（`graph_contracts.py:338-392`；最小范例 `text_metrics/package.py:76-112`）。
- 注册期校验：端口 `data_type` 必须已注册（`graph_contracts.py:614-630`）、`config_schema` 合法性（`:605`）、`capabilities` 与 `object_accesses` 一致（`:585-595`）、executor/executor_ref 可用（`:514-520`）、`config_node_references` 合法（`:536-549`）。**`default_config` 不在注册期校验**（全文件只有声明 `:368`）。
- 数据类型用 `host.register_data_type`/`register_shared_data_type`（`capability_packages.py:182-198`）。

**第 3 段 · 发现与发布**（宿主侧）
- 发现：只读解析包目录 JSON，`available: true` 只表示声明可读，不代表入口可加载（`local_workflow_packages.py:127-193`；`WORKFLOW_PACKAGES.md:76`）。
- 发布：服务初始化时按数据库保存的选择 resolve → staging → 一次性 publish（失败保留旧发布并记 `package_publication`）（`graph_service.py:174-230`；`capability_packages.py:364-520`）。
- 运行期启用：`packages.configure(enabled_packages)` — **全量选择**，活动运行期间拒绝（`graph_platform.py:183-216`，`:190-198`），事务写选择 + 类型契约，然后发布（`:199-216`）。
- 生效时点：**改包目录/代码必须重启 Host** 才取得新来源快照；页面刷新不扫盘（`WORKFLOW_PACKAGES.md:55-56`）；选择变更即时生效并持久化。

**第 4 段 · 前端加载**（画布侧）
- 类型目录：`GET /api/graph/node-types/v2` → `catalog.node-types{protocol_version:2}`（`graph_http.py:80-81`；`graph_service.py:470-473`）。
- 未知节点归入"其他"类、显示 `display_name`、端口名回退 `port_id`、配置表单按 `config_schema` 通用渲染（`frontend/src/domain/nodeCatalog.ts:104,118-131,154-170`；`GraphNodeConfiguration.vue:20-34,97-99`）——**普通节点零前端改动**。
- 只有需要自定义字段 UI 时，才要包声明 `frontend_extensions`（`capability_packages.py:217-246`）且前端已有已编译的同签名实现（`frontend/src/domain/frontendExtensions.ts:72-95`）；解压目录里的 JS 不会被加载（`WORKFLOW_PACKAGES.md:121-123`）。

### 2.2 "新建一个节点"实际要做的事

| 类型 | 必需改动 | 启用手工动作 |
|---|---|---|
| 普通节点 | 包目录 + `openagent-package.json` + `<pkg>/__init__.py` + `<pkg>/package.py`（3 个文件） | 放入包根 → **重启 Host** → `packages.configure` 提交全量选择 → 刷新目录 |
| 内置节点 | 新增包模块 + `builtin_packages.py:18-37` 登记 | 重启 |
| 自定义字段 UI | 再写前端插件模块，在 `workflowFrontendPackage.ts:3-12` 登记并构建 | 前端构建 + 重启 |

结论：**节点本体确实只要 3 个文件**，冗杂感主要来自"启用"的三个手工动作（放目录、重启、全量选择）和下面要说的测试矩阵，而不是节点定义本身。

### 2.3 验证/测试现状：用户的判断对不对

**对的部分**：从能力上说，验证一个节点"能跑 + 输入输出正确"确实只需要一个最小的 2 节点图（源节点 → 目标节点，目标 `is_output=True`）跑一次 `execute_graph`，或只做一次 `GraphCompiler.compile`。仓库里已经有这种范式：`backend/tests/definition_stage_fixture.py:33,41-42,51`、`test_definition_packages.py:40-47,97-110`、`test_graph_execution.py:55-62`。

**不对/缺的部分**：
1. 这不是产品能力，只是**测试代码**里的做法。没有 API、没有 UI、没有命令能"试跑一个节点"；`definition.inputs` 虽是唯一提前编译入口，但要整图连通到输出节点，且诊断只在"本次输入"弹窗里展示（`frontend/src/components/GraphRunControls.vue:25-33,82`）。
2. 现有测试矩阵远超"能跑 + I/O 正确"：与包/节点相关的约 **44 个后端文件 / 414 个用例 / 1.3 万行**（契约 14/128、整图 15/125、历史与架构 15/161），前端另有 20 文件 / 133 用例。多数不是"验证这个节点"必需，而是发布原子性、失败诊断、架构接线、模型阶段、配置派生的旁证或历史积累（明细见附录 A）。
3. 真正该在"创建后"就暴露的问题反而不在测试矩阵的默认路径上：`default_config` 注册期不校验、节点实例配置保存期不校验，坏配置可以一路保存成功，到 `run.start` 才 `graph_invalid_config`（`graph_contracts.py:941-943`）。

**结论**：用户的"只需测试能运行、输入输出正确"作为**验证目标**是对的，而且当前实现足以支撑（最小 2 节点图），但缺两个东西：①一个"创建即可验证"的单入口（节点试跑或 `definition.validate`）；②把 `default_config`/节点配置纳入早期校验，否则"跑一次"是唯一能发现问题的手段，而它现在又最麻烦。

---

## 3. 功能②：节点列表获取与相同节点区分

### 3.1 现状链路：三个来源在前端拼装

1. **定义文档**（实例数据）：`definition.read` → 文档 `nodes[]`，每项含 `node_binding_id`（UUID4）、`component_id`/`component_version`、`title`、`position`、`config`、`public_outputs`（`configuration_service.py:47-55`；`_compose_configuration:286-319` 会把 title/position 从 `node_presentation` 回填、合并 adjustable 配置）。前端 `frontend/src/adapters/workflowGraphApi.ts:33-38`（`readGraphDefinition`）→ `frontend/src/stores/workflowGraph.ts:318-383`（`ensureDefinitionEntry`）→ `frontend/src/components/GraphWorkbench.vue:130-137` 的 `nodes` computed。
2. **类型目录**（类型定义）：`catalog.node-types` v2 → `NodeDefinition.to_dict`（`graph_contracts.py:394-426`）→ `frontend/src/stores/workflowGraph.ts:256-269`（`loadCatalog`）/`:252-255`（`typeFor`）。只给类型，不含实例。
3. **会话状态**（运行态）：`session.read` 的 `nodes` 以 `node_binding_id` 为键，带 `label=title`/`status`/`run_id`/`outputs`（`graph_service.py:825-839`）→ 前端按 `node_binding_id` 合并到画布（`GraphWorkbench.vue:132-133`）。

**没有独立"节点列表"查询**：操作表里没有 `node.list`/`definition.nodes`，HTTP 也没有节点路由（`graph_application_contracts.py:132-189`；`graph_http.py:80-115`）；要拿节点只能读整份文档（或 `definition.list` 拿全部文档）。侧栏只有节点数量，没有列表（`WorkflowSidebar.vue:28-30,61`）。

### 3.2 身份模型：区分靠什么、哪里会撞

| 机制 | 现状 | 问题 |
|---|---|---|
| `node_binding_id` | 文档内唯一 UUID4（`graph_contracts.py:133-135` 只校验文档内唯一） | **跨工作流不唯一**：`definition.copy` 沿用原 `node_binding_id`，返回恒等 `node_id_map`（`workflow_configuration.py:530-533,545-555`；`definition.update` 同理 `:467`）。完整身份 = `(workflow_definition_id[, definition_revision], node_binding_id)` |
| `title` | 可重复、可为空，只有长度 ≤128 校验（`graph_contracts.py:136-140`）；创建时取 `display_name`（`workflowGraph.ts:1181`） | 画布只显示 title（`GraphCanvasNode.vue:38`）；默认标题还会折叠成类型名（`nodeDisplayTitle:174-178`）→ **同类型同标题节点在画布上无法区分** |
| `position` | 仅显示数据，不参与运行（`workflow_configuration.py:250-254`） | 是画布上唯一可见区分；拖动后更难辨认 |
| 复制 | 新 UUID + 位置偏移 35px，`title`/`config` 原样（`workflowGraph.ts:1290-1295`） | 复制后两个同名节点，无编号 |
| 运行数据 | `node_run` = `run_id`+`node_binding_id`；`workflow_output` = `node_binding_id`+`run_id`+`port_id`；重试同 binding 新 run_id（`graph_records.py:32-39`；`graph_service.py:731-733`；`graph_failed_retry.py:117-126`） | 运行侧**无歧义**，只是展示不足 |
| 短 id / 序号 | 无；完整 UUID 只在检查器"技术详情"（`GraphWorkbench.vue:294`） | 用户无法快速分辨 |
| 分组/映射视图 | 只有规划文档，无存储实现（`docs/节点映射与分组视图/01-...md:36`） | 不能拿它当"区分"手段 |
| 历史视图 | `GraphHistory.vue:22-25,69-70,78` 只按 title 渲染 | 同名节点在历史里视觉合并 |

### 3.3 缺口

1. **缺 `node.list` 只读查询**：返回 `[{workflow_definition_id, definition_revision, node_binding_id, component_id, component_version, title, position, public_outputs}]`，内部复用 `_compose_configuration` 即可，不需要新存储。（现在只能整文档读，前端自己筛。）
2. **缺重名策略**：复制自动标题编号、画布与历史在重名时附 `node_binding_id` 前 8 位。若要求"标题唯一/非空"就是契约变更（`graph_contracts.py:136-140`），存量文档可能已有重名/空名，风险高，建议先做 UI 策略。
3. **跨定义列表必须带 `workflow_definition_id`**：否则复制出来的工作流之间 `node_binding_id` 碰撞（`workflow_configuration.py:545-555`）。
4. 历史视图至少补短 id，否则"运行记录可区分"这个数据优势在 UI 上被浪费。

---

## 4. 功能③：节点配置编辑（配置 / 内容 / 绑定）

### 4.1 三个子面其实是三种载体

| 子面 | 载体 | 编辑入口 | 保存通道 | 校验时机 |
|---|---|---|---|---|
| **配置** | 节点 `config`（`config_schema` 参数）+ `configuration_policy`（`prompt_mode` 为 fixed 或 adjustable）+ 节点声明的 `adjustable_configuration` | 画布右栏 `GraphNodeConfiguration`：扩展字段编辑器、固定/可调切换、`object_access` 下拉、通用 schema 字段（`GraphWorkbench.vue:285-286,75-99`；`GraphSchemaFields.vue:78`）→ `patchNode`（`workflowGraph.ts:1201-1207`）/`patchNodeConfigurationPolicy`（`:589-599`）→ `submitConfigurationDocument`（`:472-532`） | `definition.update` 整文档（扩展字段的 SDK2 提交仍走它，客户端按 `configurationFields` 白名单，`frontend/src/plugins/workflowFrontendSdk2.ts:45-115`） | 保存只验声明/格式/策略（`workflow_configuration.py:174-203,415-416`）；`config_schema` 在 `definition.inputs` 与 `run.prepare` 编译时校验（`graph_contracts.py:732-739,942`） |
| **内容** | **没有独立"节点内容层"**：①节点文本就是 `config` 字段（`prompts.item@3` 的 text/presentation；`prompts.group@2` 的 members[].text，`prompt_lifecycle_nodes.py:108-121`）；②长内容在**全局资源层**（prompt-resource/lorebook/model profile/provider），定义里只存 `reference`/`object_keys` 指针（`prompt_lifecycle_nodes.py:151-160`；`tavern/lorebook_resources.py:93-118`；`model_package.py:41-61`）；③`frontend.node@2` 的聊天内容是**会话对象** `CHAT_CONTEXT@1`（`frontend_node_v2.py:63-88`） | ①右栏/专用编辑器；②插件面板；③节点页面 | ①`definition.update`（按策略进固定修订或 `workflow_current_configuration` 可调层，`workflow_configuration.py:217-235,244-265,450-457`）；②`resource.save`/`content.metadata.update`/`provider.credential.save`（`graph_application_contracts.py:42-50,76-77`）；③frontend-node 私有通道 + `patch-input` | 指针与本体跨表，保存期无一致性校验；资源可用性在运行时预检 |
| **绑定** | 模型=`models.source@7` 的 `config.reference` → 全局 profile；工具/Agent=`config.agent_node_id` + `config_node_references`（target `agents.execute@10`，`tool_binding_package.py:30-34,150-169`）；对象=`document.object_bindings`(readers/writers/owner/registrar) + 节点 `object_key`；服务=节点 `service_requirements`（用户不可编辑，由包选择决定）；控制边/会话预加载在文档内 | 模型/工具走通用 schema 字段（无专用编辑器）；对象绑定用 JSON 编辑器 `GraphObjectBindings.vue:1-45` → `setObjectBindings`（`workflowGraph.ts:1431-1442`）；预加载 `GraphSessionPreloads.vue:73,80` → `:1417-1429,1444-1490` | 全部经 `definition.update`（对象绑定/预加载是文档字段） | 保存期校验"引用节点存在"（`graph_contracts.py:166-183,216-228,751-792`）；ACL/对象类型/工具绑定准备期才校验（`graph_contracts.py:1027-1047`；`tool_bindings.py:67-105`） |

### 4.2 写入通道盘点：1 条主通道 + 5 条旁路

| # | 通道 | 写什么 | 证据 |
|---|---|---|---|
| 1 | `definition.update`（整文档 + CAS） | 定义文档的一切（节点/边/配置/绑定/预加载/包锁） | `graph_application_contracts.py:51-53`；`workflow_configuration.py:390-469` |
| 2 | 前端扩展 SDK2 窄提交 | 仍是 `definition.update`，客户端按 `configurationFields` + `expectedConfig` 限字段 | `frontend/src/plugins/workflowFrontendSdk2.ts:45-115`；`workflowGraph.ts:533-588` |
| 3 | `frontend-node.patch-input` | 服务端窄改，白名单**只有** `tools.editable-input@1.text` | `configuration_mutations.py:9-38`；`frontend_node_operations.py:148-169`；`workflow_configuration.py:341-365` |
| 4 | 内容资源 | `resource.save/delete`、`content.metadata.update`、`provider.credential.save`（不改定义） | `graph_application_contracts.py:42-50,76-77` |
| 5 | 运行数据 | `object.write`、`session.metadata.update`、`run.*`（不改定义） | `graph_application_contracts.py:44-45,70-75` |
| 6 | 定义级生命周期 | `definition.copy/archive/delete` | `graph_application_contracts.py:54-61` |

**前端没有第二套定义写路径**（只有内存乐观文档 + 单次提交；本地草稿写入口已退役只读，`workbenchPersistence.ts:37-66,79-88`）。这一点与上一轮排查结论一致：真正"多通道"的地方在后端与资源/会话层，不在画布的保存路径。

### 4.3 分叉与重复（这就是"各自逻辑"在 ③ 的表现）

- **校验四分**：保存（结构 + 声明 + 策略）→ 读取合成（再验声明/registrations/preloads，`workflow_configuration.py:286-319`）→ `definition.inputs`（完整编译）→ `run.prepare`（编译 + 实现 + 资源/工具预检）。同一"声明层"在 4 处重复出现。
- **`config_schema` 校验两处**（inputs 与 run），保存期没有。
- **保存期不校验的东西**：`config_schema`、`config_node_references`、object ACL、端口类型/必填/环、包/资源存在（`graph_service.py:921-926`；`graph_contracts.py:819-1048`）。
- **内容跨表**：内容指针（reference/object_keys）与内容本体分表，保存期不做一致性校验；`object_bindings` 是定义级，对象值是会话级。
- **前端字段白名单**（`configurationFields`）只在前端维护，后端不校验"某字段归谁改"（`workflow_frontendSdk2.ts:60-76`）。
- **白名单漂移风险**（未确认）：`workflow_configuration.py:98,100` 的策略白名单提到 `prompts.group@3`，而生产注册只见 `@2`（`prompt_lifecycle_nodes.py:118-121`）。

---

## 5. 功能④：运行工作流

### 5.1 时序

```
run.start (202)                                  命令契约 graph_application_contracts.py:66
  └─ 单事务 prepare（BEGIN IMMEDIATE，graph_service.py:1005-1029）
       抓 RunBasis（冻结 registry/catalog）         :1010, :404-416
       校验会话修订/无活动链                        :861-889, :902
       包诊断                                        :903-904
       读固定文档 + 合成当前配置                     :905-913
       输入校验                                      :914-920
       GraphCompiler.compile                         :921-924
       类型证据 + 实现检查                           :925-926
       工具绑定/资源/服务预检                        :927-936
       冻结 node_run、写 chain、会话 active          :943-960
       写 start manifest                             :961
  └─ 后台线程 _execute（:973-983）
       重读固定文档 + 冻结 config 覆盖               :1435-1445
       第二次 compile                                :1469-1472
       execute_graph 逐节点 running→执行→succeeded   graph_execution.py:319-627
  └─ settle（:1508-1549）
       写 chain/session 状态、失败附 failure_retry、成功 commit+checkpoint、end manifest、notify、release
```

失败三种表现：**prepare 异常** → HTTP 4xx、事务回滚、**不产生 run 记录**（`graph_service.py:1024-1029`）；**节点失败** → chain/node `diagnostic`，`active_chain_run_id` 保留（只有 succeeded 清空，`:1534-1537`）；**基础设施异常** → `archive_failed`/`recovery_unavailable` + `reason_code`（`:1325-1365`）。

### 5.2 阶段 × 对象校验矩阵（摘要，完整见附录 B）

| 对象 | 保存 `definition.update` | `session.create` | `run.prepare` 编译 | 执行 |
|---|---|---|---|---|
| 节点类型/执行器 | 需注册（`workflow_configuration.py:269-284`） | 不查 | compile + 实现检查（`graph_contracts.py:932-936`） | 再查 executor 未变 |
| 包锁 | 仅结构（`graph_contracts.py:245-250`） | 包诊断 | 比对（`:852-855`） | 用冻结 registry |
| `config_schema` | **不校验** | 不校验 | 编译校验（`:941-943`） | 节点内再验一次（`graph_execution.py:479`） |
| 端口/边/环/必填输入 | **不校验**（只字段+UUID） | — | 全量（`:894-1025`） | — |
| 对象绑定/预加载 | 注册 + 预加载结构 | 初始化对象 | 再验 access/default（`:1027-1047`） | 执行访问授权 |
| 工具绑定 | **不校验** | — | 只 prepare（`tool_bindings.py:67-105`） | 调用时 |
| 模型 profile | config 形状仅编译 | — | 资源预检（`model_package.py:56-61`） | 请求时再验 |
| 事件声明 | 结构校验（`:185-215`） | — | submit 时再验（`:827-846`） | — |
| 节点实例区分 | 唯一 `node_binding_id` | — | 按 binding 冻结（`:943-949`） | 按 `node_run_id` |

### 5.3 能力边界

- **无单节点 / 局部 / dry-run**：命令表只有整会话 `run.start`（`graph_application_contracts.py:66`），前端节点的"start"实际也是整会话 start（`workflow_operations.py:143-181`）。
- **执行范围**：`execution_roots` 已退役（`graph_contracts.py:235-242`）；普通运行范围 = 所有相连的 working/output 节点（`:873-893`），事件运行 = 事件 `target_node_ids`（`:827-846`）；`run.start` 不能自定义范围。
- `event.submit` 是第二个运行入口，复用 prepare/execute，仅会话无 active 链时可发（`graph_events.py:65-79,81-104`）；不是"推进挂起节点"。
- `run.control`：pause/resume/close/retry_archive/retry_acceptance/retry_failed_node（`graph_service.py:1769-1773`）。
- **每次 run 编译两次**（prepare `:921-924` + execute `:1469-1472`），外加可选的 `definition.inputs` 编译——这是 ④ 里最明显的重复。

---

## 6. 跨功能：为什么感觉"各自有各自逻辑"

### 6.1 四条管线对照

| 维度 | ① 创建/加载 | ② 列表/区分 | ③ 配置编辑 | ④ 运行 |
|---|---|---|---|---|
| 主入口 | 包目录 + `packages.configure` | `definition.read` + `catalog.node-types` + `session.read` | `definition.update` | `run.start` |
| 唯一身份 | `package_id@version`、`component_id@component_version` | `node_binding_id`（仅文档内唯一） | 同左 | `node_run_id`/`chain_run_id` |
| 版本概念 | 包版本、`component_version`、`host_protocol_version` | 文档 `revision`/`edit_sequence` | 同左 + `data_revision` | 会话 `revision`、run `revision`、`data_schema_version` |
| 写入方式 | 目录文件 + 启用选择 | 只读 | 整文档 CAS（+ 2 条旁路） | 只读触发 |
| 校验时机 | 注册期（声明契约） | 无校验 | 保存期（结构/策略） | prepare（编译/预检） |
| 失败表现 | 发布失败、需重启 | — | 4xx（结构/策略） | prepare 4xx 无记录 / chain diagnostic |
| 生效边界 | 重启/发布 | 即时 | 保存即生效（可调层）/ 运行才校验 | 每次 run 冻结 |

### 6.2 身份与版本的重复

- **身份 8 种**：`package_id`、`component_id@component_version`、`node_binding_id`、`workflow_definition_id`、`workflow_session_id`、`node_run_id`/`run_id`、`chain_run_id`、`event_id`/`checkpoint_id`/`object_key`。其中 `node_binding_id` 只在单文档内唯一，复制后跨文档重复——这是 ② 的根因。
- **版本 10 种**：包 `version`、`component_version`、`host_protocol_version`、文档 `schema_version`、定义 `revision`、`edit_sequence`、会话 `revision`、`data_revision`、run `revision`、`data_schema_version`/catalog `protocol_version`。每个功能只认自己那套：创建看包版本，列表看 `revision`/`edit_sequence`，配置看 `edit_sequence`+会话，运行看会话 `revision`。

### 6.3 校验的五层

| 层 | 位置 | 典型对象 | 暴露给谁 |
|---|---|---|---|
| L1 注册期 | `graph_contracts.py:490-636` | 端口类型、config_schema 合法性、capabilities/object_access 一致 | 包作者（发布时） |
| L2 保存期 | `workflow_configuration.py:390-469` | 文档结构、节点存在、策略、对象/预加载引用、CAS/锁 | 编辑者（保存时 4xx） |
| L3 读取合成 | `workflow_configuration.py:286-319` | 声明/registration/preload 复验 | 读取方 |
| L4 编译 | `graph_contracts.py:819-1048`（`definition.inputs`/`run.prepare`） | 端口/类型/必填/环/config schema/引用/ACL/包锁 | 编辑者（inputs）或运行者（prepare） |
| L5 执行 | `graph_execution.py` | 实现未变、配置再验、访问授权、输出校验 | 运行记录 |

**结论**：四条管线各自"自洽"，但跨管线没有统一顺序——①只保证"能发布"，②只保证"能读到"，③只保证"结构合法"，④才保证"能跑"。所以你看到的"执行顺序和代码各自逻辑"，准确说是**校验责任被切成五层、分散在四个功能里，而且暴露时机不一致**。

---

## 7. 若按你的四项重构：最小改动清单（接口级，未实施，待决策）

| 功能 | 最小改动 | 收益 | 风险/决策点 |
|---|---|---|---|
| ① | 节点试跑入口（单节点隔离执行，或"2 节点最小图 + `definition.validate`"）；`default_config` 纳入注册期校验；启用流程从"重启 + 全量选择"改为"增量启用/自动发现" | 创建后当场知道能不能跑；消除"保存成功、运行报错" | 单节点试跑要不要复用完整 Runtime（涉及执行器/宿主假设）；热加载涉及安全边界 |
| ② | 新增只读 `node.list`；复制自动编号；画布/历史重名时附短 id | 不需要整文档读就能列节点；同名可区分 | 标题唯一性属契约变更（存量文档风险），建议只做 UI 策略 |
| ③ | 单一"节点配置写"命令（服务端按 contract 做 schema/策略/引用校验）；字段元数据查询（可编辑性/候选引用）；保存前 dry-run | 取代整文档回传 + 前端白名单；校验前移 | 与现有 `definition.update` 语义并存需要版本化；白名单一致性 |
| ④ | 把 `run.prepare` 的编译/预检前移成 `definition.validate`；每 run 只编译一次 | 编辑期就能发现 90% 的运行期错误；减少重复编译 | 保存语义是否改变（保存即编译）需要决定 |
| 测试 | 节点/包相关测试从约 400 例/1.3 万行收敛到"5–8 个核心样例 + 明确保留的边界用例" | 降低维护成本，贴近"能跑 + I/O" | 测试清理需单独授权（AGENTS.md）；要区分"真冗余"与"历史边界证据" |

---

## 8. 与既有文档的关系

- **[Agent接入工作流-2026-11-04](../Agent接入工作流-2026-11-04/README.md)**：那边从"agent 操作面"得出"缺 `definition.validate`、缺机器可读文档契约"；本轮从"人类四个功能"得出同样的结论（§2.3、§7），两条路径互相印证。
- **[编辑同步冗杂排查-2026-11-04](../编辑同步冗杂排查-2026-11-04/01-其他交互与同步机制梳理.md)**：那里的"多通道 + 交叉锁、草稿层、节点配置多路径 + 4 个白名单"在本轮 ③ 里以"6 类写入通道 + SDK2 白名单 + patch-input 白名单"的形式再次出现；本轮确认前端没有第二套定义写路径，问题在后端/资源层。
- **[画布连线与删除排查-2026-11-04](../画布连线与删除排查-2026-11-04/README.md)**：属 ② 的渲染层（端口行/句柄几何），与实例身份无关，两轮结论不冲突。

---

## 9. 未确认点

1. 前端测试用例口径：本次计数 758，分区报告为 788（`it(`/`test(` 匹配范围不同），取"约 760–790"。
2. `packages.configure` 是否有产品 UI：文档只给出"浏览器控制台读平台接口"的做法（`WORKFLOW_PACKAGES.md:58-115`），未在运行时验证是否存在隐藏面板。
3. `prompts.group@3` 出现在策略白名单（`workflow_configuration.py:98,100`），但生产注册只见 `@2`——是否计划启用未确认。
4. "分组/映射视图"只有规划文档、无存储与实现（`docs/节点映射与分组视图/01-...md:36`）。
5. 前端扩展缺失对 `frontend.node@2` 人机交互的实际影响未验证（只读核查不启服务）。
6. 除 Pinia store 外是否还有对外宿主可用的节点列表 API：`src` 内未发现，未做运行时验证。
7. 节点/包测试的"必要性"判断基于用例意图分类，未逐条执行；"最小 5–8 例"是结构性估计，不是运行结果。

---

## 10. 需要你决定的分叉

1. **创建节点的一体化程度**：接受"3 文件 + 重启 + 全量选择"的运维形态，还是做"创建即可用"（包热加载 / 自动启用 / 权限收窄）？这决定 ① 的工作量级。
2. **节点试跑形态**：单节点隔离执行（要处理执行器/宿主/输出校验假设）还是"2 节点最小图 + `definition.validate`"（复用现有编译）？
3. **节点身份展示**：只做 UI 短 id/复制编号（低风险），还是改契约要求 title 唯一/非空（会动存量文档，高风险）？
4. **校验前移的边界**：新增 `definition.validate`（不落库，保存语义不变）还是"保存即编译"（保存语义改变，所有编辑路径都要跟进）？
5. **测试矩阵精简是否授权**：约 400 例 / 1.3 万行的包/节点测试，哪些可以并入核心样例、哪些属历史证据需保留？按 AGENTS.md，测试清理属单独授权范围。

---

## 11. 证据索引（关键锚点）

| 主题 | 位置 |
|---|---|
| 包清单/发现/发布 | `capability_packages.py:62-122,143-256,364-520`；`local_workflow_packages.py:67-107,127-193`；`workflow_host.py:52-54`；`graph_service.py:174-230` |
| 包启用 | `graph_platform.py:183-216`；`graph_application_contracts.py:78`；`WORKFLOW_PACKAGES.md:55-56,58-115,121-123` |
| 节点声明/注册校验 | `graph_contracts.py:338-392,490-636`；`text_metrics/package.py:76-112` |
| 类型目录 → 前端 | `graph_service.py:470-473`；`graph_contracts.py:394-426`；`frontend/src/domain/nodeCatalog.ts:104,118-131,154-170`；`GraphNodeConfiguration.vue:20-34,97-99` |
| 文档节点与合成 | `configuration_service.py:47-55`；`workflow_configuration.py:286-319`；`graph_contracts.py:121-149` |
| 实例身份/复制 | `graph_contracts.py:133-140`；`workflowGraph.ts:1181,1201-1207,1290-1295`；`workflow_configuration.py:530-533,545-555` |
| 运行记录身份 | `graph_records.py:32-39`；`graph_service.py:731-733`；`graph_failed_retry.py:117-126` |
| 配置/内容/绑定 | `GraphWorkbench.vue:271,285-286`；`GraphSchemaFields.vue:78`；`GraphObjectBindings.vue:1-45`；`prompt_lifecycle_nodes.py:108-160`；`tavern/lorebook_resources.py:93-118`；`model_package.py:41-61`；`frontend_node_v2.py:63-88`；`configuration_mutations.py:9-38` |
| 保存与分类 | `workflow_configuration.py:244-265,341-365,390-469`；`frontend/src/plugins/workflowFrontendSdk2.ts:45-115` |
| 运行时序/校验 | `graph_service.py:891-983,1005-1029,1407-1592`；`graph_contracts.py:819-1048`；`graph_execution.py:319-627` |
| 测试规模 | 见附录 A（统计口径与文件清单） |
