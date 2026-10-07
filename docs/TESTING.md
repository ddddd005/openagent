# 测试说明

默认只做受影响的离线验证，不自动运行全量测试或真实模型调用。下面命令从已完成 [快速安装](QUICKSTART.md) 的仓库开始。

## 安装测试依赖

仓库根目录：

```powershell
.\.venv\Scripts\python.exe -m pip install -e "./backend[test]"
```

在 `backend/` 内也可执行：

```powershell
..\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

该文件仅安装当前 `.[test]`。旧架构专属测试与 smolagents 适配已退役，保留的第三方源码不进入当前测试依赖。

## 后端定向测试

在 `backend/` 内执行：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_tool_package.py tests/test_graph_execution.py -q
..\.venv\Scripts\python.exe -m pytest tests/test_model_package.py tests/test_serial_agent_demo_integration.py tests/test_graph_failed_retry.py -q
```

第一组验证普通处理与图契约，第二组使用测试配置/受控 transport 验证模型包、串行 A -> B 及失败续跑，不需要真实密钥。串行集成测试含独立上下文、多轮、完成回合分叉/重开及输入材料检查。

最小无模型用例：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_graph_execution.py::test_zero_agent_text_regex_output_ignores_unreachable_unknown_nodes -q
```

它执行文本 -> 正则 -> 输出，确认不可达未知节点不被激活，不联网也不持有真实模型凭据。

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
| 生产同源静态入口 | `test_server_workbench.py`、`test_server.py`、当前客户端 HTTP 与回执读取测试 |

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

## 2026-10-06 发布检查

2026-10-06 的发布目录验证使用 Python 3.12.8 和 Node.js 24.19.0：

- 全新 `.venv` 安装 `backend[test]` 成功，核心无需 smolagents，`pip check` 通过。
- 后端核心、串行示例、平台、HTTP 和 schema 定向 36 项通过；跨语言前端包定向 4 项通过。
- 独立兼容环境的工具等定向 52 项通过；与上述批次存在重叠，不累计为独立用例数量。
- 前端锁文件在独立目录执行 `npm ci` 成功；使用新安装依赖复测定向 7 文件、144 项通过。此前已启动的一次前端全量为 71 文件、663 项通过，批次不累计。
- 前端类型检查及生产构建通过；保留已有单 chunk 大于 500 kB 的体积提示。
- 全新环境后端以动态 loopback 端口和临时库启动，health、26 种节点目录及首页返回正常；检查后停止临时服务。
- 文档中的最小无模型文本处理用例在全新环境执行，1 项通过；与其他定向范围可能重叠，不累加。

首次后端测试因验证临时目录的父目录不存在出现 fixture 错误，修正验证环境后复测通过。安装保留现有 `lucide-vue-next` 弃用提示；本次不为发布升级依赖或改写图标实现。上述结果不是后端全量、浏览器人工或真实模型验收；本次没有真实模型请求，没有操作原运行数据库或常驻服务。

## 2026-10-07 阶段性整合

2026-10-07，Asia/Shanghai。阶段性整合提交 `8eb7e93` 已完成，`origin/main` 与退役前标签 `legacy-retirement-2026-10-07` 已推送并核对远端一致，整合完成时工作树干净。阶段 A 与第一批旧架构实际移除及受影响定向验证完成，阶段 C 部分通过；包版本仍为 `0.1.0`，未发布 1.0。此后的进度文档更新不属于该提交。

本次整合复用下列已有结果，不因提交或文档整理重跑全量；详细版本、命令、初轮失败、修复和空缺见 [实际移除记录](REPLACE-REMOVAL.md)。

| 范围 | 已有结果与边界 |
| --- | --- |
| 后端移除后定向 | 8 文件 55 项、最终补测 4 文件 64 项通过；范围有重叠，不相加 |
| 前端移除后定向 | 最终持久化/API 2 文件 26 项及类型/构建通过 |
| 独立安装 | 非 editable wheel、独立 venv 最终 33/33 通过，104 包文件匹配源码、38 退役路径不入包 |
| 限定 Mock 浏览器 | 两轮/首轮分叉后续聊、暂停/继续、B=503 后只重试 B、GraphChat 和完成历史重开；只读联合核对 74/74 通过，20 请求含 2 次受控 503 |
| 状态同步窄修 | 1 文件 13 项及类型/构建通过；JS 431.16 kB / gzip 137.41 kB，浏览器补验菜单自动同步 |

原始证据保留在本地 `.local/retirement-validation/` 和 `.local/release-acceptance-20261007/`，不提交数据库、凭据、日志或原始私有输出。当前结果不代表全新前端安装/生产联合部署、常驻收口、完整 PC 操作、真实供应商或全面故障专项通过，不能据此发布 1.0。

上一轮提交前另做本地整理检查：208 个后端 Python 文件的 AST 本地导入为 0 个缺失，wheel 的 104 个包文件完全匹配源码且哈希不变；10 份文档的 146 处本地链接、4 处锚点为 0 个失效；四个待推送历史提交及暂存区敏感信息扫描无候选、无运行数据，`git diff --cached --check` 通过。这些是上一轮整理检查，不计作本轮新增测试或运行验收；固定基线及退役前标签解引用提交不变。

上述整合后的待办已在本轮推进，最新结果见下节；不得把旧 wheel 或开发服务证据称为验证了本轮新源码。

## 2026-10-07 阶段 C 本地收口

以 `8eb7e93` 加本轮未提交源码为目标，三个并发组集中实现后按受影响范围验证；详细范围、命令所在日志、目标摘要、浏览器场景和发布门槛统一记录在 [阶段 C 本地收口](REPLACE-REMOVAL.md#阶段-c-本地收口)。

| 本轮范围 | 结果与口径 |
| --- | --- |
| 后端必要行为/诊断 | 176 个独立目标通过；初轮 136 正文通过、40 系统 pytest Temp setup `WinError5`，只复测这 40 在新本地 basetemp 全部通过；系统目录根因不关闭 |
| 部署/HTTP | 64 项及新增 CLI 1 项通过，共 65 个不重复目标；不与其他批次累加 |
| 前端请求/复制/布局 | 7 文件 116 项、类型及生产构建通过；JS 432.86 kB / gzip 137.88 kB |
| 当前安装/同源资产 | 18/18 核对通过，104 包文件及生产 index/JS/CSS 匹配当前源码/构建，51 当前节点、旧入口/未知资产拒绝及 Host/Origin 防线通过 |
| 浏览器后只读证据 | 23/23 核对通过，不是 23 个浏览器测试；7 Mock 请求为 6 成功/1 受控 B=503，只有失败 B 增加一次显式尝试，断连 marker 无 chain 或模型调用 |

本轮重新构建当前 wheel 并在原独立 venv 非 editable 重装，前端复用已有依赖但使用生产 `dist`，由临时同一 Python 服务提供 `/workbench/`、GraphChat、API；不称全新 venv/`npm ci` 或常驻。`--no-build-isolation` 初试因缺 setuptools 未成功，标准隔离 wheel 构建通过，初轮日志保留。

浏览器限定实际 1382×956：节点/连线、重复连线、撤销重做、保存/切换/完成编辑副本、重复启动/保存/重试、资源 stale 冲突、精确同源 Chat、断连核实及服务重开/页面重载保护均有观察。1024×768 override 未生效，不计通过；此前窄侧栏问题不据此关闭。断连是在无 active/in-flight 时停止本轮临时服务，不是发送后丢回执或活动恢复；核实返回 `application_identity_missing` 时仍保留原 key 和输入锁，Mock 总数保持 7。

证据保留在 `.local/phase-c-closeout-20261007/`，包含各组日志、当前 wheel、`target-install-result.json`、`browser-evidence-result.json`、浏览器观察/截图和 `cleanup-result.json`。停止前 in-flight=0，已精确核对并关闭自己的 Python 启动/监听进程、8873/8998 两端口和三个浏览器页。没有收费调用、产品库或常驻操作，本轮修改尚未提交/推送。

**本地收口完成，整体阶段 C 与 1.0 未完成。**还需当次授权的同版常驻切换及目标代码真实供应商验收；全部门槛通过后更新版本、发布记录和新基线。全面故障、移动适配和活动跨进程恢复继续后置，DEMO-05/06、COND、LATER 及暂停事项不因本批通过启动。

## 2026-10-07 阶段 C 限定验收（发布前历史记录）

上节保留的是常驻与真实调用门槛尚未执行时的历史状态。随后经当次明确授权，完成本机常驻和限定真实供应商验收，再将包元数据更新为本地 `1.0.0`，重新构建、独立安装并切换常驻，当轮没有 Git 发布。下表保留当轮版本和结果；用户随后指定的当前版本及发布身份见 [0.2.0 基线](BASELINE-0.2.0-2026-10-07.md)，不得把当轮本地 1.0.0 安装视为 1.0 正式版。

| 最终门槛 | 已完成结果 |
| --- | --- |
| 初次常驻核对 | `0.1.0` 常驻在 `127.0.0.1:8765` 启动，21/21 通过；非 editable 独立环境、104 包文件、当前节点目录/SQLite schema、监听进程身份及同源资产一致 |
| 限定真实供应商 | 标准模型包及内核，一轮 A/B 各 1 次实际 HTTP，均 200，整图 `succeeded`；每次 `max_tokens=128`，耗时 6.078 秒，不重试 |
| 完成历史重开 | 原库以 SQLite `mode=ro`、`query_only` 直接重开，链/会话/节点运行/产物/事实/对象/manifest/检查点一致；不构造服务、不派发模型，无活动执行 |
| 最终 1.0 常驻 | `1.0.0` wheel 构建、非 editable 安装及常驻切换完成，最终 21/21 通过；104 后端包文件与目标源码/wheel/安装一致，工作台、GraphChat 及 API 同源服务正常 |
| 最终工作台产物 | `VITE_CHAT_UI_URL=http://127.0.0.1:8765/` 的类型检查及生产构建通过；`index-CqJ_BbyF.js` 为 432.86 kB / gzip 137.87 kB，最终常驻提供的 index/JS/CSS 与稳定 dist 一致 |

