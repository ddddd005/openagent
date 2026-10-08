# 工作区梳理与验收：2026-10-09

日期：2026-10-09，Asia/Shanghai。范围为 `openagent` 当前未提交改动及其直接影响面；用户授权先梳理、验收，不进入 Gemini 开发。

验收阶段结论：本批工作区改动已分类，发现的存档边界问题及过时测试目标已限定修正，前端全量和后端定向、真实离线浏览器验收通过。**这是可整理提交的工作区快照，不是发布版本或后端全量绿色证明。**第 1-6 节保留验收结束时尚未暂存、提交或建分支的状态；用户随后授权继续，Git 整理与开发线收口见第 7 节。本次没有推送或部署。

## 1. 仓库与开发边界

| 项目 | 本轮核对结果 |
| --- | --- |
| 唯一产品开发主仓 | `openagent` |
| 当前本地分支与 HEAD | `main`，`17617a1a460748c779d64177eb50d8ec5f94e7e8` |
| 远端只读查询 | 只有 `main`，`79c145720ec3c9b5dfe5b13628a13fd863485c02` |
| 本地未推送提交 | 一个：全局酒馆资源与独立聊天分叉 |
| 开发分支 | 尚无 `develop` |
| 当前 Git 工作树 | 只有 `openagent` 当前检出 |
| 检查开始时的工作区 | 22 个已跟踪文件有改动，另有 16 个待纳入源码/测试/文档文件及三个临时测试目录 |

`step1` 的 `master` 和 `step1/smolagents` 的 `phase1-g3-implementation` 是独立历史来源仓库；根目录 `smolagents` 是第三方参考仓。它们不是 `openagent` 的开发分支，不在这些目录继续实现新产品能力。旧目录启动 BAT 当前仅转发到 `openagent/start.bat`；该工作区外快捷方式没有纳入本仓发行文件，本轮未修改它。

后续建议固定 `main` 为验收通过的稳定线、`develop` 为唯一串行开发线。先整理本批提交并明确稳定基线，再建立开发线；只有明确的并行任务才使用短期功能分支，并指定合回目标。本轮没有执行这些 Git 写操作。

## 2. 未提交改动归属

以下是逻辑批次，尚未暂存或拆分提交。部分测试、文档共用同一文件，后续整理应按实际内容归属，不能把共用文件整份重复计入多个批次。

| 批次 | 主要文件 | 行为与边界 |
| --- | --- | --- |
| P5 联合验收补充 | `backend/tests/test_tavern_joint_browser.py`、`test_tavern_workbench_browser.py`、`tavern_workbench_browser.cjs`、`test_tavern_fork_http.py`、`docs/tavern/P5-JOINT-ACCEPTANCE-2026-10-08.md` | 实际 HTTP/浏览器、多轮与分叉、资源管理；测试资源定位使用安装包来源，不新增酒馆产品能力 |
| 空图与 schema v2 初始化 | `frontend/src/domain/workflowGraph.ts`、`serialAgentDemo.ts`、`plugins/tavern/tavernDemo.ts`、`stores/workflowGraph.ts` 及对应 domain/store/wiring 测试 | 新空图和示例统一创建，清空旧选择；旧 v1 文档不因读取而改写 |
| 当前源码启动与代理 | `start.bat`、`scripts/*.ps1`、`frontend/vite.config.ts`、`adapters/devProxyConfig.test.ts`、`backend/tests/test_dev_launcher.py`、`dev_launcher_workflow_browser.cjs` | 明确源码/数据库/端口；普通可见控制台，身份核对后仅停止自有进程；不自动安装、杀占用者或迁移旧库 |
| 本机存档恢复与供应商状态 | `frontend/src/App.vue`、`adapters/workbenchPersistence.ts`、`stores/workbenchPersistence.ts`、`components/WorkbenchStorageRecovery.vue`、`CurrentProviderPanel.vue`、`workbenchRecovery.test.ts`、`CurrentModelConfiguration.test.ts`、`backend/tests/workbench_recovery_browser.cjs` | 原文导出、明确另建独立保存位置；保留原记录和请求；区分供应商为空与读取失败 |
| 导航与既有收口记录 | `README.md`、`frontend/README.md`、`docs/QUICKSTART.md`、`docs/tavern/README.md`、`PLAN.md`、两份本机修补记录 | 文档与入口变化；历史验证记录不改写成当前结果 |
| 本轮审查修补 | `.gitignore`、`frontend/src/adapters/promptResourcesApi.test.ts`、`backend/tests/test_graph_history_definition.py`、本记录和开发说明 | 排除已确认临时目录、修正过时测试、提供唯一验收入口 |

## 3. 本轮发现与处理

### 独立存档的保存键选择

原初始化逻辑在独立键与原键内容完全相同时跳过独立键，后续保存可能落回原键；同内容损坏记录也会错误提供再次恢复入口。

