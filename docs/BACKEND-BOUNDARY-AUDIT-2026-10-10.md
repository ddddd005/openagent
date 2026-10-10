# 后端模块职责与交叉依赖审查

日期：2026-10-10，Asia/Shanghai。源码基线：`447fa552b24a6493f450d6ff2a67f2f9fca97432`；本地 `main`、`develop` 均指向该提交。审查在 `develop` 工作区进行，已有未提交故障记录保留。

**结论：后端已有模块化机制，但职责边界没有完全闭合。** HTTP、应用分发、声明式能力、模型传输和执行内核有可识别的分工；配置所有权、通用宿主、Artifact 证明、Context/Agent 依赖和错误恢复仍有跨层耦合。不能将现状概括成完全没有模块化，也不能因文件已经拆开就认为职责已经独立。

本轮采用三并发：主代理审查全包依赖、应用/HTTP/消费者边界，两名子代理分别审查供应商/模型配置和运行时/持久化/Context。只新增审查文档与索引，没有实施产品修复、改用户数据、启动正式/开发服务、调用真实上游、重放用户工具请求、提交或推送。

后续实施状态：同日用户授权按规则三并发订正，配置权威、Key 单动作生效、中立凭据/诊断、输入声明及恢复子范围已在 `develop` 实现。详见 [实施与验收记录](MODEL-CONFIG-CORRECTION-2026-10-10.md)。下文统计、路径行号及“未修复”是 `447fa55` 审查时的事实，不作为当前源码统计；BND-06/07/08/09/12 深层范围仍待处理，其他项也不因此外推全部验收完成。

2026-10-11 的执行方向以[核心规则](中期检查/06-工作流核心与修改规则-2026-10-11.md)、[整改计划](中期检查/08-后续整改计划-2026-10-11.md)和[清理审批](中期检查/09-清理候选与审批清单-2026-10-11.md)为准；本页旧冻结、恢复及整改顺序建议只作历史参考。实际勘察工作树与文档提交边界见[代码基线](中期检查/10-整改前代码基线-2026-10-11.md)，本次不重跑或改写下文历史验证结果。

## 1. 统计口径与结果

范围为 `backend/src/phase1_agent/**/*.py`，包括包的 `__init__.py`；不包含测试、JS、构建产物、第三方源码和 `.local`。通过 Python AST 解析源码，不通过导入产品模块统计依赖。

| 项目 | 结果 | 解释 |
| --- | --- | --- |
| Python 模块 | 116 | 按源码文件对应模块计数 |
| 物理行数 | 28,711 | 包括空行、注释和文档字符串，不是有效代码行 |
| 内部依赖边 | 569 | 按来源模块、目标模块去重；包括函数内延迟导入 |
| 内部导入引用站点 | 613 | 保留同一依赖在不同位置重复导入的站点 |
| 函数内延迟导入站点 | 139 | 是上述 613 的子集，不是 139 个独立问题 |
| 顶层导入的循环依赖组 | 0 | 只统计模块初始化层的内部导入 |
| 包含延迟导入的循环依赖组 | 7 | 按至少两个模块的强连通分量计数，涉及 21 个模块 |
| 工作流总协调类 | 10 个 mixin、155 个方法定义 | `GraphWorkflowService` 本体 44 个，其余来自 mixin；不是 155 个公开 API |
| 去重整改/审查项 | 13 | P1 两项、P2 九项、P3 两项；不等于 13 个已复现功能 bug |

依赖扫描解析显式 `import` / `from ... import ...`，区分顶层与函数内导入；不计纯类型检查导入。未覆盖 `importlib` 动态插件装载、所有回调调用环或完整锁顺序，不能用“顶层无环”证明全系统无循环，也不能用“存在依赖环”声称发生无限递归。

`GraphWorkflowService` 有 27 个内部模块依赖目标。源码最大的 `contract_graph.py` 为 1,619 行，但主要承担契约校验，不因文件较大直接登记耦合问题。另有 12 个非 `storage.py` / `credential_store.py` 模块接触 `_connection`，共 56 个属性所在行；其中不少是合理的 SQLite 仓库实现，不将这些访问全部算成违规。

## 2. 优先发现

以下路径相对于仓库根目录，省略目录的 Python 文件均位于 `backend/src/phase1_agent/`，行号以本次源码基线为准。P1 表示直接影响已确认的使用逻辑或恢复能力；P2 表示应整改的职责/故障隔离风险；P3 表示较低优先级的结构维护问题。

### BND-01 / P1：模型参数和容量的权威仍在工作流节点

