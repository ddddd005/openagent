# REPLACE-02 普通图供应商侧栏统一

本地验收日期：2026-10-07，Asia/Shanghai。承接 [首轮依赖盘点](REPLACE-AUDIT-2026-10-07.md)、[固定基线](BASELINE-2026-10-06.md)及 [1.0 规划](PLAN-1.0.md)，代码起点与当前 HEAD 均为 `00970f75fb52d741b694499a05f702600529fcdd`；以下结果针对该提交上的未提交工作区，不冒称已有新提交。

本轮沿用三个并发子代理：前端实施入口与路由用例，存储/测试代理补控制器与适配器联验，后端代理只读复核契约和边界；主代理集成、补锁边界断言并完成验证。

状态：**普通图供应商侧栏切片已实现并通过受影响验证；REPLACE-01/02 整体仍在进行，未完成旧架构退出或 1.0。**首轮盘点与原基线保留为各自时点的证据，不回写其历史结论。

## 1. 实际修改

| 文件 | 改动 |
| --- | --- |
| [App.vue](../frontend/src/App.vue) | 供应商主导航改用 `ProviderSidebar`，仅调整导入与挂载入口 |
| [ProviderSidebar.vue](../frontend/src/components/ProviderSidebar.vue) | 普通图经既有可信包宿主解析，挂载完整声明身份匹配的当前资源面板；旧图保留原 `ProviderPanel` |
| [ProviderSidebar.test.ts](../frontend/src/components/ProviderSidebar.test.ts) | 10 项 SSR/路由案例，覆盖声明、目录状态、新旧切换和资源锁边界 |
| [ProviderSidebarResources.test.ts](../frontend/src/components/ProviderSidebarResources.test.ts) | 6 项 SSR 与真实 controller/API adapter 的受控联验，覆盖同权威资源、CAS 和 unknown 原请求核实 |

没有修改后端、API、schema、资源 controller 或原面板；没有新增第二套资源权威或自动转换旧供应商。此前测试夹具解耦属于首轮切片，本页不将其再计为本轮修改。

## 2. 保留的边界

- 侧栏复用 `workflow.models.workbench-panel` 的完整声明与确切包版本，仍由现有宿主信任检查解析。缺包、错误版本、缺失/伪造/重复声明时明确不可用，不回退旧供应商入口。
- 当前资源面板与 `models.source` 复用现有 `useProviderResources` 单例；不因切换工作流或侧栏创建独立写入控制器。
- 全局资源编辑只受资源 busy/pending 限制，不因工作流修改锁而禁用；模型节点配置仍遵循工作流自身的编辑锁。
- 新旧 pending 请求保持各自路径、原正文和幂等键，不随切图被清空、转换或替换。unknown 刷新/重建后仍锁定写入，只有显式核实原请求才再次提交相同请求。
- 后续 CAS、Origin 或幂等冲突拒绝不能清除先前 unknown。缺包时不会为了恢复操作而改写包锁或落到旧 API。
- SSR 用例不执行 mounted 钩子或表单事件；受控联验的 fetch/持久化为替身，不当作真实后端或浏览器证据。产品仍是 SPA，本轮不新增 SSR 支持承诺。
- 独立侧栏没有创建旧模型 store，不等于整个 App 已独立；根 App 仍初始化旧 store，旧资源入口及固定工作流仍保留。

## 3. 定向自动验证

在 `frontend/` 执行：

```powershell
npm test -- `
  src/components/ProviderSidebar.test.ts `
  src/components/ProviderSidebarResources.test.ts `
  src/components/CurrentModelConfiguration.test.ts `
  src/components/ModelConfiguration.test.ts `
  src/application/workflowResources.test.ts `
  src/adapters/workflowResourcesApi.test.ts `
  src/stores/workflowDemoActions.test.ts `
  src/application/workflowFrontendPackage.test.ts `
  src/stores/workflowFrontendHost.test.ts