新增两个回归先复现失败，再将选择条件改为只判断独立键是否存在。现在无论两个键原文是否相同，都以独立键为当前保存位置；损坏独立记录继续阻断写入，不提供第三个保存位置。修补没有读取、迁移或改写用户实际浏览器记录。

### 提示词 API 的旧版本断言

前端首轮全量为 879 通过、2 失败；失败仍是既有测试把已支持的 schema 2 当作非法版本。非法测试改用未支持的版本 99，并新增 schema 1/2 的实际 read/list 接受用例。产品提示词校验器和 API 行为未修改。

### 历史定义测试的退役节点身份

图回归首轮为 40 通过、7 失败，全部位于 `test_graph_history_definition.py`。测试图已采用 `tools.text / tools.regex`，故障注入仍操作退役的 `workflow.*`。

仅修正故障注入目标，并断言声明删除实际影响一条记录；保留缺失/失形/实现变化拒绝、原历史精确恢复、父状态不变和运行期间修订隔离断言。不恢复旧节点或改动后端产品实现。

### 临时目录

`.gitignore` 新增仓库根限定的 `/.codex-tavern-p1-*/`，防止三个历史 pytest 临时目录被误提交。目录及其文件保留，没有删除或搬移。`.local/` 的证据、数据库、截图、构建和服务 metadata 继续保持忽略。

## 4. 验收结果

所有本轮证据位于：

```text
.local/baseline-audit-20261009-500c18d8/
```

| 验证范围 | 结果 | 证据 |
| --- | --- | --- |
| 前端全量最终 | 63 文件，885 项通过，零失败/跳过 | `frontend-final.json`、`frontend-final.log` |
| TypeScript 与生产构建 | `vue-tsc --noEmit`、Vite `/workbench/` 构建通过 | `build.log`、`workbench-port.json` |
| 酒馆与共享 HTTP 专项 | 158 个目标最终通过：首轮 156 通过、2 环境跳过；补齐 Node/Chromium 后两个目标通过 | `backend-tests.xml`、`backend-browser-recheck.xml` |
| 图服务、应用及历史定义 | 四文件最终 47 项通过，零失败/跳过 | `graph-final.xml` |
| 最终启动器及真实浏览器 | 三文件最终 19 项通过，零失败/跳过 | `browser-final.xml`、`browser-final-temp/` |
| PowerShell 语法 | 三个脚本零解析错误 | `powershell-syntax.json` |
| 空白检查 | `git diff --check` 通过；仅换行规则提示 | 最终终端核对 |

上述范围分别登记，不把初轮、修补前、复测和历史 P5 数量相加当作新增覆盖。首轮后端两个环境跳过伴随 Windows 默认 GBK 子进程解码警告；补跑以 `-X utf8` 和明确 Playwright/Chromium 环境执行，两个目标均无警告通过。

本轮浏览器覆盖实际 HTTP 与发行页面，模型为离线替身，没有收费请求：

- 1440px / 1024px 工作台：新空图、串行示例、酒馆草稿、切换后选择清零和统一画布。
- 同两宽存档恢复：损坏 JSON、未来格式、损坏独立记录、下载原文一致、原键不变、独立键重开及供应商空状态。无业务写入请求。
- 同两宽资源管理：Lorebook 创建/编辑、提示词复制/排序、真实定义保存、精确酒馆入口及被动草稿。业务写入只发生在独立测试数据库。
- 酒馆桌面和 390px：离线 Agent 多轮、完成检查点分叉、子续聊、父不变、重开、丢响应后只读核实和内容安全。

关键截图已目检：1024px 损坏记录提示、1440px 独立恢复与空供应商、1024px 已保存引用、390px 酒馆聊天。浏览器结果中的页面/console/资产异常为零；资源管理的横向溢出和控件重叠检查为零。

可复跑命令使用当前仓库依赖，证据目录须另选新路径，避免 pytest 清理旧证据：

```powershell
# 在 frontend 目录
npm.cmd test
$env:VITE_CHAT_UI_URL = "http://127.0.0.1:<独立空闲端口>/"
npm.cmd run build -- --base=/workbench/

# 在 openagent 目录
$env:PYTHONPATH = Join-Path (Get-Location) "backend/src"
$env:NODE_PATH = Join-Path $env:USERPROFILE ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"
$env:TAVERN_CHROMIUM_EXECUTABLE = Join-Path $env:LOCALAPPDATA "ms-playwright/chromium-1228/chrome-win64/chrome.exe"
$env:TAVERN_WORKBENCH_PORT = "<与构建一致的独立空闲端口>"

$tests = Get-ChildItem backend/tests/test_tavern*.py |
    Where-Object { $_.Name -notlike "*_browser.py" } |
    ForEach-Object { $_.FullName }
.\.venv\Scripts\python.exe -X utf8 -B -m pytest -q @tests backend/tests/test_plan13a_client_http.py backend/tests/test_plan13b_client_http.py --basetemp "<新证据目录>/backend-temp"
.\.venv\Scripts\python.exe -X utf8 -B -m pytest -q backend/tests/test_graph_service.py backend/tests/test_graph_application.py backend/tests/test_graph_application_history_integration.py backend/tests/test_graph_history_definition.py --basetemp "<新证据目录>/graph-temp"
.\.venv\Scripts\python.exe -X utf8 -B -m pytest -q backend/tests/test_dev_launcher.py backend/tests/test_tavern_joint_browser.py backend/tests/test_tavern_workbench_browser.py --basetemp "<新证据目录>/browser-temp"
```

