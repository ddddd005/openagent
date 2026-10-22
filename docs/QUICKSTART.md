# 快速启动

本页提供环境安装与本地运行参考，不证明本机当前已经安装、启动或部署。由代码助手代为安装依赖、启停服务、访问业务库、调用真实模型或执行 Git 发布时，仍须按根目录 [AGENTS.md](../AGENTS.md) 确认相应授权。

日常开发使用 `develop` 和 `start-dev.bat`。`start.bat` 只接受唯一、干净且匹配本地 `main` 的工作区，不回退到 `develop`；使用前核对实际工作区，不绕过门禁。建立新的稳定工作区及安装其依赖属于独立准备，不是每次启动的默认步骤。

历史决定、交接与验收回执按[历史索引](开发新基线-2026-10-16/旧债清理/02-历史指引精简与阅读索引-2026-10-17.md)按需查阅；[60号回执](归档/2026-10-17/中期检查2/60-旧稳定库退役与正常开发环境交接回执-2026-10-15.md)记录退役 worktree 和凭据库的历史绝对路径。保留退役 worktree 原路径及 Git worktree 锁，不移动、不解锁、不用于新开发；凭据保护与原路径绑定，不移动凭据库。60号中的空库、停服等仅是当时事实，不代表当前状态。

测试旧债的结案口径见[09号实施总账](开发新基线-2026-10-16/旧债清理/09-测试旧债实施与验证总账-2026-10-17.md)；历史结案数字不是当前全量测试结果。

以下命令从仓库根目录执行；界面操作见 [使用手册](USER-GUIDE.md)，开发规则见 [开发说明](DEVELOPMENT.md)。

## 环境与安装

- Python 推荐 3.12；包声明最低 3.10，主要验证使用 3.12。
- Node.js 推荐 24.x，带 npm。锁定的 Vite 要求 `^20.19.0 || >=22.12.0`，Vitest 要求 `^22.12.0 || ^24.0.0 || >=26.0.0`；不要使用 Node 18。
- 安装依赖需要网络。真实模型运行另需有效凭据、账号和供应商网络。
- `vendor/smolagents` 仅为来源归档，不需要安装。

以下是新建本地开发环境时的参考步骤，不要求每次启动时重复安装。仅在已获准准备的目标源码工作区缺少依赖时执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ./backend
Set-Location frontend
npm ci
Set-Location ..
New-Item -ItemType Directory -Force .local | Out-Null
```

无需激活虚拟环境。非 Windows 系统可用 `python3.12 -m venv .venv` 和 `.venv/bin/python`，采用下文手动启动；BAT 与 PowerShell 启动脚本面向 Windows。

虚拟环境和 `node_modules` 不随 Git 推送，也不会因创建 worktree 自动复制；启动器不会自动安装依赖。不要复用其他项目的环境或数据库。为 `start.bat` 另行准备 `main` worktree 需要单独授权，本页不提供默认创建步骤。

## Windows BAT

| 入口 | 选择源码 | 工作台 | 后端基址 | 默认数据库 |
| --- | --- | --- | --- | --- |
| `start.bat` | 唯一、干净且匹配本地 `main` 的工作区 | `http://127.0.0.1:5178/` | `http://127.0.0.1:8765/` | 入口根目录 `.local/dev/workflow.sqlite` |
| `start-dev.bat` | 当前根工作区的 `develop` | `http://127.0.0.1:5179/` | `http://127.0.0.1:8766/` | 入口根目录 `.local/develop/workflow.sqlite` |

施工树不再提供旧GraphChat/Tavern业务入口。准确节点页从已保存`frontend.node@2`的打开按钮进入；裸后端根页及`/tavern/`返回410，不作为就绪探测或业务入口。

日常开发先检查再启动：

```powershell
.\start-dev.bat -CheckOnly -NoBrowser
.\start-dev.bat
```

正式入口仅在已准备的 `main` worktree 唯一、干净且匹配本地 `main` 时使用：

```powershell
.\start.bat -CheckOnly -NoBrowser
.\start.bat
```

`-ResolveOnly` 仅输出选中的分支、提交、路径与端口，检查依赖用 `-CheckOnly`。正式入口遇到缺失、有修改或不匹配的 `main` 会拒绝，不回退 `develop`；开发入口拒绝正式默认端口 `8765/5178` 和正式默认数据库。

BAT 使用可见前后端控制台显示日志，不隐藏后台启动。它不切分支、不拉取 Git、不接管或终止已有进程；端口占用会拒绝。可显式指定另一组空闲端口、独立库和解释器：

```powershell
.\start.bat -BackendPort 8876 -FrontendPort 5189 -DatabasePath .local/acceptance/stable.sqlite -NoBrowser
```

相对数据库路径以入口根目录解析，不以选中的 `main` 工作区解析。解释器优先选目标工作区 `.venv/Scripts/python.exe`，否则使用 PATH 中的 Python；可用 `-PythonPath` 显式指定。内部 `scripts/start-services.ps1` 不提供分支隔离，日常使用 BAT。

启动器同步`/api`代理，检查当前源码、保留的Lorebook包/节点、新版前端节点及前后端目录一致，不输出旧Tavern链接。旧`-Mode offline`已退出；只读浏览不调用模型，显式运行真实模型节点可能收费。

