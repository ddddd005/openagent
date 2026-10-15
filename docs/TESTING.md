# 测试说明

默认按当前改动范围做离线验证，不自动运行全量、启动产品服务或调用真实模型。2026-10-15施工树聊天/检查点证据见[55号](中期检查2/55-聊天候选数据层与重roll检查点接续实施回执-2026-10-15.md)，退役清单见[56号](中期检查2/56-D-E阶段旧内容退役勘察与收口清单-2026-10-15.md)，实际整理和最终结果见[57号回执](中期检查2/57-D-E阶段旧内容退役与收口实施回执-2026-10-15.md)：后端52文件778项、前端21文件259项、类型及隔离构建通过。全后端4194项仅完成收集，不宣称历史全量执行绿色。旧节点/API专属测试不强迫新架构恢复兼容；混合测试保留有效身份、权限及回执断言，旧夹具须按原意订正。

## 测试依赖

仓库根目录：

```powershell
.\.venv\Scripts\python.exe -m pip install -e "./backend[test]"
```

或在 `backend/`：

```powershell
..\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

该文件仅安装当前 `.[test]`。旧架构专属测试及 smolagents 适配已退役，vendored 来源不作为测试依赖。跨语言测试还需 Node.js 与 `frontend/node_modules`；缺依赖或环境跳过须单独说明。

## 后端离线测试

从 `backend/` 执行。显式使用仓库内隔离 basetemp，避免已有 Windows 系统 Temp 权限问题：

```powershell
New-Item -ItemType Directory -Force .local | Out-Null
..\.venv\Scripts\python.exe -m pytest tests/test_chat_contracts.py tests/test_workflow_chat_operations.py tests/test_workflow_chat_service.py tests/test_workflow_chat_integration.py --basetemp .local/pytest-current-chat-unique -q
..\.venv\Scripts\python.exe -m pytest tests/test_chat_checkpoints.py tests/test_variable_checkpoints.py tests/test_checkpoint_repository.py tests/test_checkpoint_loading.py --basetemp .local/pytest-current-checkpoints-unique -q
..\.venv\Scripts\python.exe -m pytest tests/test_variable_registration_contract.py tests/test_variable_http_registration.py tests/test_lorebook_engine.py --basetemp .local/pytest-current-variables-unique -q
```

每批使用专用、新的basetemp目录；pytest会管理并可能清空该目录，不指向产品库或证据目录。这些用例使用临时SQLite和受控执行器，不需要真实模型密钥。旧`workflow_test_support.py`里的模型/Agent图不能自动当作当前夹具；精确旧排除及原因按当批回执保留。

最小无模型用例：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_graph_execution.py::test_zero_agent_text_regex_output_ignores_unreachable_unknown_nodes --basetemp .local/pytest-current-no-model -q
```

该用例运行文本 -> 正则 -> 输出，验证不可达未知节点不被激活。

当前原生 Agent 没有旧失败重试策略，也没有 `test_graph_failed_retry.py`。验证成功响应的接纳重试用 `test_result_acceptance_retry.py`；底层 `test_runtime_recovery.py` 的内核恢复测试不能当作工作台已允许失败节点重试或活动跨进程恢复的证明。

## 前端离线测试

从 `frontend/` 执行：

```powershell
npm test -- src/domain/nodeCatalog.test.ts src/stores/workflowSessionRegistration.test.ts src/components/FrontendNodeFields.test.ts src/application/frontendNodeEntry.test.ts src/domain/chatContext.test.ts
node node_modules/vue-tsc/bin/vue-tsc.js --noEmit
npm run build
```

`npm run build` 已包含类型检查；`dist/` 不提交。Vite >500 kB 提示与构建失败分别报告，不能仅凭 bundle 生成就判定测试或页面通过。

若使用隔离源码副本及既有依赖，须明确副本路径、固定工具入口并核对源文件hash，不用npx临时下载不同版本冒充项目依赖。启动预检测试可用`OPENAGENT_LAUNCHER_TEST_WORKSPACE`指向明确的隔离源码副本；该变量仅属测试，不改变BAT的分支选择、缺依赖拒绝或产品库路径。