## 5. 服务与数据保护

启动器验收使用空闲临时端口、独立 SQLite，实际服务保持普通可见控制台，不使用隐藏后台服务。BAT 转发通过真实 `cmd.exe` 预检；启动和停机通过真实脚本及本次 metadata 验证。

最终启动器实例为后端 `54913`、前端 `54914`；资源管理生产构建与临时 HTTP 使用 `55471`。自有临时服务在夹具 `finally` 中停止，测试 SQLite 和证据保留。具体 metadata、停止回执、端口释放与原服务 PID 核对登记在 `after.json`。

证据中的 `failed-stopped` 来自故意提供无效 SQLite 的失败清理测试，不代表验收失败。`qa-stale-identity-*` 是保留的 PID 身份故障注入副本，其 `ready` 字段不代表仍有服务运行；最终均无匹配的自有进程和监听端口。

用户原有服务仍使用 `8765 -> PID 59696`、`5178 -> PID 111828`，本轮未停止或重启。没有通过默认端口执行业务写入，没有操作 `.local/dev/workflow.sqlite` 或清空用户浏览器存储。

## 6. 收口边界与下一步

本批工作区范围验收通过，可进入整理提交和稳定基线确认；仍须区分以下未完成事项：

- 没有运行全部后端测试；本次七项历史定义测试修正不代表 BACKLOG 的其余 TEST-01 债务已关闭。
- 没有重复冷 wheel 安装、发布验收或真实供应商调用；历史 P5 安装证据仍按原记录解释。
- 用户实际损坏存档未读取、未恢复；目前仅证明合成记录上的隔离保护。
- 没有建立 `develop`、改变版本号、创建标签、推送或切换常驻。
- Gemini、思考签名回传和工具协议适配尚未实现。

后续提交应收录明确的源码、测试和文档批次，不纳入截图、数据库、临时目录、wheel 或凭据。完成提交后再以精确提交身份确认 `main` 的稳定基线，并建立 `develop` 承接新能力。

## 7. Git 整理与开发线收口

用户在本报告验收完成后授权继续。整理前重新核对 `after.json`：43 个待纳入文件均与验收时 SHA256 一致，暂存区为空。未重新实现产品能力，未覆盖用户原有改动；按依赖顺序形成以下提交：

| 提交 | 归属 |
| --- | --- |
| `73ed6eb0d78706407d2556a26b8f359a20c30712` | 新空图、串行和酒馆草稿统一 schema v2；锁副本和选择清零及对应回归 |
| `487419a15fe31d5a17ecf357c1d6413722389252` | P5 真实离线浏览器、安装态资源定位及原始 P5 记录 |
| `c309f7afcf9cba49cda77fcff5f50dc8c7ec5af6` | 可见控制台 BAT、端口/代理/自有进程保护、独立本机存档恢复及浏览器回归 |
| `aa79981013d3a0a06fb7b73b3751022fc34650e8` | 过时提示词版本断言、退役节点故障注入目标修正 |
| 本节所在文档收口提交 | 当前开发规则、导航、历史/当前状态区分和限定临时目录忽略 |

启动器测试直接引用 P5 的浏览器运行环境，并同时验收本机恢复界面，因此先收录 P5，再把启动与恢复作为完整依赖组提交，不让测试引用尚未纳入的脚本或界面。原有酒馆提交 `17617a1` 及所有更早历史不改写，历史 P5/入口/恢复记录的当时结果不倒改。

完整代码和测试基线为 `aa79981013d3a0a06fb7b73b3751022fc34650e8`；最后一组仅收录上述文档和忽略规则，不改变已验收产品代码。本次最终本地 Git 基线是该文档收口后的 `main`，`develop` 从同一最终提交建立并成为当前开发分支。两个分支建立时身份一致，后续新能力只推进 `develop`，通过明确验收后再纳入 `main`。精确最终身份可用以下命令核对；后续开发后不再要求两条线保持相同：

```powershell
git log --format="%H %s" -1 -- docs/WORKTREE-ACCEPTANCE-2026-10-09.md
git rev-parse main
git rev-parse develop
git status --short --branch
```

收口未创建发布标签、改变版本号、推送、安装或重启服务，没有 Gemini 产品实现。原 `8765` / `5178` 服务在整理前仍为 PID `59696` / `111828`，本轮只做只读核对。运行服务与分支状态分别判断；SQLite、浏览器存档和证据未纳入 Git。测试范围继续以第 4、6 节为准，不把本次提交动作当作额外测试、冷安装验证或关闭全部 TEST-01 债务。
