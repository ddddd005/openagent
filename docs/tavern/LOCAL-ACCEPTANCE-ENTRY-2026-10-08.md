# 本地验收入口与空图初始化修补

日期：2026-10-08，Asia/Shanghai。用户授权修补问题、更新 BAT，并按三路并发完成工作流初始化、启动入口和独立验收。本文为 P5 后的本地补充记录，不回写或替换历史 P5 证据。

## 1. 原因与范围

原 `step1/docs/启动服务.bat` 调用的 PowerShell 脚本仍指向 `step1/smolagents/phase1_agent` 和 `step1/workflow-frontend`。它没有锁定某个 Git commit，而是选择了旧工程目录和旧环境；新增酒馆代码位于 `openagent`，因此旧页面的节点目录看不到新包。此前针对 `openagent` 的源码检查与隔离 P5 验收不能直接说明用户当时打开的旧页面。

空图与有内容的图原本共用 `frontend/src/components/GraphWorkbench.vue`，不是逐工作流复制界面。新空图仍从 schema v1 创建，与示例 v2 及三条创建路径的选择状态清理出现差异。本次修补新建格式与初始化，不迁移产品数据库、不重写已有 v1 工作流，也不改 Agent 有效上下文。

## 2. 当前入口

- 主入口：`openagent/start.bat`，调用本目录 `scripts/start-services.ps1`。
- 旧 `step1/docs/启动服务.bat` 只转发到 `openagent/start.bat`，不再拥有另一套启动目录。
- 后端强制从本工作树 `backend/src` 导入，并核对实际 `phase1_agent/server.py` 来源。Python 优先本项目 `.venv`，其次 PATH；可显式 `-PythonPath`，不自动搜索旧工程环境。
- 前端使用同工作树 `frontend` 的 Vite 开发源码，不使用残留生产 `dist`。脚本为代理设置 `OPENAGENT_API_PORT`，为聊天入口设置同一后端的 `VITE_CHAT_UI_URL`。
- 酒馆版本从当前源码 `DEFAULT_PACKAGES` 推导，再以其 manifest 导出节点检查预检和实际协议 v2 HTTP 目录；不是把启动器锁到 P4 提交或固定酒馆版本。
- 默认端口仍为后端 `8765`、前端 `5178`。端口被占用时明确拒绝，既不停止已有服务，也不自动换端口。
- 默认数据库为 `openagent/.local/dev/workflow.sqlite`；显式相对 `-DatabasePath` 以 `openagent` 为基准。没有自动选择旧数据库、合并数据库或清空浏览器保存。

双击 BAT 前应自行停止已知旧服务。仅更新文件不会让仍在运行的旧进程换成新代码。本次测试不停止旧 `8765 / 5178` 所有者，也未自动替用户启动默认常驻服务。

无服务预检示例：

```powershell
.\start.bat -CheckOnly -BackendPort 29121 -FrontendPort 29122 -NoBrowser
```

指定独立数据库并启动：

```powershell
.\start.bat -DatabasePath ".local\dev\workflow.sqlite"
```

脚本输出实际源码、数据库、入口、日志与带 `metadata_path` 的机器可读结果。`CheckOnly` 不启动服务、不创建数据库或数据库父目录。缺依赖时明确失败，不自动安装、拉取代码或联网升级。

当前后端没有旧 `--mode` 参数；显式 `-Mode offline` 等旧参数被拒绝。模型调用由工作流节点配置控制，启动器不能承诺所有工作流离线。验收测试仅做目录、草稿与切换，不触发模型运行。

## 3. 选择与格式

所有新空图、串行示例、精简示例和酒馆草稿使用 schema v2。三条创建入口共用草稿登记流程，并清空节点、连线选择。打开其他工作流时重置相同视图状态；旧 v1 文档仍能读取，不因加载而重写。

画布仍是同一 `GraphWorkbench / VueFlow`：空图只是节点为空，相关选择操作禁用，不另建空图 UI。工具栏、画布、检查器尺寸及节点菜单不随空图和有内容图切换改成另一套界面。全局 Lorebook 引用/触发位于“类酒馆”分组，三种聊天记录节点位于 `Tavern` 分组。

本批实际菜单名称：

| 菜单名称 | 节点 ID |
| --- | --- |
| 全局 Lorebook 引用 | `lorebook.global-reference` |
| 全局 Lorebook 触发 | `lorebook.global-activate` |
| Read tavern chat display state | `tavern.chat.output` |
| Append tavern chat display references | `tavern.chat.append` |
| Present tavern chat display references | `tavern.chat.presentation` |

## 4. 进程归属与停止

