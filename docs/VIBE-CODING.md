# Vibe Coding 手册

本指南面向使用代码助手（harness）的开发者，也可供用户了解工作流能做什么。它解释当前架构和受支持的扩展入口，不是新的产品授权、迁移计划或无限施工许可；根目录 [AGENTS.md](../AGENTS.md) 仍是协作、权限和安全规则的权威。

项目当前是**有骨架的毛坯开发版**：模块边界和若干端到端路径已建立，但不少能力仍简陋、受限或未验收。本文只记录已实现且有现行文档/示例支持的事实；目标版本 `0.5.0` 尚未成为项目元数据版本，后端和前端元数据仍为 `0.2.0`。首次全量记录有后端 3 项、前端 2 项失败，之后做过定向修复和验证，但没有重新跑全量；不能称为最终验收通过或发布完成。参见 [166 号候选审阅报告](中期检查2/166-develop与main主要更改审阅及0.5.0候选更新报告-2026-10-22.md)。

## 推荐配合 Harness

当前推荐配合能够读取仓库、调用工具的 harness 进行**使用和开发**。它是项目外的模型协作环境，不是工作流中的 Agent 节点，也不代表项目内置了某个 harness。使用时可帮助理解功能、配置工作流和定位错误；开发时可沿现有架构制作包、节点与工具。手动工作台仍是可用入口。

让用户从实际目标开始，例如“我想让 Agent 改写文本并保存多轮上下文”，由助手先解释可行路径和限制，再按已确认需求落实。推荐这种协作方式不扩大操作权限：真实服务、数据、模型调用和 Git 发布仍须按任务范围获得授权。

## 先和用户对齐

不要假设用户熟悉仓库、能启动项目、会操作命令行，或已经授权真实服务、数据库、模型调用。先确认想要的用户效果，再讨论实现是否可行、由谁负责、会有哪些限制。源码地图和密集的函数清单留到对方确实需要时再给。

可以从这些问题开始：

- 希望用户最终能做成什么？给一个实际输入和期望结果即可。
- 哪些人会使用，入口是在工作流画布、Agent 工具还是管理界面？
- 数据从哪里来、要保存多久、是否需要跨节点或跨会话共享？
- 是否确实需要文件、网络、外部服务或模型？没有明确需求时先不引入这些能力。
- 当前不清楚项目或不便操作时，想先看可行性说明、最小示例，还是只想让助手代为实现？

然后用面向效果的语言说明：什么可以沿用、用户会看到什么、当前版本有哪些硬限制、哪些决定尚未确认。遇到新数据权限、事务边界、生命周期、恢复策略、业务规则或新责任归属，先向用户说明影响并询问，不自行补齐架构。报错、未知状态和原请求待核实时，优先按现有契约诊断和只读核实；不要自动重放不明请求或发明兜底。

## 架构轮廓

### 项目层与工作流层

**项目层登记子能力，不展开登记工作流内部所有部件。** 项目侧记录子能力身份和来源、调用方如何取得管理/执行角色、共享存储环境、生命周期、公开契约及跨域依赖。它不变成所有节点、类型、工具的全局组件清单。工作流包、节点、数据类型和工作流配置属于工作流域自己的规则与注册表。

项目使用同一 SQLite 主库路径和按操作取得的存储环境。共享“存储环境”不代表各域可以跨域读写对方表：工作流、供应商/模型预设、内容各自管理自己的数据、契约和事务。项目 Host 向工作流传递已校验的窄依赖，不替工作流解释图或处理其业务数据。

正常的 [WorkflowHost](../backend/src/phase1_agent/workflow_host.py) 负责统一装配并懒加载管理/执行服务。Host 创建并持有的服务由 Host 关闭；借用方不应擅自代关。`service.close()` 会等待执行器并关闭服务持有的进程内资源，正在执行的任务可能按现有规则完成结算；它不等同于某个业务 `run.close()`。仅凭 Host 关闭，不能声称持久 prepared 链已关闭或活动内核可自动恢复。

### 工作流运行和定义

一个固定修订的工作流文档包含节点身份与配置、内容边、控制依赖、对象绑定、准确包锁，以及可选的会话预加载声明。保存配置、选择包、创建会话和运行是不同操作；包启用不会自己改工作流或重置会话值。

定义与实现分开看：

