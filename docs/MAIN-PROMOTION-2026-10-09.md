# 测试版晋升与旧正式版归档

日期：2026-10-09。用户授权当前测试版替换旧正式源码，作为后续产品主干；旧版只归档供参考。晋升操作未推送、不升级应用版本、不自动启动或重启服务；随后按独立授权正常推送 `main`，远端同步情况见下节。

## 当前主干与远端

2026-10-09 已正常推送新 `main`，没有强推或重写历史。同步时本地 `main`、`develop`、`origin/main` 均为 `0c158e7`，其中产品实现提交为 `01809ff34b84f3ebba093c6b97d7390cdf9ace84`。后续文档或产品提交会推进 HEAD，当前身份应以 `git rev-parse main develop origin/main` 及远端查询核对，不把此处的同步起点当作永久 HEAD。

本轮只推送 `main`。旧版注解标签 `main-before-node-unification-2026-10-09` 仍仅在本地，源码 ZIP 和数据库快照位于被忽略的 `.local/archives/`，没有上传为远端发布产物。远端 `main` 的当前目录已是新版；旧提交仍存在于 Git 历史，正常推送不会抹除它们，也不会自动部署或替换已经运行的进程。

应用元数据仍为 `0.2.0`，未创建新版发布标签、Release、安装包发布或服务器部署。`v0.2.0` 固定的是 2026-10-07 的历史基线，不是当前 `main` 的别名。当前使用与开发规则见 [快速启动](QUICKSTART.md)、[使用手册](USER-GUIDE.md) 和 [开发说明](DEVELOPMENT.md)。

## 旧版归档

替换前本地 `main` 和 `develop` 的已提交身份相同：

`4e4f4e97656a9a88cb5aacd0cfffb2dbdf77ee24`

旧源码保留为注解标签 `main-before-node-unification-2026-10-09`，不移动 `v0.2.0` 或已有退役标签，也不新建长期历史开发分支。完整提交历史仍保留。

入口工作区的 `.local/archives/main-before-node-unification-2026-10-09/` 保存：

- `source.zip`：从上述标签导出的已跟踪源码，不包含依赖、凭据、浏览器存档或运行目录。
- `stable-workflow.sqlite`：正式入口默认 `.local/dev/workflow.sqlite` 的 SQLite 一致性快照；源以只读 URI 打开，使用 backup API，快照 `PRAGMA quick_check` 为 `ok`。

快照不复制开发库、不执行业务迁移、不打开应用存储服务。此前安装版 `.local/resident/` 保留为历史证据，不作为当前正式源码入口。归档库仅用于参考或另行授权的恢复，不由当前 BAT 自动打开。

源码 ZIP 核对为 539 个文件，包含旧上下文实现、不含运行目录。归档数据库与原稳定库的 SQL 逻辑内容一致；SQLite backup 生成的文件布局不要求与原库逐字节相同。归档 SHA256：

| 文件 | SHA256 |
| --- | --- |
| `source.zip` | `038c688cff3d85ec56ad9b1426c37bd93e12ed4b3bfd16a9f5a8aa65c2005139` |
| `stable-workflow.sqlite` | `ae97ce21278b1594bd0e7e943e7282c49060fb9f2c21bb309b0b424759afd38f` |

## 新主干

当前测试版的产品源码、测试和文档在 `develop` 提交，再由干净的 `.local/stable-main` 执行 `git merge --ff-only develop`。不覆盖 Git 历史，不把未提交修改丢弃或从旧 `main` 复制回测试版。

新主干包含现行节点路线、二级菜单、DeepSeek/Gemini 原生工具路线与 Gemini 思考签名闭环，以及 SQLite v15 / 浏览器存档的精确旧图清理。详情见 [节点目录](NODE-DIRECTORY-2026-10-09.md) 和 [Gemini 验收](GEMINI-ACCEPTANCE-2026-10-09.md)。

`main` 是当前产品主干；`develop` 是基于同一主干继续开发和隔离验收的工作区，不维持另一套产品实现。后续只从新基线继续，不恢复旧节点或历史版本选择器。晋升和此次远端同步完成时两个分支对齐，后续开发仍经对应验收再进入 `main`。

## 启动和数据

两个 BAT 不需要改写分支选择：

- 根 `start.bat`：新 `main` 源码，后端 `8765`、工作台 `5178`，默认稳定数据库 `.local/dev/workflow.sqlite`。
- 同目录 `start-dev.bat`：当前 `develop` 源码，后端 `8766`、工作台 `5179`，独立开发数据库 `.local/develop/workflow.sqlite`。

