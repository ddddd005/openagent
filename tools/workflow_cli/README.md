# Workflow CLI 操作手册

本工具使用 Python 标准库访问已经运行的可信本地 HTTP 服务，不需要安装 CLI 包，也不负责启动或停止服务。服务真实地址由用户或操作者提供；以下 `$base` 是占位变量，不代表可用地址。CLI 接受 `http` 或 `https` origin，不接受凭据、路径、查询串或片段；请求的 `Origin` 与该 origin 一致。不要把可信本地服务暴露到不可信网络。

在仓库根目录执行，使用已有 Python 环境：

```powershell
$base = 'http://127.0.0.1:8766' # 仅展示地址格式，替换为用户提供的当前地址
python tools/workflow_cli/oa.py --base-url $base describe
python tools/workflow_cli/oa.py --base-url $base catalog
```

先读 [外部 harness 入口](../../docs/harness/README.md)，搭建流程见 [工作流搭建](../../docs/harness/工作流搭建.md)，异常处理见 [排错与已知限制](../../docs/harness/排错与已知限制.md)。操作遵循用户已有授权及服务的实际契约。工具不会自行发起模型调用，但通用 `command` 可发送原生命令，例如 `run.start` 可能运行工作流并调用其配置的模型。

## 命令速查

所有命令都需要 `--base-url`；可选 `--timeout` 为正数，默认 30 秒。可用 `--help` 查看参数。输出为 JSON。

| 命令 | 用途 |
| --- | --- |
| `describe` | 查看服务应用契约。 |
| `catalog [--component ID] [--version VERSION]` | 查看当前节点类型；指定组件时保留其完整描述。组件、版本、端口和默认配置以当前目录为准。 |
| `query OP [--params FILE]` | 执行原生查询；参数文件是 JSON 对象，省略时为空对象，`-` 从标准输入读取。 |
| `command --request FILE` | 发送完整原生命令信封，可能产生对应命令的副作用。 |
| `receipt --request FILE` | 用完整原始命令信封查询 `receipt.read`。 |
| `workflow read ID [--node NODE_ID] [--full]` | 读取工作流；默认返回节点摘要，`--node` 返回指定节点及相关边和绑定，`--full` 返回完整文档（优先于 `--node`）。 |
| `workflow edit --batch FILE --request-out FILE [--apply]` | 根据批次准备完整请求，写入新文件并预演；`--apply` 再发送该请求。 |
| `workflow apply --request FILE` | 发送已有的 `definition.update` 原始请求，成功后正式重读。 |

JSON 文件必须为 UTF-8 且不含 BOM，不能有重复键、非有限数字；`--batch`、`--request` 等读取参数也接受 `-` 表示标准输入。请求信封必须恰有 `operation` 字符串和 `parameters` 对象。`--request-out` 只能创建新文件，不覆盖已有文件。

写操作优先使用具名请求文件。若使用 `--request -`，harness 必须另行保留同一完整原始信封；输出中的 `receipt_request_file: "-"` 不能还原已经消费的标准输入。通用 `command` 不会额外保存一份请求，`workflow edit` 才通过 `--request-out` 显式保存。

## 新建与编辑

先用 `describe`、`catalog` 确认当前契约。新建空图的 `empty.json`：

```json
{
  "schema_version": 1,
  "workflow": {"name": "Example"},
  "operations": []
}
```

新建时可在 `workflow` 指定规范小写 UUID4 格式的 `id`，不指定则生成；初始 revision 为 1，预期 edit sequence 为 0。空图可以保存，不代表它可编译或运行。

显式批量示例 `batch.json`（组件、版本和端口必须先用当前 catalog 核实）：

```json
{
  "schema_version": 1,
  "workflow": {"name": "Example"},
  "operations": [
    {"operation": "node.add", "as": "text", "component_id": "tools.text", "component_version": "1", "config": {"text": "hello"}},
    {"operation": "node.add", "as": "output", "component_id": "tools.output", "component_version": "1"},
    {"operation": "edge.add", "source_node_id": "@text", "source_port_id": "output", "target_node_id": "@output", "target_port_id": "input", "order": 0}
  ]
}
```

修改已有图先执行 `workflow read ID --full`，用本次正式读取的 `workflow_definition_id`、`revision` 和 `edit_sequence` 填写基线。单动作使用完全相同的批次格式，例如 `single.json`：

```json
{
  "schema_version": 1,
  "workflow": {"id": "WORKFLOW_UUID", "revision": 1, "edit_sequence": 1},
  "operations": [
    {"operation": "node.configure", "node_binding_id": "NODE_UUID", "changes": {"text": "updated"}}
  ]
}
```

将占位 UUID 和数字替换为正式读取的值。已有图的操作数组不能空；基线过期会报 `stale_baseline`。先报告冲突并重新读取，审查当前变更与原提案；目标仍明确且在授权内时，显式重新准备，不把旧提案直接套在新计数上。

```powershell
python tools/workflow_cli/oa.py --base-url $base workflow edit --batch batch.json --request-out request.json
# 检查输出中的 changes、aliases 和 preview，以及保存的原始请求
python tools/workflow_cli/oa.py --base-url $base workflow apply --request request.json
python tools/workflow_cli/oa.py --base-url $base workflow read WORKFLOW_UUID --full
python tools/workflow_cli/oa.py --base-url $base receipt --request request.json
```

先准备请求和预演，再依据预演决定是否 apply。预演只读，不预留修改资格；预演指出图不可运行也不等于不能保存，保存成功也不证明图可编译或运行。`edit --apply` 将准备、预演和发送连在一起；它不根据预演内容自行决定图是否可运行。`apply` 接受服务的 `saved`、`unchanged` 或 `derived` 后自动正式重读并返回摘要；需要完整图时按上例另行读取。保留原始请求文件及其内容，用同一信封查询回执，不要对未知结果自动重放。

