# 手工工作流包使用说明

本说明对应已批准的 [151 号准入决定](中期检查2/151-第一阶段A0执行核对与本地包工具准入决定-2026-10-22.md)。
用户手动下载、解压和放置完整包，工作流复用既有 loader 解析准确依赖及注册导出。
本地 Python 包是可信代码，不提供沙箱或导入副作用回滚。

## 1. 放置位置与声明

默认目录是主 SQLite 所在目录下的 `packages/workflow/`。正常服务器入口支持
`--workflow-package-root` 指定其他来源目录；项目 Host 也接受 `workflow_package_root`。
目录不存在时按无扩展处理，不自动创建目录或安装依赖。来源文件可共享，各数据库分别保存启用选择。

每个直属子目录放一个包版本，目录名不决定包身份：

```text
packages/workflow/sample-nodes-1.0.0/
  openagent-package.json
  sample_nodes/
    __init__.py
    package.py
    formatting.py
```

`openagent-package.json` 必须是严格 JSON，不能有重复键。外层只含以下三个字段；
内层 `manifest` 复用现有包声明格式，身份、版本和依赖均为准确值：

```json
{
  "schema_version": 1,
  "entrypoint": "sample_nodes.package:create_package",
  "manifest": {
    "schema_version": 1,
    "package_id": "sample.nodes",
    "version": "1.0.0",
    "host_protocol_version": 1,
    "dependencies": [
      {"package_id": "workflow.content", "version": "1.0.0"}
    ],
    "exports": {
      "nodes": [
        {"component_id": "sample.echo", "component_version": "1.0.0"}
      ]
    }
  }
}
```

声明示例还需配套真实实现。入口是 `CapabilityPackage` 或返回该对象的工厂；
实际 manifest 必须与 JSON 一致，注册结果必须符合 `exports`。包和节点分别使用准确版本。

入口只能取得本包目录内的模块。包内模块使用相对导入，例如 `package.py` 中的
`from .formatting import format_text`。宿主使用独立模块命名空间，不把包目录插入全局
`sys.path`，同名模块及多个包版本分别取得各自文件。缺 Python 依赖会明确报告。

放置或修改包后，按正常方式关闭并重新打开 Host/服务以取得新来源快照。
页面“刷新目录和运行”重新查询当前发布目录，不扫描磁盘热更新。

## 2. 发现与准确启用

放入目录只使包可发现，不自动启用或升级。发现阶段仅读声明；所选包及其依赖才执行入口。
目录包之间、目录包与内置/可信直接提供包的重复准确身份均明确拒绝。

在当前已运行的同源工作台页面开发者控制台读取真实平台接口：

```javascript
const platformResponse = await fetch("/api/graph/platform");
const packagePlatform = await platformResponse.json();
if (!platformResponse.ok) throw new Error(JSON.stringify(packagePlatform.error));
console.log(packagePlatform.local_package_sources.root);
console.log(packagePlatform.local_package_sources.packages);
console.log(packagePlatform.local_package_sources.diagnostics);
console.log(packagePlatform.package_diagnostics, packagePlatform.package_publication);
```

`local_package_sources` 是发现快照，`package_lock` 是已发布集合及其完整依赖。
`available: true` 表示声明可用，不能代替入口加载验证。坏未选项可诊断，不阻断正常选择。

`packages.configure` 接收**完整准确选择集合**，不是增量补丁。先核对现有选择及诊断，
再保留需要的现有包并加入目标准确版本。以下只适用于完整 `sample.nodes@1.0.0` 已经可发现：

```javascript
if (packagePlatform.package_publication ||
    packagePlatform.package_diagnostics.length) {
  throw new Error("先核实当前包失败诊断及已保存选择");
}
const selectedPackage = packagePlatform.local_package_sources.packages.find(row =>
  row.available && row.manifest.package_id === "sample.nodes" &&
  row.manifest.version === "1.0.0");
if (!selectedPackage) throw new Error("目标准确包尚不可发现");
const desiredPackages = Object.fromEntries(
  packagePlatform.package_lock.map(row => [row.package_id, row.version]));
desiredPackages[selectedPackage.manifest.package_id] = selectedPackage.manifest.version;
console.table(desiredPackages);
```

核对 `desiredPackages` 后再提交。此请求会持久保存整个选择：

```javascript
const selectionResponse = await fetch("/api/graph/commands", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    operation: "packages.configure",
    parameters: { enabled_packages: desiredPackages }
  })
});
const selectionResult = await selectionResponse.json();
if (!selectionResponse.ok) throw new Error(JSON.stringify(selectionResult.error));
console.log(selectionResult.result.package_lock);
```