每次启动生成 `.local/service-starts/<launch>/services.json`，记录工作树、数据库、端口和本次进程身份。失败清理只处理本次启动返回的进程对象和经过核对的子进程，不按端口批量杀进程。

手工停止本次启动：

```powershell
powershell.exe -NoProfile -File ".\scripts\stop-services.ps1" -MetadataPath ".local\service-starts\<launch>\services.json"
```

停止脚本要求 metadata 属于当前工作树；完整核对创建时间、可执行路径、命令行和进程树，先验证再停止。PID 身份改变、错误来源或路径越界均拒绝。数据库与日志保留。启动前不能读取本进程 CIM 身份时拒绝启动；意外无法捕获完整子树身份时会明确报告清理范围，不声称已证明所有未知子进程。

## 5. 本地验证

新增 `backend/tests/test_dev_launcher.py` 和 `backend/tests/dev_launcher_workflow_browser.cjs`。真实测试只使用未占用临时 loopback 端口和测试独立 SQLite，普通 HTTP、资产与 API 不伪造；短命 pytest 夹具在 `finally` 中按自身 metadata 停止。

覆盖内容：

- 当前 BAT 与含中文、空格路径的旧 BAT 实际 `cmd.exe` 转发参数，两者均预检为当前 `openagent`。
- 旧 `PYTHONPATH` 注入下仍优先当前后端源码；预检不创建数据库或其父目录。
- 两个端口的已有 listener 均拒绝且保留；缺解释器及旧模式参数明确拒绝。
- 无效 SQLite 可通过无触库预检，但真实启动失败后仅清理本次进程，原数据库字节不变。
- 真实后端和 Vite 代理返回完全一致的协议 v2 节点目录；实际前端服务读取当前 schema v2 与统一创建逻辑源码。
- 错误 metadata 和复用 PID 创建时间不符均拒绝，不停止仍健康的测试服务。
- 1440px 与 1024px 实际浏览器：首次空图、串行图、选中节点后新空图、酒馆草稿、切回空图、打开节点菜单再新建；schema v2、选择清零、单一 VueFlow、同工具布局和酒馆菜单节点均检查。

最终整文件 **13 项通过、无跳过**，耗时 94.75 秒；其中包含两个实际 BAT 入口和两个真实浏览器宽度。运行命令：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) "backend/src"
$env:TAVERN_CHROMIUM_EXECUTABLE = Join-Path $env:LOCALAPPDATA "ms-playwright/chromium-1228/chrome-win64/chrome.exe"
python -B -m pytest -q backend/tests/test_dev_launcher.py --basetemp .local/dev-launcher-acceptance-final4-20261008
```

最终证据根目录为 `.local/dev-launcher-acceptance-final4-20261008/dev-launcher0/`，包含 `launcher-result.json`、`stop.stdout.txt`、保留的独立 `disposable.sqlite`，以及 `workflow-ui-1440/`、`workflow-ui-1024/` 两处浏览器证据。每个宽度有 `result.json`、runner stdout/stderr 和五张截图：`fresh-empty`、`serial-populated`、`new-empty-after-node-selection`、`tavern-populated`、`empty-after-tavern`。两宽页面异常、资产异常、console 错误与 command 请求均为零。

最终实例端口为 `62175 / 62176`，metadata 为 `.local/service-starts/20261008-204910-c337d02f/services.json`。测试 `finally` 已按身份停止本次全部记录进程并确认两个端口释放，独立 SQLite 保留。测试结束后另核对旧常驻端口仍为 `8765 -> PID 79124`、`5178 -> PID 74552`，没有停止或替换这些进程。

早期试跑保留在相邻忽略目录。首轮遇到 Windows 启动子进程继承捕获 pipe 导致测试等待 EOF，已按其自身 metadata 停止，再将 QA 捕获方式改为临时文件；后续仅修正测试 cmd 引号、目录描述符预期、未保存 anchor 定位和真实英文菜单名。没有为测试修改产品 API 或节点名称。所有早期测试实例也已停止，不把失败/跳过试跑计入最终通过数。

本次使用已安装 Chromium 1228，不下载浏览器，不接收费模型，不修改用户旧库或旧常驻端口。真实测试没有向默认端口执行启动或停止操作。

## 6. 已知基线

本次全前端测试为 62 个文件、862 项通过及 2 项失败。两项都位于已有 `promptResourcesApi` 旧断言，已在 HEAD 冷源码 `.local/workflow-init-baseline-20261008/` 复现；不在本批做无关修复，也不宣称全量绿色。`vue-tsc --noEmit` 和 Vite 生产构建通过。

P4/P5 记录中的七项历史后端旧节点身份失败仍保留为原基线，不把历史重复验证累加成本批独立通过数。P4 提交仍为 `17617a1`，本次不提交、推送或部署。
