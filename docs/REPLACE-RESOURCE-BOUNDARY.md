# REPLACE-03 公共资源与请求门禁解耦

承接 [支持与处置矩阵](REPLACE-SUPPORT-MATRIX.md)、[1.0 规划](PLAN-1.0.md)及固定 [2026-10-06 基线](BASELINE-2026-10-06.md)。用户授权提交既有改动后开始架构收口；此前各切片和阶段 A 联合裁决已提交为 `237afb5c0692f78b05f1aad912a53941d67e9aee`，提交后工作区曾为空，**尚未推送**。

本页对应该提交之上的新工作区，尚未再次提交。本轮三个并发子代理分别拆公共资源宿主、请求门禁及资源纯契约，主代理负责剩余纯导入接线、当前 Agent 新进程回归和合并复核。这是阶段 B / REPLACE-03 的首个实际解耦切片，不是阶段 A 的再次盘点，也不是旧架构全部退出。

## 1. 范围与实现

| 范围 | 本轮实现 | 保留边界 |
| --- | --- | --- |
| 公共资源纯契约 | 新增 `resource_contracts.py`，迁出 10 个函数及 4 个常量；旧模块同名 re-export，当前资源和纯校验调用者改接新模块 | 限制、身份、异常、正文及旧 store 行为不改，`CORE_SESSION_NOTE` 原样保留 |
| 资源依赖预检与冻结读取 | 新增 `GraphResourceHost`，直接承载依赖声明、继承身份、运行前解析及 current 执行帧读取；当前服务 MRO 选择新宿主 | 预检仍先于服务准备和节点效果，完整身份、每节点授权、冻结值及缺帧错误保持 |
| 旧资源与模型 hook | `GraphAgentHost` 保留原同名委托；只有实际旧 model/content 需求才加载旧 resolver/store；没有 `workflow.agent@2` 时不查询旧 Agent 选中历史 | 旧 Agent、旧资源、迁移及档案调用者仍在，不提前删除旧宿主 |
| 普通/准备型请求门禁 | 新增 `prepared_request.py`；每次先验证并分离快照，按 `resolved.context.capabilities` 判断；只有 prepared 请求按需进入原完整算法 | 普通请求拒绝非空 preparation evidence，不按 `public_agent` 标记绕过校验；prepared 仍校验完整冻结证据及容量 |
| 导入接线 | 当前资源、图节点/执行/公开字段、准备程序、model/exposure 纯校验改用纯契约；WorkbenchInterfaces 仅在调用旧资源方法时构造原 store | 旧方法的锁、事务、CAS、owner 和回执语义不改 |

主要新模块：[资源契约](../backend/src/phase1_agent/resource_contracts.py)、[资源宿主](../backend/src/phase1_agent/graph_resource_host.py)、[请求门禁](../backend/src/phase1_agent/prepared_request.py)。接线入口：[graph_service](../backend/src/phase1_agent/graph_service.py)、[graph_runtime_host](../backend/src/phase1_agent/graph_runtime_host.py)、[runtime](../backend/src/phase1_agent/runtime.py)、[workbench_interfaces](../backend/src/phase1_agent/workbench_interfaces.py)。

门禁对 malformed resolved/context/capabilities 明确抛 `ContractValidationError`，不静默跳过或把非数组 capabilities 当成合法描述。有效普通和 prepared 请求的业务语义保持；prepared 消息/工具的 canonical 字符容量、消息上限、适配器工具校验和 close 转发没有重写。

本轮不修改包选择、完整锁、schema、迁移、记录分类或用户数据，也不实现冻结/只读入口。公共运行路径的独立拆分可以先实施，但不能据此跳过混合库执行配置、纯档案和回执读取这些退役前置。

## 2. 定向验证