- 定位：`backend/src/phase1_agent/model_package.py:39`、`:58`、`:73`；`model_host_service.py:58`；`model_service.py:206`、`:504`。
- 证据：`models.source` 的配置强制包含 `reference`、`parameters`、`capacity`，型号、输出限制、思考设置及上下文预算都随工作流保存，并原样交给模型宿主建立调用配置。底层供应商资源主要保存连接设置，没有当前可用的独立模型配置实体承担这些字段。
- 影响：工作流编辑同时成为底层模型配置编辑；型号为空或容量关系错误会阻止工作流编译/执行，与用户确认的“只引用已配置能力”原则冲突。
- 限定：未发现工作流执行主动写回供应商资源的路径；节点通过宿主调用，不是节点直接发 HTTP。
- 方向：底层配置服务持有可独立管理的模型配置；工作流引用明确身份，执行前冻结所引用配置，不再拥有底层配置字段。对应 CFG-07。

### BND-02 / P1：发送前失败被合并，丢失可恢复原因

- 定位：`backend/src/phase1_agent/model_service.py:521`、`:524`、`:530`；`backend/tests/test_model_package.py:188`。
- 证据：发送前捕获全部异常，事实只留下 `not_dispatched` 和 `attempt_id`，最终统一抛出 `model_not_dispatched`。供应商停用、凭据变更、凭据缺失及禁止的重试配置等不同原因无法沿此路径保留。
- 影响：用户不知道应启用供应商、重新绑定凭据还是处理配置；相同笼统诊断不能指导恢复。既有参数化测试反而断言三种不同失败都得到这个错误。
- 方向：保留经过允许列表脱敏的原因码、失败步骤和配置身份，区分用户恢复措施；不暴露 Key 或原始异常文本，不放宽发送前守卫，不自动重放。对应 CFG-09。

### BND-03 / P2：底层配置与工作流宿主共用生命周期和私有状态

- 定位：`backend/src/phase1_agent/graph_service.py:73`、`:151`、`:152`、`:155`；`graph_provider_host.py:14`、`:29`、`:51`；`graph_platform.py:69`。
- 证据：工作流总服务继承供应商管理 mixin，直接创建凭据库、注入模型专用选项。供应商状态、Key 保存、模型发现依赖其 `_lock`、`_check_open`、`_credentials` 和资源方法。更广的 10 个 mixin 也通过同一个隐式 `self` 分享运行缓存、对象、历史与包状态，并非独立服务。
- 影响：仅处理底层配置也依赖工作流服务初始化、数据库 lease 和包加载；一个领域的维护会触及总宿主生命周期。比如 `GraphPlatform` 引用 31 种私有 `self` 成员，方法签名无法体现这些依赖。
- 方向：由应用组合层建立配置/凭据服务与工作流宿主，使用显式接口连接；不是单纯拆文件或改类名。保留现有共享事务与并发保护需要的协调。

### BND-04 / P2：凭据基础设施反向依赖模型运行服务

- 定位：`backend/src/phase1_agent/credential_store.py:14`；`model_service.py:31`；`model_contract.py:65`。
- 证据：`credential_store -> model_service -> model_contract -> credential_store`。通用凭据 lease/broker 定义在模型运行服务，模型契约又从凭据存储取引用格式校验，最后一条使用延迟导入。
- 影响：验证凭据引用或复用凭据基础设施会拉入模型运行宿主和传输相关依赖；基础设施和业务宿主不能独立演进。
- 方向：凭据引用契约、lease/broker 接口下沉至中立模块，存储与模型宿主分别实现/消费。确认的是模块依赖环，不是已复现运行时递归。

### BND-05 / P2：图核心契约与具体模型能力形成反向依赖

- 定位：`backend/src/phase1_agent/gemini_capabilities.py:5`；`graph_contracts.py:303`、`:307`；`model_selection.py:13`；`model_configuration.py:14`；`gemini_adapter.py:20`。
- 证据：`graph_contracts -> model_selection -> model_configuration -> gemini_capabilities -> graph_contracts`。图内置的旧 `MODEL_RESOURCE` 校验认识模型 binding，供应商能力和适配器仅为取得 `require` 也依赖图契约。
- 影响：通用图契约绑定模型领域，模型/供应商纯校验反过来绑定图框架；一个底层校验改动会在两侧传播。
- 方向：通用错误/断言下沉，领域数据校验由注册契约提供；退出旧绑定时按精确使用范围清理，不放宽当前模型协议校验。对应 CFG-08 的已定位依赖部分。