真实验收使用更新版本元数据前的 `0.1.0` 独立安装及目标业务源码。之后仅将版本元数据改为 `1.0.0`，104 个后端包文件保持相同并经最终安装核对，逐字节等价记录在 `package-equivalence.json`；不把它写成对最终 1.0 wheel 又进行了一轮收费回归，也不是浏览器触发常驻模型的付费端到端验收。前端不把包版本写入 bundle，本次版本变更未重复构建或运行前端测试。

本次要求至少 15 项，且仅在 `flag=true` 时适用。A 被明确要求给出错误草稿 `At least 5 items; apply always.`；B 确认同时收到原需求与该精确草稿，并返回 `At least 15 items, never 5; apply only when flag=true.`。确定性检查通过“15 而非 5”和“条件适用而非无条件”，不只以 HTTP 200 判断业务质量。请求模型为 `deepseek-chat`，响应报告 `deepseek-flash`，两者按原事实分别记录。

用量为 A 输入 444、输出 49、合计 493；B 输入 470、输出 56、合计 526；共输入 914、输出 105、合计 1019 token，缓存命中 0。输出上限为两次合计最多 256 token，不是输入 token 或金额硬上限。协议、传输或业务质量失败均不自动重试；直连 HTTPS，不自动使用代理、跳转或替代端点。发送保护属于本次验收，不是新增默认产品预算能力。

