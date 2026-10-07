# 快速启动

以下命令从仓库根目录开始。开发模式使用两个 PowerShell 终端；生产构建模式只需一个运行中的 Python 服务。不要复用其他项目的数据库或虚拟环境。

**本工作区已有 1.0 常驻占用 8765，直接使用 `http://127.0.0.1:8765/workbench/` 或下方“本机已建立的常驻入口”，不要再次启动下面的 8765 示例。**下面安装/启动步骤面向没有该常驻的新环境。要并行建立独立开发或验收环境，另选空闲后端端口，并同步 Vite 的 `/api` 代理、`VITE_CHAT_UI_URL` 与对应只读检查地址；不要让新 Vite 页面误连当前常驻库。只有确认自己常驻没有 active/in-flight 或待核实业务后，才能按停止流程释放其端口。

## 环境

- Python 推荐 3.12；包声明最低 3.10，已有主要验证使用 3.12。
- Node.js 推荐 24.x，带 npm。锁定的 Vite 要求 `^20.19.0 || >=22.12.0`，Vitest 要求 `^22.12.0 || ^24.0.0 || >=26.0.0`，不要使用 Node 18。
- 前后端安装需要下载依赖；模型在线运行另外需要有效账号、凭据与网络。

## 安装

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ./backend
Set-Location frontend
npm ci
Set-Location ..
New-Item -ItemType Directory -Force .local | Out-Null
```

无需激活虚拟环境。非 Windows 系统可用 `python3.12 -m venv .venv`，并将解释器路径换为 `.venv/bin/python`。

当前运行和测试均不需要安装 `vendor/smolagents`；其源码仅保留来源与许可。

## 启动后端

终端一在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -u -m phase1_agent.server --port 8765 --database .local/demo.sqlite
```

服务监听 `127.0.0.1`；`--database` 必填，父目录须存在。日志显示于当前终端，数据库保存工作流定义、会话、资源和运行记录。

终端二在仓库根目录执行：

```powershell
Set-Location frontend
npm run dev -- --port 5178 --strictPort
```

工作台地址为 `http://127.0.0.1:5178/`，前端 `/api` 请求代理到后端 `8765`。使用该地址保持同源访问，不直接从其他站点发起写请求。

在第三个终端可做只读检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/health
Invoke-RestMethod http://127.0.0.1:5178/api/graph/node-types/v2
```

新后端库默认启用独立包；显式空选择不会被补回默认包。浏览器只有在可以读取、且没有本机存档时才建立一个空普通图。旧固定/compat 测试内容，以及关联的当前定义、会话、对象、manifest、事实和回执整组删除，不保留旧入口；无关联当前数据、独立资源/配置及原请求保护保留。浏览器保存定义或 pending 中明确含旧内容的工作流行也退出，不重发请求。本地无关联记录不可读时应先核查原记录，不能按新安装覆盖。SQLite v14 的升级范围及真正存储故障时的事务回滚见 [实际移除记录](REPLACE-REMOVAL.md)，正常旧关联不再触发回滚保全。

缺已保存包时保留原选择并提供带诊断的冻结历史只读，新业务变更不走兼容 fallback。若缺包时还有未关闭活动链，本轮需补回原精确安装后重开、显式关闭，才能重配；不支持恢复丢失的活动内核。范围与限定证据见 [默认入口与初始化收口](REPLACE-DEFAULTS.md)。

后端首页没有身份时显示未绑定状态，不新建会话。普通图保存后，从工作台打开对应图的聊天地址，以保留确切定义和可选会话身份；有原请求待核实时先核实。旧 `?session=<UUID4>` 交接和旧会话 HTTP 接口已移除，不能从旧入口启动或修改。

## 生产构建与同源启动

工作台生产构建、GraphChat 及 `/api` 可以由同一个 Python 进程提供，不需要 Vite 开发服务器或反向代理。`npm run preview` 仅用于预览前端，不是完整部署入口。当前包版本为 `0.2.0`；2026-10-07 已完成本机独立安装和同源常驻切换，并通过 21/21 联合核对。此处的常驻是本机显式启动的后台进程，不是 Windows 服务、自启动或外部生产交付；Git 版本身份及范围见 [0.2.0 基线](BASELINE-0.2.0-2026-10-07.md)，不等于 1.0 正式版。

前后端必须来自同一份目标源码。先在目标后端端口确定后构建工作台，构建失败则停止后续步骤：

```powershell
$port = 8765
$env:VITE_CHAT_UI_URL = "http://127.0.0.1:$port/"
Set-Location frontend
npm run build -- --base=/workbench/
if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
Set-Location ..
Remove-Item Env:VITE_CHAT_UI_URL
```

`VITE_CHAT_UI_URL` 在构建时写入聊天链接，不是凭据。后端改端口必须重建；`--base=/workbench/` 必须保留，普通 `/` base 构建不能直接用于该入口。

用当前已安装的后端先做本地启动：

```powershell
.\.venv\Scripts\python.exe -u -m phase1_agent.server --port 8765 --database .local/demo.sqlite --workbench-dist frontend/dist
```

工作台地址为 `http://127.0.0.1:8765/workbench/`，聊天入口为 `http://127.0.0.1:8765/`，API 仍为 `/api`。从工作台保存普通图后打开对应聊天；不要把工作台查询参数当作聊天交接。所有地址均监听 loopback，写请求仍要求同源 `Origin`，不存在旧会话或任意文件 fallback。

