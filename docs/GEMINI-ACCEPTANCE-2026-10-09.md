# Gemini 工具与思考实现记录

日期：2026-10-09。以下实现与验收章节保留验收时状态；随后提交与主干晋升见最后的 Git 收口节。没有正式发布或收费在线验收。
开发起点及当时稳定 `main` 均为 `fc2253902e7aa671472ccb03e8f282794fc36906`。

## 当前使用状态

Gemini 工具与思考已进入当前 `main`，2026-10-09 正常推送远端，主干同步起点为 `0c158e7`。后续节点统一只保留 Gemini `models.source@4`、`models.chat@4`、`agents.execute@8`，配合 `context.output/assembly/merge@4` 和提示词材料 `@2`；DeepSeek 是并列的现行协议，不是待退役兼容路线。

本页下面的版本表和四条 Agent 路线是 Gemini 接入当时的验收快照，不再作为新图选择清单。旧非原生路线及关联图按精确身份删除，不自动迁移；当前选择、删除边界和新主干复核见 [节点目录](NODE-DIRECTORY-2026-10-09.md) 与 [主干晋升记录](MAIN-PROMOTION-2026-10-09.md)。签名闭环、可见摘要、受控工具与 `final_answer` 继续保留，真实供应商验证和活动运行跨进程恢复仍未完成。

## 实现范围

- 原生非流式 `generateContent`，使用现有 httpx 与受控凭据 broker，不使用自动执行工具或隐式重试的 SDK。
- 回答 `content`、可见 `thinking_summary`、不透明 `provider_metadata` 分离。摘要不等于完整内部思维链，不从签名推导摘要。
- 原模型 Content/parts 顺序、签名、原生调用 ID 经响应、Agent 消息、运行事实、上下文、SQLite 冷重读进入下一次请求；内核 ID 只改变显式调用绑定。
- 多个同名调用按 ID 和 part 位置匹配。工具定义转换为 `parametersJsonSchema`，不支持的 schema 关键字派发前拒绝，不静默移除约束。
- Gemini 与 DeepSeek 复用现有工具执行器和受控 `final_answer`；没有另建无工具酒馆路径，也没有新增并行工具执行器。
- 请求事实、digest、冷输入估算和实际 HTTP wire 共用 `prepare_request`。HTTP 错误、超时及派发未知不触发自动模型重试或工具重放。
- 工作台、GraphChat 和酒馆独立显示摘要；普通 JSON 展示递归移除协议封套及签名。酒馆只读取精确 producer 对应的公开摘要通道，不开放私有执行事实。

## 实现时版本与配置

本节保留接入时注册范围；其中旧节点已在后续统一中移除，不能按本表创建当前工作流。模型能力表是代码校验白名单，不是供应商当前可用性或在线验收证明。

| 对象 | Gemini 新版本 | 原有版本 |
| --- | --- | --- |
| `workflow.chat-provider` 资源 | schema 2，`protocol: "gemini"` | schema 1 保持原 Chat 声明 |
| `MODEL_BINDING` | 3 普通、4 带容量 | 1/2 保持原定义 |
| `MODEL_RESULT` | 2，含摘要与协议元数据 | 1 保持原定义 |
| `models.source` | 3 普通、4 带容量 | 1/2 |
| `models.chat` | 2 | 1 |
| `agents.execute` | 5/6/7/8 对应普通、绑定、摘要、原生上下文 | 1/2/3/4 |
| `agents.delta` / `CONTEXT_UNIT` | 2 / 2 | 1 / 1 |
| 原始 `agent_message` | schema 5 承载新通道 | 无新通道的旧消息语义保留 |

不能只把旧模型源的模型名改为 Gemini。需选择对应新节点版本，并引用 schema 2 的 Gemini 资源；协议不匹配时明确拒绝。旧供应商资源不原地改为另一协议。

服务地址默认 `https://generativelanguage.googleapis.com/v1beta`，凭据仅接受 `env:GEMINI_API_KEY`。真实 key 由启动后端的环境提供，不进入配置、历史或请求事实。

代码能力白名单及验证规则如下，不代表每个账号都可在线使用这些型号：