首轮新增四个文件的合并回归在 `backend/` 执行：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_resource_contracts.py tests/test_prepared_request_gate.py tests/test_graph_resource_host.py tests/test_current_runtime_isolation.py -q --basetemp ..\.local\pytest-resource-boundary-new-237afb5-876159e2
```

首轮结果：**4 个文件，144 passed，28.27 秒，退出码 0**。其中纯契约 92 项、门禁 25 项、资源宿主 26 项、当前 Agent 隔离 1 项。子代理各自重跑和主代理单独复测均与这批重叠，不再次累计；后续 prepared 单次门禁去重后的结果另列，不沿用首轮作为全部最终代码证据。

覆盖：

- 资源身份/修订/原异常、UTF-8 字节限制、数据 schema/default/命名空间、detached 返回、旧同名导出以及原 store 的精确修订、CAS、回执。
- 资源依赖声明错误、单个 current 身份继承、缺失/禁用在效果前拒绝、完整身份授权、缺帧不回落、暂停时修改资源不改变本次冻结值及下一轮使用新值。
- 普通请求在新进程禁用旧准备链时两次 dispatch；非法快照、伪造准备证据零 dispatch 拒绝；prepared 完整冻结校验、tools 与 accepted delta 的容量边界。
- 独立 current provider/prompt、`agents.execute@3`、上下文读/装配/合并及公开输出组成的当前图：两轮运行、完成候选分叉后续聊、原分支隔离、测试 SQLite 精确历史重开。

当前 Agent 隔离回归在导入前禁止 `graph_agent_runtime`、`workbench_resources`、`workflow`、`prepared_context`、`prompt_preparation`、`variable_preparation`、旧 `kernel`；同时把旧宿主 preflight/host_call/私有 runtime 及 legacy model/content hook 改为立即失败。使用本地模型替身、dummy credential 和 `.invalid` 地址，没有真实网络或模型请求。`GraphAgentHost` 的类定义仍被导入，因此不能把结果称为旧宿主模块完全退出。

新增回归：[资源契约](../backend/tests/test_resource_contracts.py)、[请求门禁](../backend/tests/test_prepared_request_gate.py)、[资源宿主](../backend/tests/test_graph_resource_host.py)、[当前 Agent 隔离](../backend/tests/test_current_runtime_isolation.py)。

既有受影响范围在 `backend/` 执行：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_global_resources.py tests/test_graph_current_resources.py tests/test_graph_resource_dependencies.py tests/test_prompt_current_resources.py tests/test_models_service_integration.py tests/test_agent_integration.py tests/test_serial_agent_demo_integration.py tests/test_graph_nodes.py tests/test_graph_execution.py tests/test_graph_service.py tests/test_graph_platform.py tests/test_graph_public.py tests/test_workbench_resources.py tests/test_preparation_program.py tests/test_model_configuration.py tests/test_exposure_configuration.py tests/test_graph_agent_service.py tests/test_graph_prompt.py tests/test_workflow_prepared_context.py tests/test_workflow_prepared_history.py tests/test_workflow_prepared_control.py -q --basetemp ..\.local\pytest-resource-boundary-existing-237afb5-1634b927
```

首轮结果：**21 个文件，261 passed / 1 failed，734.81 秒，退出码 1**。失败为 `test_workflow_prepared_history.py::test_selected_reroll_history_keeps_complete_protocol_floor_without_unselected_sibling`：原输入及重抽样已完成，第三轮在 `wait_for_idle` 的 `future.result(timeout=20)` 超时。该批不是全部通过，不凭后续新通过关闭 HIST-01/03 的原根因。

另由请求门禁子代理执行 `test_runtime.py` 和 `test_prompt_preparation.py`，首轮 **2 个文件，135 passed，24.13 秒，退出码 0**，与上述两个范围互不重叠；后续去重复测仍算同一范围，不重复累计。

保留初跑过程：

- 请求门禁新增测试初跑 1 passed / 24 failed，因新增工具 fixture 的参数 schema 缺少必需 `description`，业务断言前即失败。只修正测试夹具，没有为测试改变产品工具协议。
- 当前 Agent 隔离初跑 1 failed；前两轮已完成，分叉 fixture 漏传必需 `expected_head_revision`。补传原第二轮 head 后单独 1 passed，6.23 秒；不放宽产品 CAS，不与最终合并批次重复计数。

首轮主代理 AST 对比确认：10 个资源函数、4 个常量、完整旧 store 类、完整冻结准备校验、原容量函数体（仅归一化名称）、PreparedRequestAdapter、PreparedPromptContext、SnapshotKernel 及资源依赖声明方法与 `237afb5` 一致。另两代理只读交叉复核资源宿主、门禁和接线；只读复核不计为测试通过。

### 单次门禁去重

追加静态复核确认，本片初版在 prepared 路径先 `validate_record`，再委托原 frozen validator，单次 gate 从一遍结构校验增加到两遍。标准 Kernel、Adapter 和 model-request 事实回调三处防线均保留，但各自多了一遍完整 snapshot 校验/deepcopy。这是本片新增冗余，可能增加 CPU 和锁占用；没有规模测量，不能据此宣称上述超时的根因已定位。

后续仅拆公共 `validate_frozen_preparation` 的结构校验 wrapper 与原完整冻结算法私有 core，使已经过门禁校验的 prepared 请求直接进入该 core，再执行原容量计算。公共历史/外部 validator 仍先完整校验，三个独立 gate 均保留；不增加可传的“已验证”开关、不缓存证据、不按 `public_agent` 绕过、不放宽 20 秒 timeout，也不启动 DEMO-05/06。

最终代码新增范围复测：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_resource_contracts.py tests/test_prepared_request_gate.py tests/test_graph_resource_host.py tests/test_current_runtime_isolation.py -q --basetemp ..\.local\pytest-resource-boundary-final-237afb5-f5c42b90
```

**4 个文件，148 passed，29.81 秒，退出码 0**。比首轮增加四个参数化案例，验证 prepared 每次 gate 恰好一次快照结构校验、每次仍校验完整 frozen evidence、公共历史 validator 的完整防线及不缓存。子代理新 gate 单独 29 passed / 23.35 秒与这批重叠，不累加。最终 `test_runtime.py` / `test_prompt_preparation.py` **135 passed，24.33 秒，退出码 0**；与这批互不重叠，与其首轮重跑不重复累计。

最终 prepared 冻结流程的受影响范围：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_workflow_prepared_context.py tests/test_workflow_prepared_history.py tests/test_workflow_prepared_control.py -q --basetemp ..\.local\pytest-prepared-gate-final-237afb5-e392ad67
```