部署验收使用独立、非 editable 后端安装，避免意外导入开发源码。从与前端相同的目标源码构建 wheel，再建立独立环境：

```powershell
New-Item -ItemType Directory -Force .local/release/wheels | Out-Null
.\.venv\Scripts\python.exe -m pip wheel --no-deps ./backend --wheel-dir .local/release/wheels
if ($LASTEXITCODE -ne 0) { throw "Backend wheel build failed" }
py -3.12 -m venv .local/release/runtime
.\.local\release\runtime\Scripts\python.exe -m pip install .local/release/wheels/phase1_agent-0.2.0-py3-none-any.whl
if ($LASTEXITCODE -ne 0) { throw "Backend install failed" }
.\.local\release\runtime\Scripts\python.exe -I -c "import phase1_agent.server as server; print(server.__file__)"
.\.local\release\runtime\Scripts\python.exe -I -u -m phase1_agent.server --port 8765 --database .local/release/acceptance.sqlite --workbench-dist frontend/dist
```

应确认打印出的后端文件在独立环境的 `site-packages` 内，而非 `backend/src`。标准 wheel 构建使用隔离构建依赖；没有安装 `setuptools` 的开发环境不使用 `--no-build-isolation`。wheel 文件名随项目版本改变；已有验收目录不可当作全新安装证据，不要在未知活动库上覆盖执行。第一次安装依赖可能联网，但只启动页面和只读检查不会调用模型。