- `NodeDefinition` 声明节点身份/版本、配置 schema、输入输出端口、对象访问、引用、执行类别和检查点配置白名单；注册的 executor 才是实现。
- `DataTypeDefinition` 声明数据 scope、类型身份/版本、schema、验证及明确支持的类型钩子；`TypeRegistry` 是同一准确版本类型目录。
- `ObjectBinding` 将稳定对象键绑定到准确 session 类型和访问权限；运行期间的当前业务值由会话数据层持有，而不是由节点配置充当当前值存储。工作流配置仍可声明初值，例如变量注册节点的初值配置或 `session_preloads` 的 `value`。
- `GraphCompiler` 校验固定图文档、配置、节点引用、端口类型、对象访问与依赖，并形成图内拓扑执行计划。
- `WorkflowDefinitionCatalog` 是对可信注册的只读投影，不另存第二份定义权威。一次运行使用同一已冻结的 `RunBasis`，不会在调用中用新 registry 替换运行依据。

不要混淆以下版本：

| 身份 | 例子 | 表达的是什么 |
| --- | --- | --- |
| 工作流包版本 | `example.text-metrics@1.0.0` | 一次包发布及其完整依赖 |
| 节点/组件版本 | `example.text-metrics@1` | 一个节点实现契约的准确版本 |
| 数据类型版本 | `content / TEXT / 2` 或 `session / example.session-state / 1` | 由 scope、type id 和 schema version 共同指定的数据契约 |
| 文档/值封套版本 | 工作流 `schema_version`、内容封套 `schema_version` | 文档形状或值编码，不是包版本 |

查找和执行都要求准确身份，不隐式选择“最新”。节点端口声明的数据类型版本也独立于节点版本。

## 数据归属与执行

### 四类状态不要混用

1. **工作流配置**：节点配置、连接、对象绑定和预加载初值属于工作流固定修订，按正式配置应用/确认/派生流程保存。
2. **业务值**：实际对象值属于会话，经现有会话对象接口读写；由 TypeRegistry 校验，遵循绑定权限、事务与 CAS（比较并交换）约束。节点不得私建业务数据库或绕过这些接口。
3. **节点私有运行态**：当前存在 `private_state_schema/default` 及宿主保存机制，属于受 schema 约束、由运行/数据管理维护的运行态。它不是任意业务数据仓库，不因此获得文件、数据库或跨会话访问权；本指南不授权新建或扩张这类状态。
4. **输出与执行事实**：节点按声明端口产生有类型的内容，运行历史、候选结果和检查点按已有流程接纳。输出不是另一种可随意覆盖的全局业务对象。

节点执行类别为 `working` 或 `non_working`。普通整图执行把所有通过数据边或控制边连接的 `working` 节点作为候选目标，并要求这些目标中至少有一个已连接输出节点；孤立的 `working` 节点不执行，连接的非输出节点也可能是计划目标。这不是只从输出节点向上裁剪依赖。普通工作图依赖 `non_working` 节点会被拒绝。`non_working` 不表示“没有行为”，而表示不由普通整图自动执行；这类声明只由专门交互或声明入口消费，例如显式工具绑定准备。不要把“节点已在目录里”当成“它一定会在本轮执行”。

### 会话预加载

工作流的“会话预加载数据”是**新会话初始化配置**，不是工作流级的当前值表：

- 列表项只保存 `{object_key, value}`；准确 `type_id`、`schema_version`、对象 `shared/private` 归属和 readers/writers 由同一工作流文档中的 `object_bindings` 保存。
- 普通创建时，先建基础对象，再由会话域一次性初始化列表值；与会话 seed 在原事务内提交。失败由原事务处理，不留下半初始化会话。
- 修改列表只影响之后按新固定配置创建的会话，不持续同步、不重置已有会话。复制工作流可复制配置列表，但没有源会话和运行历史。
- 检查点/分叉按所选历史还原值，不重新应用配置初值；fork 创建新目标会话，不覆盖旧会话。
- 当前普通预加载支持准确 `workflow.variable@1` 和由可信包注册的普通结构类型，且要求同一 `TypeRegistry` 中准确可用、声明允许检查点、完整合法初值。普通结构不支持自定义 validator、迁移、写钩子、外部引用或 artifact 等特殊语义；这不是交互式类型 CRUD。
- 未赋值变量与已赋值的空字符串、`0`、`false` 不同。初始化项不能同时从 binding default 或 registrar 获得第二个初值来源。

详细表单及规则见 [SESSION_PRELOADS.md](SESSION_PRELOADS.md)。节点读写仍经原有 session object API，预加载不会自动授予权限。