获得明确范围后可在 `frontend/` 执行 `npm test` 运行前端全量；后端全量命令需另选专用 basetemp 并保留退出码、日志及 JUnit，不因其失败自动调用真实模型。

## 按改动选测试

| 改动 | 优先覆盖 |
| --- | --- |
| 节点目录、二级菜单、协议切换 | `test_current_node_directory.py`；`nodeCatalog`、`nodeSelection`、`NodeProfileDialog` 前端测试 |
| 内容、端口、编译与引用 | `test_tool_package.py`、`test_graph_execution.py`；`domain/workflowGraph.test.ts` |
| 模型来源与供应商资源 | `test_model_package.py`、`test_global_resources.py`、`test_gemini_model_integration.py`；`workflowResources`、`workflowModelResources`、`CurrentModelConfiguration` |
| Gemini 工具、思考、签名及连续轮次 | `test_gemini_adapter.py`、`test_gemini_history.py`、`test_gemini_joint_integration.py` |
| 上下文准确项、快照和分叉 | `test_context_registration_fork_integration.py`、`test_checkpoint_loading.py`；Agent/模型历史夹具不自动纳入当前安装验收 |
| 暂停、结果接纳与不重放 | `test_runtime_pause.py`、`test_result_acceptance_retry.py`、`test_gemini_model_integration.py`；`adapters/graphChatConsumer.test.ts` |
| 显式重置、正常初始化与旧工作台隔离 | `test_development_reset.py`、`test_storage_initialization.py`、`test_prompt_current_resources.py`；`adapters/workbenchPersistence.test.ts`、`stores/workbenchLegacyIsolation.test.ts` |
| 回执、未知结果、授权与身份 | `test_graph_receipt_http.py`、`test_graph_application_receipt_custody.py`、`test_graph_application_identity.py`；`adapters/workflowReceiptApi.test.ts`、`application/workflowClientBoundary.test.ts` |
| 信息、公开摘要与准确节点消费者 | `test_graph_information_integration.py`、`test_frontend_node_chat_http.py`；`application/workflowInformation.test.ts` |
| 静态资产与启动分支 | `test_server_workbench.py`、`test_branch_launcher.py`；`test_dev_launcher.py` 中预检/静态用例 |
| 内存表单、原请求及异步归属 | `test_frontend_node_retired_receipts.py`、`test_frontend_node_evidence.py`；`useVolatileFormState`、`resourceOutbox`及现行管理原请求测试 |
| 逻辑聊天、重roll及完整候选分叉 | `test_chat_contracts.py`、`test_workflow_chat_service.py`、`test_workflow_chat_integration.py`、`test_chat_checkpoints.py` |
| 输入声明、提示词专用表单及不完整图 | `test_graph_input_declarations.py`、`test_prompt_package.py`；`graphInputDeclarationsApi`、`CurrentPromptResources` |
| 连线拒绝与撤销记录 | `domain/graphConnections.test.ts`、`stores/workflowGraph.test.ts`；真实拖线用 `frontend_p56_browser.cjs` |
| 正文/签名展示、本机名称和目录归档 | `resultPresentation`、`managementLabels`；冻结历史、确认取消及其他暂停会话用 `frontend_p56_browser.cjs` |
| 旧浏览器内容不导入及原请求只读核实 | `stores/workbenchLegacyIsolation.test.ts`、`adapters/resourcePendingStorage.test.ts`、`application/resourceOutbox.test.ts`；旧归档恢复专属测试已退出，当期证据仅保留历史记录 |
| 工作流/会话目标归档、删除及管理隔离 | `test_workflow_lifecycle.py`；`stores/workflowLifecycle.test.ts`、`application/workflowCommands.test.ts`、`components/WorkbenchSidebar.test.ts` |
| 单一私有 Key、全局模型配置及回执恢复 | `test_provider_configuration.py`、`test_model_profiles.py`、`test_model_configuration_browser.py`；`providerCredentialEditing`、`modelProfiles`、`resourceOutbox` |