## 批次契约

顶层必填 `schema_version: 1`、`workflow`、`operations`；可选 `idempotency_key`（非空字符串，服务端要求 1–128 字符，省略则生成 UUID）和 `confirm_derivation`（如提供必须为 `true`）。新建 `workflow` 恰含必填 `name` 和可选 `id`；已有图恰含 `id`、`revision`、`edit_sequence`。除列出的字段外不接受其他字段。操作按数组顺序执行；构造失败不会发送写命令，也不修改正式工作流。

| operation | 必填字段（除 `operation` 外） | 可选字段 |
| --- | --- | --- |
| `node.add` | `component_id`, `component_version` | `as`, `node_binding_id`, `title`, `position`, `config`, `configuration_policy` |
| `node.remove` | `node_binding_id` | 无 |
| `node.configure` | `node_binding_id`，且 `changes` 或 `replace` 恰选一个 | 无 |
| `edge.add` | `source_node_id`, `source_port_id`, `target_node_id`, `target_port_id`, `order` | `edge_id` |
| `edge.remove` | `edge_id` | 无 |
| `binding.put` | `binding` | 无 |
| `binding.remove` | `object_key` | 无 |

`node.add` 从当前 catalog 的默认配置开始，`config` 仅做顶层浅覆盖；`node.configure` 的 `changes` 也只做顶层浅赋值，`replace` 则替换整个配置对象。节点和边 ID 不指定时生成 UUID4；显式 ID 必须是规范小写 UUID4。`as` 是不带 `@` 的唯一非空别名；`@别名` 只可引用本批次更早添加的节点，仅在操作的节点 ID、binding 节点引用以及 catalog 声明的配置节点引用中解析，不能用于边 ID 或任意配置字符串。

`edge.add` 需要精确的源、目标端口 ID，`order` 是非负整数；从当前 catalog 核实端口，不要猜测。`node.remove` 自动移除普通关联边，但不会自动解决控制边、事件目标、对象绑定或声明的节点引用；批次结束仍有依赖会报错。删除 binding 后仍有相应 session preload 也会报错。服务端完整校验仍是权威结果。

当前批次只有上述七种动作。改名、移动或替换节点、修改配置策略、控制边、事件绑定、预加载等没有专用批次动作时，可在正式完整文档上显式构造原生 `definition.update`，先用 `query definition.preview --params FILE` 预演，再用 `workflow apply --request FILE` 提交。保留原文档的其他字段、包锁及双基线，不因此改源码或直接写库。

`binding.put` 按 `object_key` 新增或整体替换绑定，必须提供完整对象。例如下面是一条操作片段，假设本批次已经添加别名为 `agent`、`context-reader`、`context-merge` 的正确节点；实际类型和登记规则仍按当前 catalog 核对：

```json
{
  "operation": "binding.put",
  "binding": {
    "object_key": "agent-a-context",
    "type_id": "workflow.effective-context",
    "schema_version": 4,
    "scope": "shared",
    "owner_node_id": null,
    "registrar_node_id": "@agent",
    "readers": ["@context-reader", "@context-merge"],
    "writers": ["@context-merge"]
  }
}
```

另有可选字段 `default_value`，仅在该类型和初始化规则允许时提供；上例上下文由 Agent 登记，不另造初值。`scope` 为 `shared` 时 owner 必须为 null；为 `private` 时 owner 必须是节点，readers/writers 只能包含该 owner。registrar 可以为 null 或节点；两个权限数组各自不能重复，所有非 null 节点引用必须存在。连线和 registrar 身份均不自动授予读写权限。

## 结果与恢复

- 退出码 0 表示 CLI 命令正常完成，1 表示输入、传输、服务错误或写入需要关注；仍须检查 JSON 内容和服务返回的业务结果。
- `workflow edit` 成功准备并预演时顶层为 `status: prepared`。使用 `--apply` 时还要检查嵌套的 `apply.status` 和退出码，不能仅凭顶层 `prepared` 判定已保存。预演请求失败为 `preview_failed`，请求文件已创建。
- `workflow apply` 的 `saved` 表示服务返回接受结果且后续摘要读取成功；`confirmation_required` 表示服务要求确认派生，CLI 不自动确认。确需派生时依据用户决定和当前契约显式准备带 `confirm_derivation: true` 的新请求，使用新请求文件和新 key；保留旧文件，不修改结果未知的原请求。
- `unknown` 表示写入结果无法确定（例如传输失败、服务 5xx 或缺少可判定结果），保留输出中的 `receipt_request_file`，用原始信封查询回执并核对正式状态，不自动重放。通用 `command` 也可能返回 `unknown`，其原生命令成功响应不经过 workflow apply 的结果判定或正式重读。
- `saved_read_failed` 表示服务已接受保存、未能完成后续正式读取；保留响应，重新读取并核对，不将其当作未知写入。
- 输入错误和其他 HTTP/传输错误通常在 `error` 对象中给出；查看具体 code、details 和服务响应。`command` 输出会遮盖请求中键名包含 `secret` 的字符串及其在响应中的匹配值，但这不是通用的敏感信息清理机制，应保护原始请求文件。

通用命令和查询的参数以 `describe` 为准，不为所有原生命令强加 `definition.update` 的字段或结果语义。例如 `packages.configure` 当前不接收幂等键；凭据和原请求证据各有专用契约。运行控制、会话、资源和包操作的详细参数见[原生 API 说明](../../docs/Agent接入工作流-2026-11-04/外部Agent接入使用说明-2026-11-04.md)；其作用范围及授权不因使用 CLI 而改变。