### 检查点与图内顺序

检查点不是整库快照。只有当前契约明确准入的对象类型/声明会被收集：例如现有变量、聊天/上下文路径，以及显式声明并满足条件的普通预加载类型。`checkpoint_admission` 不是“任意对象都收进去”的开关；恢复还校验目标文档、准确类型、绑定键集合和初始 CAS。完整/差分物化遵循现有检查点规则，不能靠放宽白名单绕开类型/权限。

节点配置也只采集 `checkpoint_save.config_paths` 白名单中的固定部分，不能假定全部配置、任意节点运行态、外部资源或凭据都能随检查点恢复。

每轮内部的图执行顺序由图边、控制依赖与拓扑计划决定，和整轮大阶段不是一个顺序 API。节点之间的图内依赖不会因全轮步骤编号而消失。

### 一轮运行的大顺序

当前实现只声明并消费这五个内置大步骤：

| 顺序 | 步骤 | 用途 |
| --- | --- | --- |
| `0100` | 本轮准备 | 固定并校验本轮依据、准备运行所需上下文 |
| `1000` | 整图执行 | 执行本轮图计划及其节点依赖 |
| `2000` | 业务结算 | 按现有结算事务接纳成功结果、提交及检查点事实 |
| `2100` | 提交后通知 | 发送通知并单独记录通知诊断 |
| `2200` | 结束释放 | 按既有生命周期规则释放本轮进程内资源 |

同号步骤明确报错，不靠输入顺序或覆盖来修复。编号只是当前内置流程，不是给外部插件排序的扩展 API；当前也没有对外开放的阶段插入协议。节点结果被接纳不代表整张图是一个原子事务；尤其不要因后续结算/通知错误就假设之前的模型或工具调用会自动重放。`service.close()` 不能替代 `run.close()`，也不能宣称可自动恢复活动内核。

对普通 Agent 工具调用的参数校验错误，当前整批工具调用零执行并向 Agent 返回订正反馈；对结果未知或原请求待核实，按原请求只读核实，不自动重发。参数校验、工具执行失败和执行后的结果接纳是不同环节，不能全部退回为重新调用 Agent。具体工具错误契约以实际执行路径为准，不要把一次节点接纳解释为整轮图已原子提交。

## 本地工作流包和节点

### 载入边界

扩展节点使用工作流包机制，不是项目子能力目录。开发者手工下载并解压到主 SQLite 所在目录下的 `packages/workflow/`，也可由受支持的启动配置显式指定来源目录。每个直属子目录是一份准确包版本，外层 `openagent-package.json` 声明入口和 manifest；包目录名本身不是身份。

放置只代表可发现，不会自动启用。应用先读取声明，再对完整准确选择及其依赖加载入口和注册导出；工作流文档保存准确 `package_lock`，运行再消费冻结的 `RunBasis`。包版本、节点版本和数据类型版本分别核对。放置或修改包后，须经获准的正常流程重建/重开 Host 取得新来源快照；页面刷新不是磁盘热扫描。

坏的未选来源可显示诊断，不阻断正常包选择；所选候选在校验或注册阶段失败时，整组拒绝且不部分发布。若数据库已提交选择、随后内存发布失败，应保留 `package_publication` 中的已提交事实，不伪称回滚或自动补偿；按 [包手册](WORKFLOW_PACKAGES.md) 核实后经正常关闭并重建 Host。

这是**手工、本地、可信 Python 代码**路径：不是网络市场、不是联网安装器、不热更新，也没有 Python 沙箱或导入副作用回滚。不自动安装缺失依赖；不要把包目录插入全局 `sys.path`。专用前端实现须是可信、已编译的宿主配对代码，解压目录里的 JS 不会自动加载。

完整放置/准确启用与失败诊断步骤见 [WORKFLOW_PACKAGES.md](WORKFLOW_PACKAGES.md)。一个包声明的最小结构如下（具体字段以手册和现有示例为准）：

```text
my-package-1.0.0/
  openagent-package.json
  my_package/
    __init__.py
    package.py       # create_package() + register(host)
    nodes.py         # 节点声明和纯业务实现
```

实现应保持本包相对导入、manifest 与实际注册身份一致、依赖和导出准确、节点配置可校验。普通节点不因注册而自动成为 Agent 工具。

### 扩展步骤