活动运行或尚未释放的执行资源会拒绝选择变更；不能借更换包替换进行中的运行依据。
停用或升级需显式编辑完整选择，核对其他包的准确依赖，不隐式禁用依赖或改写工作流锁。

## 3. 节点配置、保存与执行

成功启用后刷新工作台目录。未知的普通可执行节点进入“其他”，节点选择保留后端提供的准确
`component_id` 和 `component_version`；已知内置节点仍遵守原目录策略。
`GET /api/graph/node-types/v2` 可直接核对已发布声明、包锁和类型目录。

普通节点按 `config_schema.properties` 生成基础控件，创建实例时复制 `default_config`，
不要求另写前端插件。专用扩展必须与准确包版本、导出和已编译进前端的可信实现配对；
解压目录中的 JS 不会自动加载为浏览器扩展。

建议按下列连续步骤核对本次包：

1. 在“添加节点”的“其他”中选择准确节点，检查默认配置，修改一个声明字段并连接所需端口。
2. 等待当前正式配置应用接纳编辑；需要派生确认时核对并确认。出现原请求待核实提示时使用
   “再次只读核实”，保留原请求，不能把本地显示变化当成保存成功。
3. 重新打开工作流，并独立读取正式配置，核对节点身份、配置、修订和包锁。
   公开读取为 `GET /api/graph/definitions/<工作流UUID>`；
   指定固定修订为 `GET /api/graph/definitions/<工作流UUID>/revisions/<修订号>`。
4. 选择与正式固定修订相符的会话，按现有运行入口执行，检查节点结果及运行历史。
5. 正常关闭并重新打开服务后，再核对启用集合、正式配置和原结果；不重新播放旧业务请求。

正式保存仍经 `definition.update` 及现有配置应用的请求证据、确认和独立重读链。
包启用不会自行创建工作流、改配置或重置会话值。

## 4. 失败诊断与已提交事实

选择及完整依赖作为一个候选先校验，再提交并发布。缺包、版本冲突、协议不符及入口/注册/导出
失败均拒绝本次集合，不部分发布；数据库失败不推进 loader 契约历史。

来源及候选校验失败响应的 `error.diagnostics` 提供可定位的来源、准确包与失败阶段；
数据库异常仍按原服务错误路径表达，不能假定每类异常都带同一组字段。
提交后发布失败的持久/内存事实见下述 `package_publication`。
`package_diagnostics` 保留已保存选择缺包等诊断；原有管理能力保留，缺失依据的执行被拒绝。

若 `package_publication` 非空，核对 `selection_committed`、`publication_completed`、
`enabled_packages`；已提交选择不伪称回滚，包锁保留最后确认发布集合。该实例拒绝新的工作流
业务，保留平台诊断、原请求/回执只读核实及关闭，独立供应商/内容域按原契约使用。
核实后正常关闭并重新构造，以已保存准确选择加载；不自动补偿、重放或重试。

## 5. 通用工具接续

首版已接通纯计算及既有变量对象读写。普通节点不会自动成为 Agent 工具；包必须显式注册
带工具适配器的加载节点，声明准确目标实现、工具说明和参数，再由工作流配置将其绑定到目标
Agent。包的启用、节点配置和工具调用权限是不同边界。

完整可手工放置示例见
[text-metrics-1.0.0](../examples/workflow-packages/text-metrics-1.0.0/README.md)；
[入口声明](../examples/workflow-packages/text-metrics-1.0.0/openagent-package.json) 与
[实现](../examples/workflow-packages/text-metrics-1.0.0/text_metrics/package.py) 可直接对照。
按第 1、2 节放置并显式选择 `example.text-metrics@1.0.0`，保留项目原有准确选择。
示例不加入默认包，不需要第三方依赖。

### 5.1 纯计算适配器

从 `phase1_agent.tool_binding_contract` 导入 `PureToolBindingAdapter`：

```python
adapter = PureToolBindingAdapter(
    normalize=normalize,  # (config: dict) -> list[dict]
    call=call,            # (spec: dict, arguments: dict) -> (config: dict, inputs: dict)
    result=result,        # (spec: dict, outputs: dict) -> dict
    prompt_output=("materials", "PROMPT_MATERIALS", 1),
    agent_config_field="agent_node_id",
)
```

`normalize` 每项必须恰好包含下列字段，不包含对象键、ACL、`access` 或变量操作：

```python
{
    "name": "text_metrics",
    "description": "Count characters and lines in text.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to inspect."},
            "include_spaces": {"type": "boolean", "description": "Include spaces."},
        },
        "required": ["text", "include_spaces"],
        "additionalProperties": False,
    },
    "target": {"component_id": "example.text-metrics", "component_version": "1"},
    "config": {"include_spaces": True},
}
```

