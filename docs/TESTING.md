# 测试说明

默认按改动范围做离线验证，不自动运行全量、启动服务或调用真实模型。下面路径均对应 2026-10-09 现行源码；准备环境见 [快速启动](QUICKSTART.md)，声明与数据退役见 [节点目录](NODE-DIRECTORY-2026-10-09.md)。

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
..\.venv\Scripts\python.exe -m pytest tests/test_current_node_directory.py tests/test_tool_package.py tests/test_graph_execution.py --basetemp .local/pytest-current-core -q
..\.venv\Scripts\python.exe -m pytest tests/test_model_package.py tests/test_serial_agent_demo_integration.py tests/test_context_native_integration.py tests/test_agent_native_execution.py tests/test_result_acceptance_retry.py --basetemp .local/pytest-current-native -q
..\.venv\Scripts\python.exe -m pytest tests/test_gemini_adapter.py tests/test_gemini_history.py tests/test_gemini_model_integration.py tests/test_gemini_joint_integration.py --basetemp .local/pytest-current-gemini -q
..\.venv\Scripts\python.exe -m pytest tests/test_storage_retirement.py tests/test_prompt_current_resources.py tests/test_global_resources.py --basetemp .local/pytest-current-storage -q
```

每批使用专用、新的 basetemp 目录；pytest 会管理并可能清空该目录，不指向产品库或证据目录。这些用例使用临时 SQLite 和受控/mock transport，不需要真实模型密钥。`workflow_test_support.py` 提供现行原生图及共用替身。

最小无模型用例：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_graph_execution.py::test_zero_agent_text_regex_output_ignores_unreachable_unknown_nodes --basetemp .local/pytest-current-no-model -q
```

该用例运行文本 -> 正则 -> 输出，验证不可达未知节点不被激活。

当前原生 Agent 没有旧失败重试策略，也没有 `test_graph_failed_retry.py`。验证成功响应的接纳重试用 `test_result_acceptance_retry.py`；底层 `test_runtime_recovery.py` 的内核恢复测试不能当作工作台已允许失败节点重试或活动跨进程恢复的证明。

## 前端离线测试

从 `frontend/` 执行：

```powershell
npm test -- src/domain/nodeCatalog.test.ts src/domain/nodeSelection.test.ts src/components/NodeProfileDialog.test.ts src/domain/serialAgentDemo.test.ts src/stores/workflowDemoActions.test.ts src/components/CurrentModelConfiguration.test.ts
npm test -- src/adapters/workbenchPersistence.test.ts src/stores/workbenchRecovery.test.ts src/adapters/workflowReceiptApi.test.ts src/application/workflowClientBoundary.test.ts src/adapters/graphChatConsumer.test.ts
npx vue-tsc --noEmit
npm run build
```

`npm run build` 已包含类型检查；`dist/` 不提交。Vite >500 kB 提示与构建失败分别报告，不能仅凭 bundle 生成就判定测试或页面通过。

获得明确范围后可在 `frontend/` 执行 `npm test` 运行前端全量；后端全量命令需另选专用 basetemp 并保留退出码、日志及 JUnit，不因其失败自动调用真实模型。

## 按改动选测试

| 改动 | 优先覆盖 |
| --- | --- |
| 节点目录、二级菜单、协议切换 | `test_current_node_directory.py`；`nodeCatalog`、`nodeSelection`、`NodeProfileDialog` 前端测试 |
| 内容、端口、编译与引用 | `test_tool_package.py`、`test_graph_execution.py`；`domain/workflowGraph.test.ts` |
| 模型来源与供应商资源 | `test_model_package.py`、`test_global_resources.py`、`test_gemini_model_integration.py`；`workflowResources`、`workflowModelResources`、`CurrentModelConfiguration` |
| Gemini 工具、思考、签名及连续轮次 | `test_gemini_adapter.py`、`test_gemini_history.py`、`test_gemini_joint_integration.py` |
| 原生上下文、精简与串行图 | `test_context_native_integration.py`、`test_agent_native_execution.py`、`test_compacting_serial_integration.py`、`test_serial_agent_demo_integration.py` |
| 暂停、结果接纳与不重放 | `test_runtime_pause.py`、`test_result_acceptance_retry.py`、`test_gemini_model_integration.py`；`adapters/graphChatConsumer.test.ts` |
| 退役图、资源与浏览器存档 | `test_storage_retirement.py`、`test_prompt_current_resources.py`；`adapters/workbenchPersistence.test.ts`、`stores/workbenchRecovery.test.ts` |
| 回执、未知结果、授权与身份 | `test_graph_receipt_http.py`、`test_graph_application_receipt_custody.py`、`test_graph_application_identity.py`；`adapters/workflowReceiptApi.test.ts`、`application/workflowClientBoundary.test.ts` |
| 信息、公开摘要与消费者 | `test_graph_information_integration.py`、`test_plan12b_information_scope.py`、`test_tavern_chat_integration.py`；`application/workflowInformation.test.ts` |
| 静态资产与启动分支 | `test_server_workbench.py`、`test_branch_launcher.py`；`test_dev_launcher.py` 中预检/静态用例 |