1. **先写用户效果**：输入是什么、输出是什么、是否需要状态、是否由 Agent 调用。拿不准需求时先回到上节沟通。
2. **选最窄的现有能力**：纯转换优先做普通工作节点；跨节点/跨轮业务值通过 session object API；只有确需模型按 schema 调用的能力才做显式工具绑定。
3. **看同类现有示例和测试**：先读 `text-metrics` 或 `numeric-clamp`，再读对应的本地包、数据预加载或工具绑定测试。不要先大批搬历史计划。
4. **确定准确版本**：为包分配 `package_id/version`，节点分配 `component_id/component_version`，类型分配 `scope/type_id/schema_version`；各自独立递增。旧工作流固定引用不应被悄悄覆盖。
5. **声明数据，再写节点**：已有类型直接使用同一 TypeRegistry；新普通 session 结构由包注册 `DataTypeDefinition`，再供配置/节点使用。数据读写通过 `ObjectBinding`、明确 ACL 和既有对象接口；不能写 SQL 代替业务 API。普通预加载还须满足上面的准入条件。
6. **声明节点契约**：定义配置 schema/default、准确输入输出端口和数据 schema version、执行类别、对象访问/节点引用。executor 只做声明允许的工作，返回值要符合输出端口类型。
7. **如需 Agent 工具，显式适配**：用 `tool_binding` 注册加载节点，使用 `config_node_references` 指向准确 Agent 和提示材料输出。纯计算工具通过 `PureToolBindingAdapter` 提供 `normalize/call/result`；它只接收 JSON 参数，映射到目标节点的配置/输入，再将目标输出投影为 JSON。既有变量工具沿原变量适配、ACL、逐次写入接纳和独立重读链。
8. **提供最小说明与验收**：包内 README 写清放置路径、准确选择、节点使用连接和实际限制；新增测试覆盖声明/坏配置/执行结果及必要的存储隔离。不要声称测试覆盖范围以外的可靠性。

Agent 工具首版限制严格：必填扁平标量参数、JSON 对象、拒绝额外字段；纯计算目标为同步、无业务状态/对象/服务/资源/副作用的工作节点。普通节点不会自动变成 Agent 工具。当前不开放任意文件/网络访问、Host 服务、任意对象 CRUD、删除、多对象写、特殊结算或以 `executor_ref` 作工具目标。包是可信 Python，以上约束是宿主适配接口限制，不是对任意 Python 副作用的沙箱保证。

新增同类工具应在包中提供目标、声明、适配器和提示投影，不在 Agent/runtime 核心增加具体工具名硬编码分支。

## 推荐完整例子：Text Metrics

推荐从 [text-metrics-1.0.0](../examples/workflow-packages/text-metrics-1.0.0/README.md) 开始。它不需第三方依赖，既提供可普通连线执行的 `example.text-metrics@1` 节点，也提供显式的 `example.text-metrics-tools@1` 加载节点，将 `text_metrics(text, include_spaces)` 绑定到一个 `agents.execute@10` Agent；结果为包含 `characters` 和 `lines` 的 JSON 对象。

按以下顺序演练：

1. 读 [外层声明](../examples/workflow-packages/text-metrics-1.0.0/openagent-package.json) 和 [实现](../examples/workflow-packages/text-metrics-1.0.0/text_metrics/package.py)，区分包 `1.0.0`、两个组件版本 `1`、依赖包的准确版本。
2. 将整个目录放到 `<主 SQLite 目录>/packages/workflow/text-metrics-1.0.0/`，经正常 Host 生命周期取得来源；查看平台包发现诊断，确认未选包时不会自动进入执行目录。
3. 核实 `example.text-metrics@1.0.0` 及准确依赖，再把它加入**完整**选择集合，保留项目当前其他有效准确选择。完整 API 步骤见 [WORKFLOW_PACKAGES.md](WORKFLOW_PACKAGES.md)，不能把 `packages.configure` 当增量补丁。
4. 刷新节点目录，验证普通节点可选；再创建工具加载节点，把 `agent_node_id` 设为目标 Agent 实例，把 `materials` 输出连到 context assembly 的 `materials` 输入。它会把工具说明/参数 schema 加入该 Agent 提示材料。
5. 通过正式配置保存接纳后，独立重读正式配置和固定修订，确认节点 ID/version、连接、配置及包锁一致；不要以画布当前显示代替持久证据。
6. 在隔离测试或获准的离线模型替身环境验证纯计算工具的有效调用和结果，再用错误参数验证整批零执行与订正反馈；本例没有对象 ACL，不做“缺少对象访问”测试。变量工具的 ACL 应另由既有变量工具测试验证。不要为这份示例直接调用真实上游模型。
7. 如测试范围包含持久化，使用隔离库按授权正常关闭并重新构造 Host，重读包选择、固定工作流和既有运行结果；不重放旧业务请求。

