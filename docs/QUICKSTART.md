# 快速启动

本文面向当前 `main` 源码。2026-10-09 的现行测试版已晋升并正常推送为主干；旧正式版与原开发机的固定安装快照只作参考，不是新版启动入口。正常 Git 推送不会自动部署或重启服务。

以下命令从仓库根目录执行；界面操作见 [使用手册](USER-GUIDE.md)，开发规则见 [开发说明](DEVELOPMENT.md)。

## 环境与安装

- Python 推荐 3.12；包声明最低 3.10，主要验证使用 3.12。
- Node.js 推荐 24.x，带 npm。锁定的 Vite 要求 `^20.19.0 || >=22.12.0`，Vitest 要求 `^22.12.0 || ^24.0.0 || >=26.0.0`；不要使用 Node 18。
- 安装依赖需要网络。真实模型运行另需有效凭据、账号和供应商网络。
- `vendor/smolagents` 仅为来源归档，不需要安装。

在将被启动的源码工作区安装：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ./backend
Set-Location frontend
npm ci
Set-Location ..
New-Item -ItemType Directory -Force .local | Out-Null
```

无需激活虚拟环境。非 Windows 系统可用 `python3.12 -m venv .venv` 和 `.venv/bin/python`，采用下文手动启动；BAT 与 PowerShell 启动脚本面向 Windows。

克隆后主工作区若干净检出 `main`，正式 BAT 直接使用它。若根工作区位于 `develop`，正式入口需要唯一检出的 `main` 工作区；未建立时显式执行：

```powershell
git worktree add .local/stable-main main
```

然后在 `.local/stable-main` 中重复安装步骤，再回到入口工作区。虚拟环境和 `node_modules` 不随 Git 推送，不因创建 worktree 自动复制；启动器不会自动安装依赖。不要复用其他项目的环境或数据库。

## Windows BAT

| 入口 | 选择源码 | 工作台 | 后端 / 通用聊天 | 默认数据库 |
| --- | --- | --- | --- | --- |
| `start.bat` | 唯一、干净且匹配本地 `main` 的工作区 | `http://127.0.0.1:5178/` | `http://127.0.0.1:8765/` | 入口根目录 `.local/dev/workflow.sqlite` |
| `start-dev.bat` | 当前根工作区的 `develop` | `http://127.0.0.1:5179/` | `http://127.0.0.1:8766/` | 入口根目录 `.local/develop/workflow.sqlite` |

酒馆壳在对应后端 `/tavern/`；实际使用应从已保存图打开带身份的入口，不从空首页创建业务。

先做不启动服务、不开数据库的检查，再正常启动：

```powershell
.\start.bat -CheckOnly -NoBrowser
.\start.bat
```

开发验收使用：

```powershell
.\start-dev.bat -CheckOnly -NoBrowser
.\start-dev.bat
```

`-ResolveOnly` 仅输出选中的分支、提交、路径与端口，检查依赖用 `-CheckOnly`。正式入口遇到缺失、有修改或不匹配的 `main` 会拒绝，不回退 `develop`；开发入口拒绝正式默认端口 `8765/5178` 和正式默认数据库。

BAT 使用可见前后端控制台显示日志，不隐藏后台启动。它不切分支、不拉取 Git、不接管或终止已有进程；端口占用会拒绝。可显式指定另一组空闲端口、独立库和解释器：

```powershell
.\start.bat -BackendPort 8876 -FrontendPort 5189 -DatabasePath .local/acceptance/stable.sqlite -NoBrowser
```

相对数据库路径以入口根目录解析，不以选中的 `main` 工作区解析。解释器优先选目标工作区 `.venv/Scripts/python.exe`，否则使用 PATH 中的 Python；可用 `-PythonPath` 显式指定。内部 `scripts/start-services.ps1` 不提供分支隔离，日常使用 BAT。

