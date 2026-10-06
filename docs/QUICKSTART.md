# 快速启动

以下命令从仓库根目录开始，使用两个 PowerShell 终端。不要复用其他项目的数据库或虚拟环境。

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

普通新版 demo 不需要安装 `vendor/smolagents`。需要旧兼容测试时参见 [测试说明](TESTING.md)。

## 启动后端

终端一在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -u -m phase1_agent.server --port 8765 --database .local/demo.sqlite --mode offline
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

后端首页保留旧消费入口，不等于新图的独立聊天入口。新图应从工作台打开对应图的聊天地址，以保留确切图和会话身份。

## 配置真实模型

`--mode offline` 只控制旧固定流程的宿主模式。新版 `models.source` 按资源及后端凭据调用模型，这个标签不保证离线。

将 `DEEPSEEK_API_KEY` 注入启动后端的进程环境，不在代码、工作流或供应商表单中填写密钥正文。PowerShell 可在后端启动前用无回显交互读入：

```powershell
$secret = Read-Host "DEEPSEEK_API_KEY" -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
Remove-Variable secret
```

环境变量只影响之后启动的进程。已启动的后端需要停止并重新启动；不要为此重发未知结果的运行。结束演示并停止后端后，可在该终端执行 `Remove-Item Env:DEEPSEEK_API_KEY`。环境变量不是加密存储，不应将进程环境或完整请求头上传。

1. 在工作台使用“创建串行 Agent 示例”，保留空白新图作为另一个入口。
2. 在“模型供应商 · 当前资源”新增资源；示例默认名称为 DeepSeek，服务地址为 `https://api.deepseek.com`，凭据引用选择 `env:DEEPSEEK_API_KEY`，保持启用并保存。
3. 选择示例的模型源节点，选供应商资源，填写该供应商当前实际支持的模型 ID，应用模型配置。原开发验收使用过 `deepseek-flash`，不保证该 ID 在所有账号或未来仍可用。
4. 可先使用最大输出 `1024`、`temperature=0`；字段当前限制分别为 `1..8192`、`0..2`，模型还须接受这些参数。当前编辑器固定 `thinking=disabled`、`stream=false`。
5. 检查 A/B 提示词并保存工作流。创建草稿和保存不自动派发模型；配置不完整的草稿可以保存，但运行准备会拒绝无效模型配置。
6. 从该图打开独立聊天，显式提交一条短输入，等待整图完成并查看 B 的公开回答。

默认 18 节点示例中，原输入分别进入 A/B，A 的结果只作为 B 本轮的附加材料。两者各自维护上下文，B 成功后再执行上下文写回及聊天记录追加。两个 Agent 应通过 `final_answer` 工具交付含 `text` 的答案；默认提示词已经说明完成格式。

## 停止与常见问题

在两个服务终端分别按 `Ctrl+C`，只停止本次服务，不删除数据库。重新启动并选同一会话可以读取完成历史，不表示恢复原进程中的活动 Agent 内核。

| 现象 | 检查 |
| --- | --- |
| 端口占用 | 不结束不明进程。前端可另选如 `5180`；后端换端口时必须同步 `frontend/vite.config.ts` 的代理目标 |
| 代理连接失败 | 先看后端终端及直连 health，再查前端代理；前端能打开不代表后端已启动 |
| 缺 Python 模块 | 确认使用本仓库 `.venv` 解释器并已 editable 安装 `./backend` |
| 没有新版节点 | 检查节点目录、启用包及实际后端源码；不要误连旧服务 |
| 凭据不可用 | 检查后端启动环境、资源启用状态与引用；保存资源成功不等于模型可调用 |
| 资源或控制结果待核实 | 核实同一原请求，保留 body/key；不要刷新后换 key 重发 |
| 有模型响应但没有聊天回复 | 检查节点接纳、上下文 merge、前端追加和 presentation，模型 HTTP 200 不等于整图完成 |

故障分类及恢复动作见 [调试说明](DEBUGGING.md)。demo 仅面向本机，勿暴露到公网。