### BND-06 / P2：单次图事务绑定整个数据库的健康状态

- 定位：`backend/src/phase1_agent/graph_store.py:61`；`graph_records.py:223`；`graph_service.py:194`。
- 证据：`_change -> atomic -> callback -> read_bundle(include_graph=True) -> validate_graph_bundle`，每笔新图事务校验全库图记录，而非此次操作涉及的闭包。
- 影响：一个无关会话/历史损坏也能阻断别的图事务；结算边界反复扫描全历史，耗时随全库增长。这里确认故障隔离风险，未做规模基准，不给出未经测量的性能结论。
- 隔离实验：内存库中，不读任何记录、只返回 `{"saved": true}` 的 callback 原本成功；插入一条属于其他会话的损坏 graph session 记录后，新 key 的同类事务报 `storage_contract_violation` 并回滚。没有接触产品库，也不能据此认定是此次 API 故障根因。
- 方向：事务校验相关记录及必要授权引用闭包；全库体检/隔离独立进行。不得直接删除全库校验而失去完整性保护。

### BND-07 / P2：Artifact 生产者事实解析与授权所有权分散

- 定位：`backend/src/phase1_agent/session_objects.py:185`、`:194`、`:227`；`graph_public.py:356`、`:365`；`graph_information.py:269`；`graph_runtime_host.py:234`、`:288`。
- 证据：对象仓库除 CAS/权限/版本外，还追溯图历史、输出、运行与定义，取得生产者配置。公开读取和信息读取直接调用其私有 `_resolve_write_artifact`；运行时另有 Artifact 解析与闭包代码。`storage -> session_objects -> graph_store -> storage` 也体现了持久层与图语义双向依赖。
- 影响：修改生产者/历史协议需要同步改对象仓库、公开消费和运行时；共享事实解析依赖对象写入的私有助手。
- 方向：一致的 Artifact 生产者事实读取接口，加上各入口显式授权策略。不同入口拥有不同范围是合理的，不可为了共用代码合并权限；本轮未发现现有越权漏洞。

### BND-08 / P2：通用平台核心承载 Context 业务证明

- 定位：`backend/src/phase1_agent/graph_platform.py:136`、`:138`、`:145`、`:180`。
- 证据：`adopt_context_view` 直接导入原生 Context 采用/证明算法，硬编码 `context.merge@4`、业务绑定，并穿透对象仓库解析 Artifact。
- 影响：本应属于 Context 能力的业务规则留在通用平台；增加上下文实现或版本也要修改宿主核心。
- 方向：业务证明通过注册能力/服务提供，宿主继续负责稳定性、授权、事务与提交，不能让能力包自行提交破坏原子结算。

### BND-09 / P2：Context 依赖 Agent 执行实现，包依赖未表达

- 定位：`backend/src/phase1_agent/context_v4_nodes.py:56`、`:76`、`:136`；`context_v4.py:303`；`context_package.py:32`。
- 证据：Context merge 导入 `agent_executor.resolve_input/validate_execution_update`；持久证明硬编码 `agents.execute@4/@8` 和 prompt/model/policy 输入。Context manifest 声明 content/prompts 依赖，却没有表达这条执行实现依赖。
- 影响：Context 表面消费更新契约，实际依赖 Agent 执行文件和分散的版本白名单；Agent 输入/receipt 变化不能独立演进。
- 方向：中立的“已接受执行更新证明”契约或证明服务。官方生产者限制是安全策略，不能删校验换取解耦；本轮未复现当前官方 Agent 场景失败。

### BND-10 / P2：消费者输入声明与整图可执行性混在一起

- 定位：`backend/src/phase1_agent/graph_public.py:115`；`graph_input_declarations.py:44`；`tavern/frontend/adapter.js:17`。
- 证据：消费者先编译整个执行图；失败就返回空输入和诊断。编辑入口已能独立保留各节点输入声明，但消费者没有区分“声明存在”和“当前不能运行”。酒馆首先按空输入判断数量，给出缺少唯一 TEXT 的提示。
- 只读实验：有唯一 TEXT 声明、但输出未连线的图，编辑投影输入数为 1，消费者输入数为 0，真实诊断是 `graph_required_input_missing`。后端诊断没有完全丢失，但数据形状让调用方容易误判。
- 方向：分别表示输入声明、执行所需输入和可执行诊断；消费者优先显示真实阻断原因，不放宽真正的输入数量/类型规则。对应已登记 CFG-06，不作为另一项新增缺陷重复计数。

### BND-11 / P2：HTTP 兜底把所有未知异常都标成存储问题