结果：**3 个文件，33 passed / 1 failed，401.45 秒，退出码 1**。失败仍是同一个旧 prepared 历史案例，在第三轮 `future.result(timeout=20)` 超时。属于首轮 21 文件中的重叠范围，不再次相加；原输入、重抽样及其他控制/冻结案例通过，不将整批称为通过，也不增加继续重跑或放宽超时。

最终代码以上三个范围互不重叠，共 **9 个文件、317 个案例，316 passed / 1 failed**。首轮、子代理和单例复测不追加累计；首轮其他受影响文件的结果保留为当时版本的证据，不冒称全部已在最终代码重新执行。

旧门禁单案例对照：fresh 进程通过 importlib loader 分别载入 `237afb5` 的 `prepared_context.py` 和 `runtime.py` 原源码，确认旧 gate 同一对象及 git blob 身份；其他当前源码不变，无产品落盘修改。仅执行上述失败案例，仍 **1 failed，65.12 秒，退出码 1**，同为第三轮 `future.result(timeout=20)`。有一条 anyio 已导入的 pytest assert-rewrite warning。这仅证明原 gate/runtime 的局部对照也复现等待失败，不是全库基线验收，不能据此排除所有当前改动或关闭历史根因；不增加继续重跑直到通过的统计。

最终 AST 复核：资源十个函数/四个常量/完整旧 store 类仍相同；公共 frozen wrapper 保留原结构校验语句，冻结主体逐条原样迁入私有 core；容量主体除 helper 名称接线及说明外相同。PreparedRequestAdapter、PreparedPromptContext、SnapshotKernel 和资源依赖声明方法保持；没有为超时改变算法或等待阈值。资源宿主代理又只读交叉复核去重后的 gate/core 调用边界，未发现新增校验绕过、初始化循环或兼容性阻断；不计为新增测试。

## 3. 剩余收口

1. 分开历史精确包选择和当前执行配置，落实 current/frozen/blocked 的记录级分类；保护原锁、旧 status/active/facts，解除同库新图的配置阻断。
2. 迁出完整旧会话及 migrated archives 的纯档案投影，提供受作用域限制的 receipt-only；冻结记录不 bootstrap、不恢复写入，无法核实的请求保留 unresolved。
3. 落浏览器 schema1–6 frozen passthrough 和静态旧键只读桥，再替换正式旧 UI/store/聊天入口；不重 POST，不因 404/410/CAS 清未知请求。
4. 在这些前置到位后退出剩余旧宿主/执行器/路由和必要测试，继续 REPLACE-06 及基础使用验收，不能以本轮隔离回归发布 1.0。

`GraphWorkflowService` 仍继承 `GraphAgentHost`；旧迁移/档案和 WorkbenchInterfaces 仍有条件调用者，prepared 请求仍有意使用原准备实现。原 `import_legacy_current` 不变，也没有成为独立提示词迁移入口。本轮闭环的是当前资源预检/读取和普通请求的特定旧加载依赖，不是 REPLACE-03/02 整体完成。

没有前端产品修改、类型/构建或新的浏览器截图证据；没有全量回归、真实模型调用、用户混合库读写、常驻服务变更、真实故障专项、用户数据清理或第二次提交/推送。普通测试使用独立 ignored `.local` basetemp；旧 prepared 测试保留自有夹具，在 `backend/tests` 新建 TemporaryDirectory 并按原夹具收尾。所有存储证据均来自新建临时库，已有服务保持不动。

DEMO-05/06、COND、LATER 仍在 1.0 之后按需安排；节点打组及历史展示正文编辑/删除继续暂停。固定基线 SHA256 保持 `0690253BEC23A2E377245E6F570B2814C3320CEA2C384A4DE927534678C6FCC4`，不倒改历史记录。

本片主要模块 SHA256：

```text
F31CE73EEFDA77418A7804CA46B364FC7B69E15A1FA8A544D4279C00BBE8B739  backend/src/phase1_agent/resource_contracts.py
152A7A2798D70D7B1F4E65D08D79AA02F4089C870B0D29B22FCC23E57DF654DD  backend/src/phase1_agent/graph_resource_host.py
BD1F4E97B1A1B43F88F4CC0EF8EAAC20E772BD592762FA32C881E8DF7C50200A  backend/src/phase1_agent/prepared_request.py
0CFE8D0B8CECD47308E57F577773281F25E6450A83AD3A848D78E7330858DA8F  backend/src/phase1_agent/prepared_context.py
```

六份新增/更新文档的 133 个本地行内链接、结尾换行及格式检查通过；全部 27 个改动/未跟踪源码和文档的空白/冲突标记检查通过。`git diff --check`、上列校验值及固定基线检查通过，已有提交仍为 `237afb5`，新切片未提交或推送。文档检查不替代失败案例的根因关闭或 1.0 验收。