正式 BAT 下次正常启动时会打开稳定库并执行 SQLite v15 升级，按已有授权删除明确旧路线及其关联测试内容，无关联现行数据保留；稳定浏览器存档在页面重读时执行对应清理。晋升本身不混合两个数据库，不复制 localStorage，不执行上述清理，不调用真实模型，不重放工具。

晋升时正在运行的开发验收服务没有自动停止或换版；这是当时的进程状态，不保证它现在仍运行。正式入口仍打开可见前后端控制台，不采用隐藏后台、自启动或 Windows 服务注册。后续启动或停止以启动器 metadata 和实际进程身份核对为准。

## 验收边界

已有产品验收为前端 67 文件 / 928 项通过、最后后端补充回归 191 项通过、离线浏览器 9 场景 / 11 截图通过。后端全量第二轮为 3008 通过 / 50 既有失败 / 9 跳过；失败身份均存在于旧稳定基线，不声明全量绿色。最后修改与全量的范围差异见节点目录记录。

## 晋升复核

产品与节点整理提交为 `01809ff34b84f3ebba093c6b97d7390cdf9ace84`，已从 `develop` 快进到 `main`。本文验收补记只改变文档，再次提交并快进对齐两个分支；最终 HEAD 用 `git rev-parse main develop` 查询，不把产品提交当作文档收口后 HEAD。

| 复核 | 结果 |
| --- | --- |
| 新 `main` 前端全量 | 67 文件、928 项通过 |
| 新 `main` 类型检查与生产构建 | 通过；JS 516.01 kB / gzip 159.91 kB，仍有大于 500 kB 提示 |
| 新 `main` 后端定向回归 | 13 文件、239 项通过；覆盖目录、退役、资源、提示词、模型、Gemini、原生上下文及结果接纳 |
| 根 `start.bat -CheckOnly -NoBrowser` | 通过；明确选中新 `main` 工作区及其解释器、默认 8765/5178 和原稳定库路径 |
| 根 `start-dev.bat -ResolveOnly` | 通过；同一产品提交、默认 8766/5179 和独立开发库 |
| 开发 BAT 完整预检 | 通过；指定 8877/5187 仅作不启动服务的检查，避免占用正在运行的默认开发端口 |
| Git 与归档 | 新主干与开发分支对齐、工作区干净、旧标签身份准确、源码 ZIP 与数据库快照核对通过 |

后端定向复核使用已有开发测试解释器，显式将 `PYTHONPATH` 和测试路径指向 `.local/stable-main`，临时库与缓存位于入口工作区的隔离 `.local` 路径；没有给稳定解释器追加测试依赖。

新增证据位于入口工作区 `.local/main-promotion-backend.log` / `.xml`、`.local/main-promotion-frontend.log`、`.local/main-promotion-build.log`、`.local/main-promotion-stable-preflight.log` 及两个开发入口日志。未重复后端全量或收费模型调用；既有 50 项失败不因晋升更名为通过。正在运行的开发服务没有通过本次晋升自动替换为正式服务。

## 文档订正续记

2026-10-09，用户授权三并发核对和订正文档，完成后本地提交并正常推送，不另行询问普通提交/推送授权。项目介绍、使用/启动手册、前后端说明、开发/调试/测试入口及规划状态已统一为新主干口径，新增文档索引和使用手册。历史页面加上快照边界，27 条已删除源码的旧链接改为核实过的历史提交固定链接，原统计与固定基线不倒改。

代码相关修改仅为 Python 包描述和包模块 docstring，描述现行 DeepSeek/Gemini 工作流能力；应用版本、依赖、启动脚本、协议 schema 和可执行行为没有变化，不重建或发布安装包。

本轮检查为 45 份 Markdown、512 条本地链接及 12 个章节锚点通过；节点目录和两个契约导出测试共 10 项通过。工程文档命令收集到 275 个后端测试目标、列举 11 个前端测试文件成功，收集不等于运行通过。`git diff --check`、变更范围及元数据不变核对通过；没有重跑前后端全量、启动服务、操作产品库或调用收费模型，50 项既有失败继续保留。

原始检查证据留在忽略的 `.local/docs-link-audit.json`、`.local/docs-historical-link-repairs.json`、`.local/docs-update-smoke.log` / `.xml`。本轮只正常同步 `main`，不改写历史、不连带推送归档标签或本机产物；推送后的完整提交身份以 `git ls-remote --heads origin main` 与本地 `git rev-parse main develop origin/main` 核对，不在本页写入自身提交哈希。