- 定位：`backend/src/phase1_agent/server.py:161`、`:373`、`:382`。
- 证据：通用 `except Exception` 对任意 graph 请求统一返回 `storage_error`；此处未记录异常/追踪身份，HTTP 日志方法也为空。包加载、配置发现或程序异常都可能被标为存储故障。
- 影响：层级信息和实际失败步骤消失，用户误判原因，开发者也缺少此入口的服务端关联证据。并非所有其他模块均无日志，本结论限定该兜底。
- 方向：未知异常保留中性内部错误分类，服务端保存脱敏关联诊断，用户端给步骤、关联编号和安全恢复入口；真实原因可识别时由责任模块输出。不能把未知结果一律改成确定失败或授权重发。对应 CFG-09。

### BND-12 / P3：共享契约、执行辅助与包注册之间仍有反向环

- 定位：`content_contracts.py:88`、`:166`、`:208`，`prompt_contract.py:14`，`prompt_lifecycle.py:10`、`:155`；`prompt_config.py:213`、`preparation_program.py:15`、`prompt_values.py:18`；`context_package.py:22`、`context_v4_nodes.py:11`；`context_v4.py:130`、`:131`、`context_update.py:278`、`context_prompt_v6.py:145`、`:163`。
- 证据：基础 content 定义同时委托具体 prompt 校验/组装和生命周期注册；prompt 配置校验依赖准备程序，准备程序的值契约又依赖 prompt 配置；Context 节点引用自身包注册入口里的私有输入助手，view/update/assembly 互相依赖。
- 影响：纯 shape 校验、组装/回放和包注册边界不闭合，独立复用与变更传播难以控制；属于结构风险，未据此确认调用递归或当前业务失败。
- 方向：提取真正共享的中立契约/输入助手，注册层单向装配具体实现；保留必要的验证委托，不单纯为清零环数拆文件。

### BND-13 / P3：退役模型配置路径仍被现行契约牵连

- 定位：`backend/src/phase1_agent/model_configuration.py:18`、`:22`、`:114`；`model_selection.py:68`、`:92`、`:120`；`graph_contracts.py:307`。
- 证据：保留固定两节点 binding、`frontend:main-test`、旧配置记录和选择计划解析。仓库检索未找到旧配置解析入口的当前调用者，但旧 binding 验证仍由通用图契约引用，当前包也复用其中的默认供应商常量。
- 影响：文件同时包含旧配置路线与仍在用的辅助，误导后续开发对配置权威的判断，并延续 BND-05 的反向依赖。
- 方向：先确认历史读取/类型使用范围，将当前辅助归回明确模块，再处理真正退役代码；不能整个文件直接删除，也不能把它描述成两条现行运行路线。

## 3. 七组循环依赖

`->` 仅表示模块依赖，不表示调用无限递归。下列每组至少包含一条函数内延迟导入，顶层子图本轮无环。

| 组 | 已确认的环或强连通成员 | 代表定位 | 对应发现 |
| --- | --- | --- | --- |
| 1 | `graph_contracts -> model_selection -> model_configuration -> gemini_capabilities -> graph_contracts` | `graph_contracts.py:307`、`gemini_capabilities.py:5` | BND-05 |
| 2 | `credential_store -> model_service -> model_contract -> credential_store` | `credential_store.py:14`、`model_contract.py:65` | BND-04 |
| 3 | `storage -> session_objects -> graph_store -> storage` | `storage.py:79`、`session_objects.py:186`、`graph_store.py:12` | BND-07 |
| 4 | `content_contracts`、`prompt_contract`、`prompt_lifecycle` | `content_contracts.py:88`、`:208`、`prompt_lifecycle.py:155` | BND-12 |
| 5 | `prompt_config -> preparation_program -> prompt_values -> prompt_config` | `prompt_config.py:213`、`preparation_program.py:15`、`prompt_values.py:18` | BND-12 |
| 6 | `context_v4`、`context_update`、`context_prompt_v6` | `context_v4.py:130`、`:131`、`context_update.py:278` | BND-12 |
| 7 | `context_package -> context_v4_nodes -> context_package` | `context_package.py:22`、`context_v4_nodes.py:11` | BND-12 |

**未发现 `adapter.py` / `gemini_adapter.py` 反向读取或调用工作流。** 当前已证实的供应商周边环在契约、凭据和宿主依赖中，不是供应商传输执行工作流。完整回调依赖与锁顺序仍需独立审计。

## 4. 已有清楚的模块边界