只读联合检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/health
Invoke-RestMethod http://127.0.0.1:8765/api/graph/node-types/v2
(Invoke-WebRequest http://127.0.0.1:8765/workbench/).StatusCode
(Invoke-WebRequest http://127.0.0.1:8765/static/index.html).StatusCode
Get-FileHash .local/release/wheels/phase1_agent-0.2.0-py3-none-any.whl -Algorithm SHA256
Get-FileHash frontend/dist/index.html -Algorithm SHA256
```

验收还须记录目标提交及工作区差异、wheel 与 dist 全部文件摘要，并完成实际浏览器流程；单独 `health=ok` 或首页 HTTP 200 不是完整通过证据。服务在启动时读取 index 和允许的 assets 为内存快照，运行中重建 dist 不会半更新，需要显式重启才换版本；只提供 `index.html` 与允许的 JS/CSS/图像/字体，不暴露 source map、数据库或目录。工作台仅放行画布必要的样式属性，脚本仍限同源，GraphChat 与 API 的原安全策略不变。

安装为常驻服务、替换已有服务/正式库、注入真实凭据和收费调用必须在该次操作获得明确授权。没有授权时，上述验证应使用本轮独立环境和临时新库，验证结束只停止自己的进程；跨进程活动内核恢复不是当前保证。

### 本机已建立的常驻入口

本工作区的 `.local/resident/` 已准备非 editable 的 `0.2.0` 独立环境、稳定 `dist` 和独立数据库。它是忽略的本机运行目录，不随仓库克隆分发；新机器先按上面的安装与同源启动流程建立自己的环境，不照搬这些本机命令或私有配置。

从仓库根目录执行：

```powershell
.\.local\resident\launch.ps1 -CheckOnly
.\.local\resident\launch.ps1
```

前者仅核对当前启动条件，后者显式启动或确认本机对应进程。工作台为 `http://127.0.0.1:8765/workbench/`，GraphChat 为 `http://127.0.0.1:8765/`。运行解释器在 `.local/resident/runtime/`，静态产物在 `.local/resident/dist/`，数据库在 `.local/resident/data/workflow.sqlite`，运行状态与日志在 `.local/resident/run/`。不上传私有凭据来源、环境、数据库或日志。

需要停止或更换安装/静态产物时，先由操作者确认没有 active/in-flight 业务，也没有结果未知或待核实的原请求；仅检查数据库空闲不能替代此确认。然后执行：

```powershell
.\.local\resident\stop.ps1 -ConfirmIdle
```

`-ConfirmIdle` 是操作者对上述前提的明确确认，不是脚本自动证明空闲。停止后才替换文件，再使用 `launch.ps1` 显式启动；启动器遇到不明端口占用或进程身份变化会拒绝接管。当前没有 Windows 服务注册、自启动或活动内核跨进程恢复。

## 配置真实模型

服务不再接受旧 `--mode` 参数。`models.source` 按资源及后端凭据调用模型，真实运行可能联网并产生费用。

将 `DEEPSEEK_API_KEY` 注入启动后端的进程环境，不在代码、工作流或供应商表单中填写密钥正文。PowerShell 可在后端启动前用无回显交互读入：

```powershell
$secret = Read-Host "DEEPSEEK_API_KEY" -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
Remove-Variable secret
```

环境变量只影响之后启动的进程。已启动的后端须确认没有 active/in-flight 或待核实业务后再停止并重新启动；不要为此重发未知结果的运行。本机常驻有自己的私有凭据来源，上面的交互命令仅用于由当前终端启动的服务，不会修改已运行常驻进程的环境。结束演示并停止后端后，可在该终端执行 `Remove-Item Env:DEEPSEEK_API_KEY`。环境变量不是加密存储，不应将进程环境或完整请求头上传。

1. 在工作台使用“创建串行 Agent 示例”，保留空白新图作为另一个入口。
2. 点击左侧“供应商”，在“模型供应商 · 当前资源”新增资源；示例默认名称为 DeepSeek，服务地址为 `https://api.deepseek.com`，凭据引用选择 `env:DEEPSEEK_API_KEY`，保持启用并保存。
3. 选择示例的模型源节点，选供应商资源，填写该供应商当前实际支持的模型 ID，应用模型配置。原开发验收使用过 `deepseek-flash`，不保证该 ID 在所有账号或未来仍可用。
4. 可先使用最大输出 `1024`、`temperature=0`；字段当前限制分别为 `1..8192`、`0..2`，模型还须接受这些参数。当前编辑器固定 `thinking=disabled`、`stream=false`。
5. 检查 A/B 提示词并保存工作流。创建草稿和保存不自动派发模型；配置不完整的草稿可以保存，但运行准备会拒绝无效模型配置。
6. 从该图打开独立聊天，显式提交一条短输入，等待整图完成并查看 B 的公开回答。

默认 18 节点示例中，原输入分别进入 A/B，A 的结果只作为 B 本轮的附加材料。两者各自维护上下文，B 成功后再执行上下文写回及聊天记录追加。两个 Agent 应通过 `final_answer` 工具交付含 `text` 的答案；默认提示词已经说明完成格式。

## 配置全局提示词

普通图使用独立 `prompts.global-reference@1` 时，可从左侧“内容”的“提示词 · 当前资源”新增或更新提示词。此时主画布保留，可以同时配置引用节点；旧内容库和主编辑器已退出。

1. 新增资源，按需新增条目、填写正文和呈现字段，保存当前资源。空组可保存，但不保证空材料能独立装配为有效提示词。
2. 显式选择“全局提示词引用”节点的已有资源并应用，保存工作流；“全局提示词解析”节点解析它输出的引用。资源选择保留范围、类型和 UUID，缺失引用不会自动替换。
3. 更新或停用资源会产生新 current 序号；节点配置仍保留同一身份。显示标签取正文首行，空组显示 UUID，不是新增持久名称。
4. 结果未知时使用“核实原提示词请求”，保留原 key/body；不要改 key 重发。当前入口不提供持久资源删除、独立资源导入或旧内容自动迁移。

默认串行示例的 A/B 提示词仍是图内条目，不因新增全局管理入口自动改为资源引用。创建、配置和保存不会自动运行模型。

## 停止与常见问题

先确认没有 active/in-flight 或结果待核实的业务。开发模式在两个服务终端分别按 `Ctrl+C`；前台生产模式只在本次 Python 服务终端按 `Ctrl+C`；本机常驻使用上面的 `stop.ps1 -ConfirmIdle`。只停止自己的服务，不删除数据库。重新启动并选同一会话可以读取完成历史，不表示恢复原进程中的活动 Agent 内核。

| 现象 | 检查 |
| --- | --- |
| 端口占用 | 不结束不明进程。开发前端可另选如 `5180`；开发后端换端口时同步 `frontend/vite.config.ts` 代理目标。生产模式改 `--port` 后须以相同 `VITE_CHAT_UI_URL` 重建工作台 |
| 代理连接失败 | 先看后端终端及直连 health，再查前端代理；前端能打开不代表后端已启动 |
| 缺 Python 模块 | 确认使用本仓库 `.venv` 解释器并已 editable 安装 `./backend` |
| 没有新版节点 | 检查节点目录、启用包及实际后端源码；不要误连旧服务 |
| 凭据不可用 | 检查后端启动环境、资源启用状态与引用；保存资源成功不等于模型可调用 |
| 资源或控制结果待核实 | 核实同一原请求，保留 body/key；不要刷新后换 key 重发 |
| 有模型响应但没有聊天回复 | 检查节点接纳、上下文 merge、前端追加和 presentation，模型 HTTP 200 不等于整图完成 |
| 生产工作台空白或资源 404 | 检查 `--workbench-dist` 指向正确 dist，构建有 `--base=/workbench/`，访问带结尾 `/` 的 `/workbench/`；不要用目录或 SPA fallback 掩盖错误 |

故障分类及恢复动作见 [调试说明](DEBUGGING.md)。demo 仅面向本机，勿暴露到公网。