如果已有数据库的精确包选择缺少当前酒馆包，启动器拒绝本次新进程，保留原选择。请使用独立新库验收，或在确认业务状态后显式管理该库启用包；它不会自动换包锁以掩盖错误。

## 数据升级

施工树存储版本为20、图文档schema3、检查点schema4，分别校验。旧schema3检查点库未迁移，不能直接当成当前验收库；使用独立新库。不自动删除旧图、重绑会话、升级节点或导回旧数据。Key及原请求证据不随工作流清理删除。

旧工作台恢复、聊天consumer及预设入口已经退出，存量原请求只核实，不导入新节点或重发。浏览器namespace未因源码退役自动清空；数据删除必须另行核对授权路径及身份。

新后端库默认启用现行独立包；显式空包选择不会被默认补回。缺少已保存的精确包时保留原选择和可读冻结历史，不恢复旧执行。开发版允许直接关闭所属活动链，不要求为了目标关闭/归档先重新安装节点；实际运行、继续及默认数据初始化仍需要保存包声明。丢失的活动内核不能跨进程恢复。

## 保存 API Key

开发版只保留页面填写 Key 这一条凭证路线，不再读取环境变量密钥，也没有“后端凭据引用”选择器。打开左侧“供应商”，新增供应商进入草稿，填写名称、协议、URL 和 API Key，再点击一次“保存供应商”；此前不会创建生效配置或保存 Key。输入 Key 后可以继续编辑其他字段。

独立私有库使用 Windows 当前用户及绝对路径绑定保护，通用草稿、资源和工作流不保存密钥正文；宿主不支持安全保护时拒绝，不回退环境变量或明文。不要移动凭据库；历史位置见[60号回执](归档/2026-10-17/中期检查2/60-旧稳定库退役与正常开发环境交接回执-2026-10-15.md)。既有供应商也使用同一个“保存供应商”按钮：同时更改 URL 和 Key 会一起提交；Key 留空时保留已保存 Key。非秘密草稿可冷重读，尚未提交的 Key 不能在刷新或关闭后恢复。

Key 结果未知只能核实原请求或先归档再解除锁定，不重发。正式和开发数据库不自动合并，不搬运测试配置。配置限制见 [使用手册](USER-GUIDE.md)，本机保存成功不等于上游鉴权通过。

## 首次运行

1. 打开工作台。首次可读且没有本机存档时建立空普通图。
2. 在“供应商”新增供应商草稿，选择 DeepSeek 的 Chat Completions 协议，手填服务地址和 API Key，点击“保存供应商”提交整份配置。
3. 在“工作流”新建普通图，手动添加输入、材料、模型/Agent及需要的上下文/变量读写节点；不使用旧预设builder。
4. 保存全局命名模型配置，手填型号、容量和四参数，再由模型来源引用；下游按该配置的协议调用。
5. 检查提示词、准确数据项授权及真实依赖，保存正式配置；保存不要求调用模型。
6. 可用工作台声明输入显式启动；节点前端需添加`frontend.node@2`，绑定可编辑输入，保存后从节点打开并发送。开窗本身不建立会话或运行。新会话初值可使用[会话预加载数据](SESSION_PRELOADS.md)一次性初始化；采用变量注册节点时，仍需在稳定会话中主动注册。两种路径不能同时负责同一个对象键，见[使用手册](USER-GUIDE.md#5-变量注册赋值与引用)。
7. 查看整图状态、节点“状态与结果”和展示输出。模型 HTTP 200 不等于整图完成。

“本次输入”直接填写 TEXT 字段，复杂或未声明输入保留在高级 JSON。节点正文在右侧配置上方，UUID 和契约收进技术详情；新增节点可用画布工具栏加号和搜索。结果默认正文与可展开摘要，历史操作分别确认。

上下文通过节点显式加载和写回，Agent不持有或定位持久项。runtime判断全轮完成，输出节点不提前终止流程。历史验收范围不代表当前部署或全量验收，按[历史索引](开发新基线-2026-10-16/旧债清理/02-历史指引精简与阅读索引-2026-10-17.md)查对应回执。

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
npm run dev -- --port 5179 --strictPort
```

工作台为 `http://127.0.0.1:5179/`。`--database` 必填，手动方式父目录须先建立。服务仅监听 loopback；保持同源访问，不从其他站点发起写请求。

可在另一个终端只读检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8766/api/health
Invoke-RestMethod http://127.0.0.1:5179/api/graph/node-types/v2
```

## 同源构建（部署另行授权）

以下是独立构建与部署验收的操作参考，由代码助手执行时须另行确认范围。

工作台、准确节点页与`/api`可以由同一个Python进程提供，不需要Vite服务。`npm run preview`不是完整部署入口。先用同份目标源码构建，失败则停止后续操作：

```powershell
$port = 8876
Set-Location frontend
npm run build -- --base=/workbench/
if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
Set-Location ..
New-Item -ItemType Directory -Force .local/release | Out-Null
.\.venv\Scripts\python.exe -u -m phase1_agent.server --port 8876 --database .local/release/acceptance.sqlite --workbench-dist frontend/dist
```

工作台为`http://127.0.0.1:8876/workbench/`，节点页地址由准确节点entry返回。保留`--base=/workbench/`和结尾`/`，不配置全局聊天链接；后端健康检查访问`/api/health`。

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