启动器同步 `/api` 代理与聊天链接，并检查当前源码、酒馆包和前后端目录一致。旧 `-Mode offline` 已退出；启动和只读浏览不会调用模型，但显式运行真实模型节点仍可能收费。

如果已有数据库的精确包选择缺少当前酒馆包，启动器拒绝本次新进程，保留原选择。请使用独立新库验收，或在确认业务状态后显式管理该库启用包；它不会自动换包锁以掩盖错误。

## 数据升级

当前 SQLite 存储版本为 v15。首次用新版打开已有库时，按明确退役身份删除旧测试图及关联定义、会话、运行、对象、事实、manifest 和回执，旧 schema 1 提示词资源也退役；不是无损迁移旧流程。浏览器重读也移除明确含旧路线的整图条目。升级前应备份自己的库和浏览器记录。

无关联现行数据、供应商和 schema 2 提示词资源保留，未知插件/版本不会仅因缺失被删除。损坏或无法归类的浏览器记录继续阻止覆盖，见 [存档恢复](tavern/LOCAL-STORAGE-RECOVERY.md)。原开发机归档数据库不随克隆分发，正式和开发库不自动合并。

新后端库默认启用现行独立包；显式空包选择不会被默认补回。缺少已保存的精确包时保留原选择和可读冻结历史，不恢复旧执行。若仍有活动链，须补回所需精确安装并显式关闭后才能重配；丢失的活动内核不能跨进程恢复。

## 配置后端凭据

在将要启动 BAT 或手动后端的 PowerShell 中注入对应密钥，不把正文填入代码、节点或供应商表单。以 Gemini 为例：

```powershell
$secret = Read-Host "GEMINI_API_KEY" -AsSecureString
$env:GEMINI_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
Remove-Variable secret
.\start.bat
```

DeepSeek 改用 `DEEPSEEK_API_KEY`。环境只传给随后创建的进程，不能改变已运行后端；双击 BAT 也不会继承另一个 PowerShell 临时设置的环境。更换凭据需要确认没有活动或待核实业务，再停止并重新启动已知服务。结束后可在设置环境的终端移除相应变量。