- HTTP 适配与 `GraphApplication` 应用分发已分离；应用 facade 包装身份/回执，不拥有运行状态和持久记录，属于合理上层编排。
- `adapter.py`、`gemini_adapter.py` 负责协议收发/转换，不执行工具、不读取会话；Gemini 原始签名/part 和工具协议投影属于通信协议职责，应保留。
- `provider_discovery.py` 自身可以独立进行受限管理查询；问题在入口所属宿主和配置所有权，不是它内部启动工作流。
- `HostServiceRun` 已有冻结授权、注入环境、请求/响应契约、释放 hook；`PublicModelService` 不直接持有数据库或 Agent。运行授权/事实/防重放是模型宿主职责，不应为追求“纯通信”删除。
- `SnapshotKernel` 与宿主运行控制有分工，持久历史没有冒充可重放的活动 continuation。
- Tavern 节点以声明式能力和 settlement intent 接入，不直接修改数据库；本轮未发现其承担供应商 API 通信。

## 5. 验证与限制

| 路线 | 验证范围 | 结果 |
| --- | --- | --- |
| 主代理 | `test_graph_input_declarations.py`、`test_current_architecture_retirement.py`、`test_current_runtime_isolation.py` | 11 通过，8.93 秒 |
| 供应商代理 | `test_model_package.py`、`test_provider_configuration.py` | 63 通过，3.70 秒 |
| 运行/持久化代理 | `test_graph_session_objects.py`、`test_runtime_hosting_integration.py`、`test_current_runtime_isolation.py` | 20 通过，14.21 秒 |
| 只读/内存实验 | 消费者输入投影；无关损坏记录对图事务的影响 | 结果分别记录在 BND-10、BND-06 |

运行代理与主代理重叠 1 个 isolation 用例，以上数字不相加作为独立验收总数。供应商/运行代理首次默认系统 Temp 目录遇 ACL 拒绝，改用全新 `.local` 隔离目录后通过，不能计为产品失败。

全部用临时数据库、内存库或 mock transport，不访问产品数据或真实上游。产品服务未启动；隔离子进程和执行器均已结束。已有测试保护了不泄密、冻结、原子性和不重放，但不能据通过结果关闭本报告的架构/使用逻辑问题，也不代表后端全量绿色。

本轮未实施完整动态调用追踪、锁顺序审查、规模测试或浏览器完整恢复验收；此前 outbox/CAS/未决请求问题见 [原故障记录](PROVIDER-RECOVERY-TODO-2026-10-09.md)，没有因本次调查而修复。

## 6. 后续整改顺序建议

1. **先恢复可诊断性。** 处理 BND-02、10、11，并联动 CFG-01–03、09 的原请求核实/表单恢复；确保用户能知道失败阶段并安全继续，不以再次清后端代替修复。
2. **再调整配置权威。** 处理 BND-01、03，底层配置独立管理 URL/Key/模型/容量，工作流只引用能力；加载、编辑、保存和执行都不修改底层配置。
3. **收紧依赖方向。** 处理 BND-04、05、13，中立契约/凭据接口在下层，领域服务在上层，图核心通过注册能力而非旧模型模块了解领域类型。
4. **统一事实解析与业务证明接口。** 处理 BND-07–09、12，明确 Artifact 事实、各入口权限和 Context/Agent 证明的责任，保留防伪与原子结算。
5. **最后做事务故障隔离和测量。** 处理 BND-06，建立相关闭包校验与独立全库体检，再测历史增长成本。

每批增加边界测试：供应商通信不依赖工作流；底层配置可独立初始化；工作流配置不能写底层；声明输入与执行诊断分离；真实错误保留脱敏原因；跨会话损坏隔离。已有环设明确基线，禁止新增跨层环，逐批消除，不能用删校验、伪造签名或自动重放制造通过。

本节只是整改建议，不是实施授权、排期或已完成修复。

## 7. 后续用户裁决

用户已订正配置入口、复用、采用、容量与开发测试清理规则，见 [模型供应商整改规则](MODEL-PROVIDER-DECISIONS-2026-10-10.md)。按统一供应商入口、全局命名配置、新运行自动采用、容量仅手填推进设计；“保存 Key”直接写入生效关联，不再要求保存第二次资源。新一轮代码开发/底层更新清理此前全部开发测试工作流与相关历史内容，不为旧流做兼容更新或迁移，不同时维护多套运行架构；日常配置编辑不触发删图。开发测试损坏重置按用户裁决，不按本报告 BND-06 的局部隔离建议直接实施。审查事实与未修复状态不因规则订正而关闭。