表内后端文件在 `backend/tests/`。前端短名在 `frontend/src/` 下以 `.test.ts` 结尾，按确切路径运行。`test_dev_launcher.py` 的完整模块包含真实本机服务/浏览器场景，不能将其统称为“不启动服务”的离线组；需要静态检查时只指定已核对的用例，例如：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_dev_launcher.py::test_launcher_keeps_services_in_visible_consoles tests/test_branch_launcher.py::test_wrapper_has_no_hidden_launch_or_automatic_git_mutation --basetemp .local/pytest-launcher-static -q
```

## 浏览器与在线验收

菜单的现行离线浏览器脚本为 `backend/tests/node_menu_browser.cjs`。以真实生产 `dist`、隔离 browser context 和显式 mock catalog 验证菜单及存档；静态资产和请求由脚本拦截，不监听端口。该结果不能代替真实后端启动、用户存档或整工作台移动适配。复现环境要求见脚本及 [节点目录证据](NODE-DIRECTORY-2026-10-09.md)。

真实调用只有在明确授权供应商、费用和范围后执行，使用隔离数据库，不使用正式会话：

1. 记录源码完整 SHA、实际启动路径、资源修订、精确模型及参数；凭据只保留环境引用。
2. 普通串行示例核实 A 收到原文、B 收到原文及 A 本轮材料，公开输出来自 B。
3. 显式再输入一轮，核对独立 A/B 上下文和本轮材料，不把历史重读自动变成新派发。
4. 核对逻辑请求、实际 HTTP、用量、工具派发/结算、接纳、对象写回及完成检查点，不能以 HTTP 200 代替业务完成。
5. Gemini 还需核对连续工具轮次和签名接纳；只看可见摘要不能证明供应商协议有效。离线合成签名不得发到真实网络。
6. 在无活动/未知请求前提下停止自己的服务；重启只读核对完成历史，不默认恢复活动内核、重放工具或启动下一轮。

确定失败、超时未知、暂停和接纳故障优先使用受控注入，不主动制造收费失败。Agent 可能多次请求模型或工具，“两个 Agent”不是固定“两次 HTTP”；运行次数/输出 token 限制也不是完整费用硬上限。

## 当前结果

以下是已登记的历史执行结果，不是本次文档订正重新运行的测试：

| 范围 | 已登记结果与边界 |
| --- | --- |
| 新 `main` 晋升前端 | 67 文件 / 928 项通过，类型/生产构建通过 |
| 新 `main` 后端定向 | 13 文件 / 239 项通过；非后端全量 |
| 节点整理后端全量第二轮 | 3008 通过 / 50 失败 / 9 跳过 |
| 全量后最后修改补测 | 191 项通过；包含全量开始后新增/修改的案例 |
| 菜单离线浏览器 | 9 场景 / 11 截图通过，0 后端变更、0 页面/控制台/资产错误 |
| 共用类型声明 | 33 个共同声明在原版本下无重定义 |

50 项失败的测试身份全部在旧稳定基线对照中出现，未发现基线外失败，但仍属于未解决测试债务。集中于 `test_execution_facts.py` 的已删除固定 `save_bundle/get_record` API、`test_frontend_controls.py` 缺失脚本及 `test_host_sdk.py` 的既有预期。不恢复退役 API、不删除通用测试来宣称全绿。最终补测与各轮集中范围有重叠，不相加为独立覆盖数；全量第二轮也不是最后所有源码修改后的完整重跑。

记录命令、目标版本、环境、退出码、通过/失败/跳过和未执行项。初轮失败保留，修复后另存复测；系统 Temp、工具包装和产品失败分别分类。未完成的真实 Gemini 验收、全面故障、活动跨进程恢复和整个工作台移动适配不因上述通过关闭。

本地日志/JUnit/截图在 ignored `.local`，不能从远端仓库取得，也不上传数据库、密钥或原始私有响应。可审阅的范围与口径见 [节点目录](NODE-DIRECTORY-2026-10-09.md)、[主干晋升](MAIN-PROMOTION-2026-10-09.md) 和 [Gemini 实现](GEMINI-ACCEPTANCE-2026-10-09.md)。

## 历史证据

2026-10-06/07 的发布目录、wheel、旧节点数量、限定 mock/真实模型及当时常驻状态只证明当时快照。完整数字及初轮问题保留在 [旧架构移除](REPLACE-REMOVAL.md)、[0.2.0 基线](BASELINE-0.2.0-2026-10-07.md) 和 [工作区梳理](WORKTREE-ACCEPTANCE-2026-10-09.md)。不将旧 `B=503` 重试场景、旧安装包或旧真实调用当作现行原生 Agent 能力或当前部署验证；`v0.2.0` 是固定源码身份，不等于当前 HEAD。