表内后端文件在 `backend/tests/`。前端短名在 `frontend/src/` 下以 `.test.ts` 结尾，按确切路径运行。`test_dev_launcher.py` 的完整模块包含真实本机服务/浏览器场景，不能将其统称为“不启动服务”的离线组；需要静态检查时只指定已核对的用例，例如：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_dev_launcher.py::test_launcher_keeps_services_in_visible_consoles tests/test_branch_launcher.py::test_wrapper_has_no_hidden_launch_or_automatic_git_mutation --basetemp .local/pytest-launcher-static -q
```

## 浏览器与在线验收

菜单的现行离线浏览器脚本为 `backend/tests/node_menu_browser.cjs`。以真实生产 `dist`、隔离 browser context 和显式 mock catalog 验证菜单及存档；静态资产和请求由脚本拦截，不监听端口。该结果不能代替真实后端启动、用户存档或整工作台移动适配。复现环境要求见脚本及 [节点目录证据](NODE-DIRECTORY-2026-10-09.md)。

旧P1/P2浏览器脚本及记录只代表当期持久草稿/酒馆入口合同，不作为当前验收。退役的预设创建和浏览器存档恢复脚本不再维护；当前普通工作台及新版节点页面的离线浏览器证据分别见49号、55号及后续收口回执。

P3/P4 当期用 `frontend_p34_browser.cjs` 验证修订、Key、双协议模板及发现列表；当期结果见 [历史记录](FRONTEND-P3-P4-2026-10-09.md)。2026-10-10 已删除此内联配置夹具及无人引用的旧 `gemini_browser.cjs`，不继续维护旧凭证/模型路线。

开发版当前用 `backend/tests/test_model_configuration_browser.py` 驱动 `model_configuration_browser.cjs`。P01 的补绑定 mock/断言已退出，改为部分失败真实报告、冷重读和只读核实，不增加 Key 或资源派发；需要继续其他配置测试时显式归档原请求，不补写配置。其余场景覆盖统一供应商保存、Key 草稿保护、空 Key 保留、丢响应核实、手填容量、全局配置重读及引用节点。最新实际结果见首批实施记录，不以旧夹具通过数证明新接口。密码框验证 `autocomplete="off"` 且不标记为新密码；系统密码管理弹窗不属于 headless DOM 验收。全部资产及请求拦截，不监听端口、不调用上游，不等同于重跑旧 P3/P4 的全部场景。凭据后端测试使用临时私有库及合成 Key，拒绝环境变量引用；Windows 案例实际调用当前用户 DPAPI。

默认 pytest 临时目录不可访问时，从仓库根目录指定一个新的 `.local/pytest-<unique-id>` 作为 `--basetemp`，不要删除或改权限处理用户的系统临时目录。生产服务不由此启动。

P5/P6 用 `backend/tests/frontend_p56_browser.cjs` 验证正文置前、提示词身份/metadata、半输入数字冷恢复、具名输入保留复杂字段、真实拖线命中与吸附、冻结历史、摘要/签名隐藏、本机名称及可逆归档。恢复/分叉取消没有业务 mutation；另一个已知暂停会话阻止目录归档。全部请求和资产拦截，不监听端口。范围、fixture 和命令见 [P5/P6 记录](FRONTEND-P5-P6-2026-10-09.md)；390px 仅验证弹窗，工作台仍保留 1024px 最小宽度。

本轮按用户裁决使用 DeepSeek，由用户启动开发页面、填写保存 Key 并执行真实验收；全部职责订正完成后统一验收合并。开发代理当前不启动服务、不读取真实秘密、不调用上游。后续真实调用须明确供应商、费用和范围，使用隔离数据库，不使用正式会话：

1. 记录源码完整 SHA、实际启动路径、资源修订、精确模型及参数；仅保留非秘密凭据引用，不记录 Key、私有保护数据或请求头。
2. 普通串行示例核实 A 收到原文、B 收到原文及 A 本轮材料，公开输出来自 B。
3. 显式再输入一轮，核对独立 A/B 上下文和本轮材料，不把历史重读自动变成新派发。
4. 核对逻辑请求、实际 HTTP、用量、工具派发/结算、接纳、对象写回及完成检查点，不能以 HTTP 200 代替业务完成。
5. Gemini 还需核对连续工具轮次和签名接纳；只看可见摘要不能证明供应商协议有效。离线合成签名不得发到真实网络。
6. 在无活动/未知请求前提下停止自己的服务；重启只读核对完成历史，不默认恢复活动内核、重放工具或启动下一轮。

确定失败、超时未知、暂停和接纳故障优先使用受控注入，不主动制造收费失败。Agent 可能多次请求模型或工具，“两个 Agent”不是固定“两次 HTTP”；运行次数/输出 token 限制也不是完整费用硬上限。

## 历史结果

以下表格是各历史批次结果，不是当前源码全量结果，也不是本次文档订正重跑。当前施工树的实际选组、准确失败/排除和页面边界以最新实施回执为准，不能累计重叠案例。

| 范围 | 已登记结果与边界 |
| --- | --- |
| 新 `main` 晋升前端 | 67 文件 / 928 项通过，类型/生产构建通过 |
| 新 `main` 后端定向 | 13 文件 / 239 项通过；非后端全量 |
| 节点整理后端全量第二轮 | 3008 通过 / 50 失败 / 9 跳过 |
| 全量后最后修改补测 | 191 项通过；包含全量开始后新增/修改的案例 |
| 菜单离线浏览器 | 9 场景 / 11 截图通过，0 后端变更、0 页面/控制台/资产错误 |
| 共用类型声明 | 33 个共同声明在原版本下无重定义 |
| P5/P6 源码 | 前端全量 87 文件/1075 项、类型/构建及后端定向 14 文件/286 项通过；功能提交 `0c15587` 已本地合入 main，本批未推送 |
| P5/P6 离线浏览器 | 12 场景/7 截图；另复核草稿 7 场景、P3/P4 7 场景及菜单 9 场景，不累加为独立覆盖 |
| 酒馆入口/旧存档修补 | 前端全量 88 文件/1087 项、后端定向 3 文件/67 项及类型/构建通过；入口/归档浏览器 8 场景/8 截图，另复核草稿与 P5/P6，不派发模型或工具 |

50 项失败的测试身份全部在旧稳定基线对照中出现，未发现基线外失败，但仍属于未解决测试债务。集中于 `test_execution_facts.py` 的已删除固定 `save_bundle/get_record` API、`test_frontend_controls.py` 缺失脚本及 `test_host_sdk.py` 的既有预期。不恢复退役 API、不删除通用测试来宣称全绿。最终补测与各轮集中范围有重叠，不相加为独立覆盖数；全量第二轮也不是最后所有源码修改后的完整重跑。

记录命令、目标版本、环境、退出码、通过/失败/跳过和未执行项。初轮失败保留，修复后另存复测；系统 Temp、工具包装和产品失败分别分类。未完成的真实 Gemini 验收、全面故障、活动跨进程恢复和整个工作台移动适配不因上述通过关闭。

本地日志/JUnit/截图在 ignored `.local`，不能从远端仓库取得，也不上传数据库、密钥或原始私有响应。可审阅的范围与口径见 [节点目录](NODE-DIRECTORY-2026-10-09.md)、[主干晋升](MAIN-PROMOTION-2026-10-09.md) 和 [Gemini 实现](GEMINI-ACCEPTANCE-2026-10-09.md)。

## 历史证据

2026-10-06/07 的发布目录、wheel、旧节点数量、限定 mock/真实模型及当时常驻状态只证明当时快照。完整数字及初轮问题保留在 [旧架构移除](REPLACE-REMOVAL.md)、[0.2.0 基线](BASELINE-0.2.0-2026-10-07.md) 和 [工作区梳理](WORKTREE-ACCEPTANCE-2026-10-09.md)。不将旧 `B=503` 重试场景、旧安装包或旧真实调用当作现行原生 Agent 能力或当前部署验证；`v0.2.0` 是固定源码身份，不等于当前 HEAD。