完成历史重开保存了 18 个节点运行、24 个产物、22 个运行事实（其中 6 个模型事实）和 3 个对象的一致性检查，10 项只读核对通过。真实验收使用独立数据库，没有修改产品库、历史会话或常驻配置。最终 1.0 临时读取服务还取得浏览器正文重开及同样 10 项一致性证据，未新增模型请求；初次本地读取启动参数拼装错误修正后通过，原日志保留。临时服务/页已关闭，8765 常驻保留。证据在 `.local/phase-c-closeout-20261007/final-release/` 的 `resident-initial-result.json`、`resident-final-result.json`、`frontend/`、`real-acceptance/result.json`、`real-acceptance/readonly-reopen.json`、`real-history-browser.json`、`real-history-1.0.png` 和 `readback-post/readonly-reopen.json` 等文件；完整请求、响应和私有运行材料仅留本地，不复制到公开文档。

常驻是本机显式管理的后台进程，没有注册 Windows 服务或自启动；停止/切换须另行确认无 active/in-flight 或结果未知业务，持久化空闲检查不替代该前提。未执行全面进程/锁/磁盘故障、发送后丢回执、活动内核跨进程恢复、窄屏/移动适配或全量测试。HIST-01/03 的旧超时、HIST-02 系统 Temp `WinError5` 根因、HIST-04 诊断异常及原六回合日志写入退出 1 均不因本次通过关闭。DEMO-05/06 延后至 1.0 之后，COND/LATER 选做，节点打组和历史正文编辑/删除继续暂停；项目许可及外部生产交付不在本次本地门槛内。

## 2026-10-07 0.2.0 基线发布检查

用户指定本次提交为 0.2.0 后，仅调整后端项目及前端根/锁文件版本，标准隔离构建、非 editable 安装并切换常驻。三个版本 wheel 的全部 104 个包文件与当前源码逐字节相同，无重复/新增/移除；前端依赖和稳定 bundle 不变。版本一致性与安装/进程/资产/SQLite 只读核对 21/21、`pip check` 通过，原固定基线和退役标签保持不变。

新证据在 ignored `.local/baseline-0.2.0-20261007/` 的 `package-equivalence-0.2.0.json`、`resident-before-switch.json`、`resident-result-0.2.0.json` 及构建/安装日志。此次只做发布一致性检查，没有重跑业务、前端测试/构建、全量或收费调用；上述真实验收和浏览器重开通过同字节证据承接，不冒称新增 0.2.0 付费端到端验收。源码身份由 `v0.2.0` 固定，版本基线不等于 1.0 正式版或外部生产交付。
