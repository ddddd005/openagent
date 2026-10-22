# 会话预加载数据

工作流的“会话预加载数据”保存新会话的初始化配置。普通会话创建时，基础对象初始化后，通过会话域一次性建立这些数据；会话及其种子记录全部提交成功后才返回。后续节点读写的是各自会话的数据，工作流列表不持续同步或重置它们。

实施依据见 [155 号执行规划](中期检查2/155-通用会话数据预加载与统一类型消费执行规划-2026-10-22.md)。本页说明使用和接入规则；实际测试与限制由实施回执记录。

## 1. 配置与使用

1. 在工作流的“会话预加载数据”视图中新建数据项，选择准确的数据类型和版本。
2. 填写对象键、初始值、共享或私有归属，以及允许读取/写入的节点。变量使用名称、值类型、是否赋值及初值表单；普通结构填写符合该类型 schema 的 JSON。
3. 通过工作流原有配置保存流程提交。预加载项和对应对象绑定作为同一次配置修改处理。
4. 创建新会话。初始化成功后，节点通过原有会话对象接口读写数据；不需要另建注册节点。

配置受原有运行锁、归档、修订、待核实和确认派生规则约束。已有有效会话时修改固定配置，按现行规则确认派生；不会改写已有会话。复制工作流只复制配置及预加载列表，不复制会话或运行历史。

对象键创建后在该列表界面中保持稳定，变量名称、初值和权限仍可编辑。不同包的内部配置可能引用对象键，界面不猜测并批量改写这些包内配置。

“未赋值变量”和“变量值为空字符串、0、false”是不同状态。未赋值使用 `assigned=false,value=null`；已赋值必须符合选择的标量类型。

## 2. 唯一声明

schema 3 工作流文档可含 `session_preloads`。每项仅保存 `object_key` 和完整逻辑 `value`，准确类型、版本和权限仍由 `object_bindings` 唯一维护。

下例是文档片段；空的权限列表表示没有节点可以读写，实际消费前应指定工作流中的节点：

```json
{
  "object_bindings": [
    {
      "object_key": "session/affinity",
      "type_id": "workflow.variable",
      "schema_version": 1,
      "scope": "shared",
      "owner_node_id": null,
      "readers": [],
      "writers": []
    }
  ],
  "session_preloads": [
    {
      "object_key": "session/affinity",
      "value": {
        "registered": true,
        "name": "affinity",
        "value_type": "integer",
        "assigned": true,
        "value": 0
      }
    }
  ]
}
```

列表键必须唯一且有匹配的绑定。同一绑定不能再含 `default_value` 或 `registrar_node_id`。旧注册节点继续服务它们自己的数据键；本功能不自动迁移旧节点，不允许两种来源同时负责同一键。

没有新字段的历史文档保持原形状，不自动添加空列表。schema 1/2 不接受该字段；旧程序也可能拒绝包含新字段的文档，不应将其交给旧程序编辑。

## 3. 新结构类型

新结构通过现有可信[本地工作流包](WORKFLOW_PACKAGES.md)注册，使用原有 `DataTypeDefinition` / `TypeRegistry`，进入同一准确类型目录。预加载列表不内嵌第二套类型定义。

普通结构首版要求：

- `scope="session"`，准确 `type_id` / `schema_version`，`checkpoint_admission=True`。
- 使用声明式 JSON schema；列表必须给出完整合法初值，类型本身可以没有默认值。
- schema 引用只允许本地 fragment；远程或文件引用在值校验前拒绝，不为预加载访问外部资源。
- 不依赖自定义 validator、引用解析/映射、迁移、写入钩子、artifact/public 引用或内容变换。需要这些行为的类型明确拒绝进入普通列表；不能把它们的专用语义当作普通 JSON 复制。

`workflow.variable@1` 复用已有变量校验器。变量、chat 和 effective-context 的类型 ID 保留专用语义，其他版本不能借普通结构进入列表。Agent/context/chat 等特殊类型继续原有领域路径。首版不提供交互式类型定义 CRUD 或通用数据草稿工具。

例如，在包的既有注册函数中提供一个无默认值的普通结构：

```python
from phase1_agent.host_sdk import DataTypeDefinition

host.register_data_type(DataTypeDefinition(
    "example.session-state", 1,
    {"type": "object", "properties": {"count": {"type": "integer"}},
     "required": ["count"], "additionalProperties": False},
    scope="session", checkpoint_admission=True,
))
```

包按准确版本加载后，可选择该类型并填写 `{"count": 0}` 作为列表初值。`host` 是包注册回调收到的原有注册接口；上面是注册片段，完整包入口与 manifest 仍按本地包说明组织。

节点读取和更新仍通过原有对象接口，并声明所需对象能力及准确访问类型。列表初始化不会给节点增加权限，也不改变私有对象归属、CAS 或原有写入事务。

## 4. 历史与失败

普通创建使用固定工作流依据中的初值，每个新会话只应用一次。原创建请求重放返回原回执，不重新初始化。初始化或后续种子提交失败时，原创建事务整体回滚，不留下半创建会话。

检查点和楼层分叉使用所选历史数据，不重新应用配置初值。变量保留原 `variables` 分区；明确预加载的普通结构使用可选 `preloaded_items` 分区。准确类型、键集或恢复 CAS 不匹配时明确失败，不补默认值。候选切换和重新生成沿原聊天恢复流程还原相应计算数据。

原检查点缺少新分区时保持原存储形状。新分区是当前 schema 4 的可选扩展，不涉及数据库迁移；旧程序未获得读取新分区的能力。

Agent 继续只能通过已有获准工具操作已存在的值。本功能不会赋予 Agent 类型定义、首次注册、创建/删除声明或工作流管理权限。