### 验收清单

- [ ] 包目录、声明 JSON、入口工厂、返回 manifest、注册导出完全一致；包内导入来源明确。
- [ ] 包发现、准确依赖、完整选择和工作流 `package_lock` 都可独立核对；坏项有诊断，不静默忽略或部分启用。
- [ ] 注册节点在准确节点版本目录中可见；配置、端口和 content 类型版本均校验。
- [ ] 正式配置应用后独立重读，节点配置、节点身份、连线、固定修订和包锁均一致。
- [ ] 普通节点执行的输出符合准确 schema；工具加载节点只绑定它声明的 Agent，提示说明与调用来自同一运行依据。
- [ ] 有效 Agent 参数只调用被绑定目标；无效参数整批零执行并给出反馈。工具加载不绕过准确 Agent 绑定；如包含变量工具，另核对其 ACL 和逐次接纳，不把对象权限矩阵套到纯计算工具。
- [ ] 需要状态时，状态使用现有 session object 及权限/CAS 接口；新会话预加载只初始化一次，已有会话和历史分叉不被新初值覆盖。
- [ ] 检查点只包含现行规则准入的对象；fork 是新目标而非覆盖旧会话。
- [ ] 验证使用隔离数据与受控 transport；报告实际执行范围，不把定向测试写成全量、浏览器/真实模型验收或发布完成。

## 最小代码地图

按问题查少数入口即可，不必通读整个后端：

| 主题 | 入口 |
| --- | --- |
| 项目角色、共享环境、依赖声明 | [project_support.py](../backend/src/phase1_agent/project_support.py)、[workflow_host.py](../backend/src/phase1_agent/workflow_host.py)、[workflow_capability.py](../backend/src/phase1_agent/workflow_capability.py) |
| 本地包与注册 | [capability_packages.py](../backend/src/phase1_agent/capability_packages.py)、[local_workflow_packages.py](../backend/src/phase1_agent/local_workflow_packages.py)、[WORKFLOW_PACKAGES.md](WORKFLOW_PACKAGES.md) |
| 节点/图契约、类型、对象绑定 | [graph_contracts.py](../backend/src/phase1_agent/graph_contracts.py)、[host_sdk.py](../backend/src/phase1_agent/host_sdk.py)、[workflow_definitions.py](../backend/src/phase1_agent/workflow_definitions.py) |
| 一轮顺序与执行依据 | [workflow_run_order.py](../backend/src/phase1_agent/workflow_run_order.py)、[run_basis.py](../backend/src/phase1_agent/run_basis.py)、[graph_execution.py](../backend/src/phase1_agent/graph_execution.py) |
| 会话值、预加载、检查点 | [session_preloads.py](../backend/src/phase1_agent/session_preloads.py)、[session_objects.py](../backend/src/phase1_agent/session_objects.py)、[checkpoint_collection.py](../backend/src/phase1_agent/checkpoint_collection.py)、[checkpoint_loading.py](../backend/src/phase1_agent/checkpoint_loading.py) |
| Agent 工具边界 | [tool_binding_contract.py](../backend/src/phase1_agent/tool_binding_contract.py)、[tool_batch_validation.py](../backend/src/phase1_agent/tool_batch_validation.py)、[generic tool tests](../backend/tests/test_generic_tool_bindings.py) |

可作为扩展检查起点的现有验证：

- 普通磁盘包集成：[test_local_package_integration.py](../backend/tests/test_local_package_integration.py)
- 包发现/入口：[test_local_workflow_packages.py](../backend/tests/test_local_workflow_packages.py)
- Agent 通用工具：[test_generic_tool_bindings.py](../backend/tests/test_generic_tool_bindings.py)
- 会话预加载：[test_session_preloads.py](../backend/tests/test_session_preloads.py)、[test_preloaded_package_integration.py](../backend/tests/test_preloaded_package_integration.py)

测试是现行实现的局部证据，不是新架构或额外权限的授权。创建服务、启动/关闭既有服务、访问真实数据库/凭据、安装依赖、调用真实模型、迁移业务数据及提交/合并/推送仍按根 [AGENTS.md](../AGENTS.md) 单独授权；本指南本身不改变这些边界。