npm run build
```

最终结果：**9 个文件，50 passed，1.92 秒，退出码 0**。先前 49 项通过后新增一项锁边界用例，再复测相同范围得到 50 项；两批重叠，不累加为 99 项。首轮后端 73 项及串行四项复测另见首轮记录，不合并成一次全量验证。

`npm run build` 的 `vue-tsc --noEmit` 与 Vite 构建均通过，退出码 0。仍有已有的单 chunk 大于 500 kB 警告：本轮 JS 683.74 kB、gzip 210.10 kB；按 LATER-09 保留，不启动分包或性能专项。

收尾检查：`git diff --check` 通过；新增文件无尾随空白或冲突标记，Markdown 代码围栏闭合；README、基线、规划、待办、两份执行记录及快速启动共 7 个文件的 50 条本地文档链接无缺失。固定基线 SHA256 仍为 `0690253BEC23A2E377245E6F570B2814C3320CEA2C384A4DE927534678C6FCC4`。

## 4. 浏览器与真实本地服务验证

使用 Codex 内置浏览器和现有本地依赖，新建隔离数据库 `.local/replace-provider-20261007-7d3e9c/verification.sqlite`，没有读写原运行库。后端 loopback `8765`，前端 Vite `5178`；均为本轮隐藏启动的临时预览，不修改常驻部署。

后端启动参数为 `--mode offline`。该标签不是新版节点的网络禁用开关；本次资源使用 `https://example.invalid`、凭据引用未配置，模型名称为 `offline-no-call`，**没有点击启动或派发任何模型请求**。

| 操作 | 实际观察 |
| --- | --- |
| 打开空普通图，再打开供应商主导航 | 挂载“模型供应商 · 当前资源”，未显示旧 provider 列表 |
| 表单新建并保存供应商 | 当前资源出现 `s1`；没有旧资源双写 |
| 显式创建已有 18 节点串行示例，选择模型源并应用配置 | 下拉可选刚保存的供应商，引用 UUID 与侧栏资源一致；没有运行示例 |
| 从侧栏修改名称并保存 | 同一 UUID 更新为 `s2`，模型源选项立即更新，无需单独刷新节点资源 |
| 保存图、双击切到旧固定图 | 仍显示旧 DeepSeek `r1`，不出现新资源；当前资源面板不挂载 |
| 返回新图并重载，再打开供应商与模型源 | 当前 `s2`、同一 UUID、模型名及已保存图均保留 |
| 浏览器控制台检查 | 本次采集的 error/warn 列表为空 |

验收供应商身份：`workspace / 1b4677fb-34da-47ad-b219-447766d2cf08`。截图与服务日志保留在本地忽略目录 `.local/replace-provider-20261007-7d3e9c/`；运行库、截图和构建产物不纳入公开快照。预览地址为 `http://127.0.0.1:5178/`，仅面向本机，不是生产或常驻部署验收。

未执行：真实模型调用、前后端全量、运行期间的浏览器资源编辑、真实断网/unknown 故障注入、current unknown 在缺包后恢复的浏览器操作、独立安装及常驻验收。unknown/CAS/锁保护本轮取得的是受控自动证据，不冒称上述故障操作已通过。

## 5. 下一步与阶段状态

- REPLACE-02 的普通图供应商主入口缺口已补齐；下一必要切片是 v2 全局内容的 current 管理/选择，仍需解决默认入口与持久化支持范围。
- REPLACE-01 的旧图版本、缺包历史只读、旧存档/outbox、显式资源导入、可选兼容与初始化六项裁决继续保留，不以侧栏切片完成而关闭。
- REPLACE-03–06 尚未完成。没有删除旧产品实现、清理旧记录、改默认包配置、提交或推送。
- DEMO-05/06、COND、LATER 和暂停项不变；不开展长上下文、规模测量或能力扩展。

本轮文件 SHA256，便于识别未提交验证目标：

```text
5405ED1D70C33A17811F3DE63E9A24BCC9F48DF9561335667DD0166922B58D2E  frontend/src/App.vue
FA44CE8A38F09F811AA1D63502AB05E715D2C2A838F86D74E5254AA1A956ABAE  frontend/src/components/ProviderSidebar.vue
BDE4D1FF3677C237F26FF7B369F79891C5315B57D7DEC907EDCE15D56CCE2159  frontend/src/components/ProviderSidebar.test.ts
E15AD9F705A87842B15C2E53B16702C16D854409CEE9B4DA67053C877E7598D8  frontend/src/components/ProviderSidebarResources.test.ts
```
