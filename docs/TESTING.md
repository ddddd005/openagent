# 测试说明

默认只做受影响的离线验证，不自动运行全量测试或真实模型调用。下面命令从已完成 [快速安装](QUICKSTART.md) 的仓库开始。

## 安装测试依赖

仓库根目录：

```powershell
.\.venv\Scripts\python.exe -m pip install -e "./backend[test]"
```

旧兼容测试需要本仓库保留的上游包。在 `backend/` 内可执行：

```powershell
..\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

该文件通过 `../vendor/smolagents` 安装兼容依赖。普通新版运行不依赖它；不要把上游兼容测试结果称为新版工作流完整通过。

## 后端定向测试

在 `backend/` 内执行：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_tool_package.py tests/test_graph_execution.py -q
..\.venv\Scripts\python.exe -m pytest tests/test_model_package.py tests/test_serial_agent_demo_integration.py tests/test_graph_failed_retry.py -q
```

第一组验证普通处理与图契约，第二组使用测试配置/受控 transport 验证模型包、串行 A -> B 及失败续跑，不需要真实密钥。串行集成测试含独立上下文、多轮、完成回合分叉/重开及输入材料检查。

最小无模型用例：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_graph_execution.py::test_zero_agent_text_regex_output_ignores_unreachable_unknown_and_agent -q
```

它执行文本 -> 正则 -> 输出，确认不可达 Agent 不被激活，不联网也不持有真实模型凭据。

## 前端定向测试

在 `frontend/` 内执行：

```powershell
npm test -- src/domain/serialAgentDemo.test.ts src/stores/workflowDemoActions.test.ts src/components/CurrentModelConfiguration.test.ts
npx vue-tsc --noEmit
npm run build
```

`npm run build` 已包含类型检查。单独类型检查可更快定位错误；Vite 的大 chunk 提示与构建失败分别报告。构建生成的 `dist/` 不提交。

## 按改动选测试

| 改动 | 优先覆盖 |
| --- | --- |
| 内容节点、端口或编译 | `test_tool_package.py`、`test_graph_execution.py` 与前端图契约测试 |
| 模型源或资源保存 | `test_model_package.py`、前端 `workflowResources`、`workflowModelResources` 和 `CurrentModelConfiguration` 测试 |
| 普通串行模板 | 后端 `test_serial_agent_demo_integration.py`，前端 `serialAgentDemo`、`workflowDemoActions` |
| 失败续跑及控制 | `test_graph_failed_retry.py`、前端 `graphChatConsumer`、相关运行控制测试 |
| 信息读取/授权 | `test_graph_information_integration.py`、`test_plan12b_information_scope.py`、前端 `workflowInformation` |
| 上下文或前端对象 | `test_context_package.py`、`test_frontend_execution_packages.py`、串行集成与前端接线测试 |

表内前端名称均对应 `src/` 下的 `.test.ts` 文件；用文件路径限定运行，不因触及共享代码就隐式发起在线调用。共享契约改动应增加跨模块测试及授权/幂等边界覆盖。

## 人工在线验收

只有明确允许真实调用与费用时执行，使用独立数据库和服务，不使用正式会话。离线替身通过不能替代真实通信，实际 HTTP 验收也不能替代浏览器操作。

1. 按快速启动配置有效供应商、精确模型名称与凭据引用；记录源码版本和模型参数，不记录密钥。
2. 从普通串行示例输入一条短文本，核实 A 输入为原文，B 同时收到原文和 A 的本轮材料，公开输出来自 B。
3. 显式再输入一轮，确认 A/B 各自上下文按轮次写回，B 不重复带入上一轮 A 材料。
4. 核对逻辑请求、实际 HTTP 尝试、响应事实、产物及用量。Agent 可能多次调用模型或工具，不将“两个 Agent”固定当作“两次 HTTP”。
5. 检查上下文 merge、user/assistant 记录、presentation 和完成检查点，而非只检查 HTTP 200。
6. 停止自己的服务，重新启动后只读核对完成历史和公开结果；这一步不得默认派发下一轮模型。

不得自动重发失败或未知请求。确定失败、暂停和接纳故障优先使用受控测试注入，真实验收不主动制造收费失败来证明所有恢复分支。

## 结果口径

记录测试命令、环境、范围、退出码、通过/失败数和未执行项；重复批次按用例身份去重，不累加冒充新增覆盖。保留失败日志，修复后另存复测结果。

原开发阶段有受影响测试与六回合真实串行证据，但不存在本发布快照的全量绿灯承诺。六回合业务及只读恢复通过，原包装因 `WinError 5` 退出 1；没有覆盖浏览器人工操作、常驻部署、1M token 极限或无限长会话。新改动必须有自己的验证记录，不能沿用历史通过结论。

## 本次发布检查

2026-10-06 的发布目录验证使用 Python 3.12.8 和 Node.js 24.19.0：

- 全新 `.venv` 安装 `backend[test]` 成功，核心无需 smolagents，`pip check` 通过。
- 后端核心、串行示例、平台、HTTP 和 schema 定向 36 项通过；跨语言前端包定向 4 项通过。
- 独立兼容环境的工具等定向 52 项通过；与上述批次存在重叠，不累计为独立用例数量。
- 前端锁文件在独立目录执行 `npm ci` 成功；使用新安装依赖复测定向 7 文件、144 项通过。此前已启动的一次前端全量为 71 文件、663 项通过，批次不累计。
- 前端类型检查及生产构建通过；保留已有单 chunk 大于 500 kB 的体积提示。
- 全新环境后端以动态 loopback 端口和临时库启动，health、26 种节点目录及首页返回正常；检查后停止临时服务。
- 文档中的最小无模型文本处理用例在全新环境执行，1 项通过；与其他定向范围可能重叠，不累加。

首次后端测试因验证临时目录的父目录不存在出现 fixture 错误，修正验证环境后复测通过。安装保留现有 `lucide-vue-next` 弃用提示；本次不为发布升级依赖或改写图标实现。上述结果不是后端全量、浏览器人工或真实模型验收；本次没有真实模型请求，没有操作原运行数据库或常驻服务。