| 模型 | 思考参数 |
| --- | --- |
| `gemini-2.5-pro` | budget 128..32768 或 -1，不可关闭 |
| `gemini-2.5-flash` | budget 0..24576 或 -1 |
| `gemini-2.5-flash-lite` | budget 0、512..24576 或 -1 |
| `gemini-3-flash-preview` | level minimal/low/medium/high |
| `gemini-3.1-pro-preview` | level low/medium/high |

```json
{"model":"gemini-3-flash-preview","thinking":{"mode":"level","level":"medium","include_summary":true},"stream":false,"max_tokens":4096}
```

Budget 格式为 `{"mode":"budget","budget":1024,"include_summary":true}`；可关闭的型号仍使用原 `"thinking":"disabled"`。未知模型、不匹配的模式或范围派发前拒绝，不猜能力。沿用项目 `max_tokens <= 8192` 限制；Gemini 的 `maxOutputTokens` 同时约束思考和回答，不自动提高输出预留或改小用户预算。

## 离线验收证据

各组存在重叠，不累加为一个“全仓通过数”。本地证据位于 `.local/`，不纳入产品数据库或源码提交。

| 验收 | 最终记录 |
| --- | --- |
| 前端全量 | 64 文件、893 项通过；`.local/gemini-acceptance/frontend-final.json` |
| 类型与生产构建 | `npm run build` 通过 |
| 四条真实 Graph/SQLite 路线 | 4 项通过；`.local/gemini-acceptance/joint-final.xml` |
| Gemini 传输专项 | 43 项通过；`.local/gemini-prep-tests/transport-last.xml` |
| 传输/旧模型等较广回归 | 260 项通过；`.local/gemini-prep-tests/transport-complete.xml`，与专项重叠 |
| 历史与当前上下文/事实 | 275 项通过；`.local/gemini-history-tests/current-context-facts.xml` |
| 历史最终定向与暂停 | 107、34 项分别通过；`.local/gemini-history-tests/final-targeted.xml`、`latest-contracts-pause.xml` |
| 根组合回归首轮 | 344 通过、2 环境跳过、1 旧暂停断言失败；`.local/gemini-acceptance/backend-final.xml` |
| 暂停语义修正补测 | 82 项通过，含上述原失败及相邻 Agent/历史/恢复；`.local/gemini-history-tests/package-pause-semantics.xml` |
| 酒馆客户端及两项环境跳过补测 | 10 项通过；`.local/gemini-acceptance/shell-final.xml` |
| 分支与启动预检 | 24 通过、8 实际启动场景未选；`.local/gemini-acceptance/launcher-final.xml` |
| 实际开发 BAT 与页面 | `start-dev.bat` 正常可见启动；5 张截图、0 页面/控制台/资产错误、0 执行命令；`.local/gemini-acceptance/browser-final/` |

联合 fixture 连续两个工具轮次后使用既有 `final_answer`，第一轮含两个同名调用。冷关闭 SQLite 后新服务重读，再启动下一回合；每个原 Content 与请求 wire 逐项一致，实际 HTTP 与持久化请求事实一致。所有签名均明确标注为合成测试材料，仅进入 mock transport，没有冒充真实有效签名。

首轮暂停用例仍断言“暂停前已接纳模型消息”，与新增的 `before_accept` 暂停边界不符。只修正这一测试，新增保留响应与接纳后材料完全一致、零重复模型请求、唯一工具派发/结算的断言；未为通过测试回退保留响应实现。组合首轮 XML 保留原失败，补测独立记录，不覆盖或隐去失败证据。

页面验收覆盖 1440/1024 工作台配置、供应商默认值、配置刷新重读，以及 1440/390 酒馆摘要折叠与历史重开。浏览器使用独立 context，未覆盖用户浏览器存档；后端使用 `.local/gemini-acceptance/joint-temp4/test_gemini_agent_tools_contex3/native.sqlite`，未打开用户原数据库。

已导出 `backend/schemas/contracts-v2.schema.json`；旧 provider1/binding1/binding2/result1 声明 digest 与稳定基线完全一致。第三方复制清单 13 项目标 SHA256 核对通过；修改说明追加到 SillyTavern 来源记录。