环境变量不是加密存储，不上传完整进程环境或请求头。供应商表单只选 `env:GEMINI_API_KEY` / `env:DEEPSEEK_API_KEY` 引用。按 [使用手册](USER-GUIDE.md#模型与思考) 配置协议、模型名、上下文容量与思考；保存成功不等于模型已可用。

## 首次运行

1. 打开工作台。首次可读且没有本机存档时建立空普通图。
2. 在“供应商”新增当前资源，选择协议、地址和后端凭据引用并保存。
3. 在“工作流”创建串行 Agent 示例或精简示例；也可从“内容”创建酒馆示例。
4. 配置模型来源的资源、模型名称、容量和参数；使用 Gemini 时同步切换模型来源与全部 Agent 的协议并配置思考。
5. 检查提示词与连线，保存工作流。示例仅生成草稿，不自动调用模型。
6. 编辑“本次输入”为 `{"text":"你好，请用一句话回答。"}`，显式“启动”；或保存后打开对应聊天前端再提交。
7. 查看整图状态、节点“状态与结果”和展示输出。模型 HTTP 200 不等于整图完成。

普通串行示例为 18 个节点，精简示例多两个策略节点；A/B 分别维护上下文，A 的本轮分析作为 B 的材料。默认 Agent 用 `final_answer` 交付含 `text` 的答案。详见 [使用手册](USER-GUIDE.md)。

## 手动启动

不用 BAT 时，在两个 PowerShell 终端运行同份源码。终端一：

```powershell
New-Item -ItemType Directory -Force .local/manual | Out-Null
.\.venv\Scripts\python.exe -u -m phase1_agent.server --port 8766 --database .local/manual/workflow.sqlite
```

终端二：

```powershell
Set-Location frontend
$env:OPENAGENT_API_PORT = "8766"
$env:VITE_CHAT_UI_URL = "http://127.0.0.1:8766/"
npm run dev -- --port 5179 --strictPort
```

工作台为 `http://127.0.0.1:5179/`。`--database` 必填，手动方式父目录须先建立。服务仅监听 loopback；保持同源访问，不从其他站点发起写请求。

可在另一个终端只读检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8766/api/health
Invoke-RestMethod http://127.0.0.1:5179/api/graph/node-types/v2
```

## 同源构建

工作台、GraphChat 与 `/api` 可以由同一个 Python 进程提供，不需要 Vite。`npm run preview` 不是完整部署入口。先用同份目标源码构建前端，构建失败停止后续操作：

```powershell
$port = 8876
$env:VITE_CHAT_UI_URL = "http://127.0.0.1:$port/"
Set-Location frontend
npm run build -- --base=/workbench/
if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
Set-Location ..
Remove-Item Env:VITE_CHAT_UI_URL
New-Item -ItemType Directory -Force .local/release | Out-Null
.\.venv\Scripts\python.exe -u -m phase1_agent.server --port 8876 --database .local/release/acceptance.sqlite --workbench-dist frontend/dist
```

工作台为 `http://127.0.0.1:8876/workbench/`，通用聊天为同源 `/`，酒馆为 `/tavern/`。保留 `--base=/workbench/` 与结尾 `/`；改后端端口须同步重建聊天链接。

部署验收应从同份源码构建 wheel，安装至独立、非 editable 环境，并核对实际导入来自 `site-packages`。不要用原开发机 `.local/resident/` 的忽略环境、启动脚本或旧 dist 作为新版发布产物。具体安装、构建身份及验证要求见 [后端说明](../backend/README.md) 与 [测试说明](TESTING.md)。

运行服务加载静态资产内存快照，重建 dist 或推送 Git 不会半更新服务，必须显式重启。当前不是 Windows 服务或自启动部署方案；本机历史常驻验证只代表当时快照。

## 停止与故障

先确认没有 active/in-flight 或待核实原请求。BAT 每次打印完整 metadata 路径和对应停止命令，使用原样打印的命令，仅停止该次启动的已核对进程，保留数据库。例如：

```powershell
powershell.exe -NoProfile -File .\scripts\stop-services.ps1 -MetadataPath "<本次打印的完整 services.json 路径>"
```

若所选源码在另一个 worktree，优先使用启动器打印的脚本绝对路径。手动模式在自己的后端/Vite 终端按 `Ctrl+C`。旧服务不能用另一次启动的 metadata 停止，不结束身份不明的端口进程。

| 现象 | 处理 |
| --- | --- |
| 端口占用 | 先确认旧服务身份和业务状态，再按其原停止方式释放；或指定空闲端口与独立库 |
| 前端可打开但代理失败 | 查后端控制台、直连 health、`OPENAGENT_API_PORT` 与聊天链接是否一致 |
| 缺 Python/Node 依赖 | 在实际选中的源码工作区安装，而非只装入口根目录 |
| 没有现行节点 | 核对 BAT 打印的提交、源码路径和包目录，避免连到旧安装快照 |
| 凭据不可用 | 查后端启动环境、协议、启用状态和引用；保存不证明供应商可调用 |
| Gemini 配置被拒绝 | 使用已登记型号及对应预算/强度；不能关闭思考的型号不要选“关闭” |
| 保存记录无法读取 | 导出原记录，再按需另建本机工作区；不清空存储或重发原请求 |
| 请求结果未知 | 核实同一原请求 body/key，不换 key 重试 |
| 有模型响应但无回复 | 检查节点接纳、上下文写回、聊天追加与公开展示；不要只看 HTTP 200 |
| 同源工作台 404/空白 | 查 `--workbench-dist`、`--base=/workbench/` 与带结尾 `/` 的入口 |

更细的失败分类见 [调试说明](DEBUGGING.md)。应用仅面向本机，不要暴露到公网。