`call` 和 `result` 接收上述字段的独立副本，另含宿主生成的 `binding_id` 与 `agent_node_id`。
`call` 将模型参数投影为准确目标配置及命名输入端口内容；输入值必须符合端口声明的内容
类型和 schema version。宿主会重新校验返回配置，不能借参数投影绕过目标配置约束。
目标原生 executor 返回命名输出端口内容，宿主校验后才交给 `result` 投影为 JSON 对象。
准备阶段只执行声明解析和校验，不调用 `call`、`result` 或目标 executor。

### 5.2 工作流装配

工具加载节点仍经现有 `NodeRegistry.register(..., tool_binding=adapter)` 注册并受原加载节点
约束。使用原生 executor 的加载节点只能有声明的唯一提示输出；无输入、业务能力、对象
访问、服务、资源或特殊结算。其 `config_node_references` 必须声明
`agent_config_field` 对应的单个准确 Agent 引用。

示例链路为：

```text
example.text-metrics-tools@1
  config.agent_node_id = 目标 agents.execute@10 的节点 UUID
  materials: PROMPT_MATERIALS@1
    -> context.assembly@5 或 @6 的 materials 输入
    -> prompt 输出
    -> 同一 Agent 的 prompt 输入
```

加载节点调用 `context.bound_tool_prompt(config["agent_node_id"])` 取得自身已准备的工具说明，
再生成成对的 `tool-description` / `tool-schema` 材料，保持 Agent、工具名、`binding_id` 和
说明部分的来源证据。public spec 为 `binding_id/name/operation/description/parameters`；
纯计算的 `operation` 是宿主内部调用定位值 `"invoke"`，不属于模型参数或变量读操作。
示例通过已有材料类型传递，不修改旧 `TOOL_PROMPT@1` 的四种变量操作契约。

说明与调用都来自同次 `RunBasis` 的准确包来源和准备帧。说明展示不授予权限；调用不重新
查询当前 registry 代替冻结依据。一个加载节点最多准备 64 项声明，Agent 最终静态工具、
业务工具及唯一 `final_answer` 合计不超过 120 个，工具名不得冲突。

### 5.3 首版边界

| 类别 | 支持或拒绝 |
| --- | --- |
| 参数 | 严格 JSON 对象，`additionalProperties=false`；全部字段恰好必填一次；字段类型仅 `string/integer/number/boolean`，每项含 `description`；允许零参数 |
| 可选、嵌套、数组及 nullable 参数 | 当前拒绝，不因增加包而放宽 schema |
| 结果 | `result` 必须返回严格 JSON 对象；拒绝 Python 专属值、循环和 NaN/Infinity |
| 纯计算目标 | 原生同步 working executor，仅显式参数、配置和准确内容类型；拒绝异步/生成器函数及采用这些实现的 callable 对象 |
| 纯计算状态 | 不提供对象、会话变量、共享态、私有态、外部输入、Host 或工具桥；声明业务能力或形成业务 effects 时拒绝 |
| 既有变量工具 | 原注册变量及 `read/set/add/subtract`，保留 Agent 原 ACL、单对象逐次 `put` 接纳、调用身份和独立重读；不扩为任意对象 CRUD |
| 文件、网络、资源、宿主服务、`executor_ref`、特殊结算、多对象写及删除 | 当前不开放，不由模型参数隐式取得这些权限 |

新增同类工具应由包提供声明、原生目标、适配和提示投影，并修改选择及工作流配置；
Agent、runtime 和通用准备/调用层不增加具体工具名分支。包内 Python 属可信代码，
纯计算约束限定宿主接口，不保证拦截任意 Python 文件或网络调用。

A4 已用隔离库和离线 transport 验证示例装配、模型真实工具调用、事实重读及原变量保护。
这些结果不代表真实上游模型或整个项目已完成最终验收。

### 5.4 独立扩展验证

第二个独立示例为 [numeric-clamp-1.0.0](../examples/workflow-packages/numeric-clamp-1.0.0/README.md)，
准确包身份是 `example.numeric-clamp@1.0.0`，提供 `numeric_clamp(value, minimum, maximum)`。
它仅依赖 `workflow.content@1.0.0` 与 `workflow.agents@1.0.0`，无需导入第一个示例的实现。

该示例在通用协议冻结后加入，只新增包和验证。两个包与既有变量工具已通过同一 Agent 的
正式保存、离线模型工具调用、持久事实读取及关闭重开验证；通用准备、调用、注册和 Agent
核心的六个文件哈希未变。首版范围内新增工具按同一方式提供包，不在核心加入工具名分支。