## 验收边界

- 同进程暂停保留待接纳模型响应和工具队列；接纳前暂停时响应不进入已接受消息，恢复后接纳并继续，不再次派发。没有新增活动运行的跨进程恢复或自动工具重放。
- 模型结果及终态产物的接纳重试只重新提交保留结果。任意中途 `message_accepted` 事实存储失败仍按既有规则终止，没有通用中途事实重试机制。
- 正则保护协议材料，精简及历史摘要若覆盖签名记录则保守拒绝，不重新生成签名，不填 validator 绕过值。
- Gemini 3 首个 function-call part 缺必需签名时报告模型、来源和 part 位置；后续并行 call 不强求各自都有签名。本地校验不能证明签名密码学有效。
- 没有收费供应商调用；真实账号可用性、签名接纳与费用上限仍需单独授权后在线验收。
- `test_execution_facts.py` 的旧 `save_bundle/get_record` API 测试债务在稳定源码重现为 48 失败、28 通过，证据 `.local/gemini-acceptance/stable-legacy.xml`；本次不据此宣称后端全量绿色，也不恢复退役 API。

## 启动与稳定线

根 `start.bat` 解析唯一干净的稳定 `main` 工作区，默认 8765/5178；验收阶段该工作区尚未包含 Gemini，后续主干晋升见下节。开发入口是 `start-dev.bat`，默认后端 8766、工作台 5179；不使用隐藏启动、Windows 服务、后台注册或自启动。

本次页面验收的可见启动记录为 `.local/service-starts/20261009-024539-3b5aa824/services.json`。开发服务使用隔离验收库；它不是稳定服务或用户原记录。终止时只按这份 metadata 停止本次拥有的进程，不删除数据库。验收阶段没有执行提交或主干晋升；用户随后另行授权合入 `main`。

## Git 收口

2026-10-09，用户明确要求将开发完成的内容合并入 `main`。全部产品修改按两组在 `develop` 提交，再从干净的 `.local/stable-main` 执行 `git merge --ff-only develop`；主干保持线性，无临时功能分支或额外合并提交：

| 提交 | 范围 |
| --- | --- |
| `07fa343` | 稳定 BAT 固定 main、开发启动隔离、启动测试与快速启动说明 |
| `cca89a8b69a9d88290c42b06e9c687837a250c06` | Gemini 原生工具与思考、签名闭环、前端展示、schema、测试与验收材料 |

本节文档收口也在 `develop` 提交并快进同步 `main`，最终分支身份以 Git log 为准。`.local` 的数据库、截图、JUnit、服务 metadata、依赖及构建输出均未纳入提交。

合并前定向回归 87 项通过，证据 `.local/gemini-acceptance/merge-preflight.xml`。合并后对稳定工作区 `main` 源码重跑相同 87 项通过，证据 `.local/gemini-acceptance/main-merge.xml`；稳定 venv 只安装运行依赖，没有 pytest，因此使用既有开发测试解释器并明确指向稳定源码，未给稳定环境追加测试依赖。

合并后的根 `start.bat -CheckOnly -NoBrowser` 通过：解析稳定 `main` 及其独立解释器，默认端口 8765/5178，默认数据库仍为根工作区 `.local/dev/workflow.sqlite`；预检未打开该数据库或启动服务。稳定工作区的前端类型检查与生产构建通过。

未推送、未改版本或标签、未调用收费 API、未启动/停止/重启服务、未迁移用户数据库。正在运行的开发验收服务不因本次 Git 快进自动换版；下次从根目录运行 `start.bat` 才使用已晋升的主干代码。真实供应商、跨进程活动恢复及历史测试债务等边界继续保留。

以上“未推送”和进程状态只描述 Gemini 合并阶段。随后节点统一与主干替换已完成，2026-10-09 的新版 `main` 已正常推送；旧版源码标签与本地快照仅作参考，应用版本未升级，也未因推送执行部署。当前全量仍有 50 项已核对的基线失败，不能把早期定向通过或后续晋升写成全量绿色。
