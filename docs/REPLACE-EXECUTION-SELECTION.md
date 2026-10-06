# REPLACE-03 执行配置分流与纯分类准备

承接 [固定基线](BASELINE-2026-10-06.md)、[支持与处置矩阵](REPLACE-SUPPORT-MATRIX.md)、[1.0 规划](PLAN-1.0.md)和上一片 [公共资源与请求门禁解耦](REPLACE-RESOURCE-BOUNDARY.md)。已有提交仍为 `237afb5c0692f78b05f1aad912a53941d67e9aee`，尚未推送；本片及上一资源边界切片均在该提交之上的未提交工作区，不倒改固定基线或历史验证记录。

本片由三个并发子代理分别实现配置存储/依赖解析、纯记录分类准备及独立存储保全测试，主代理完成构造入口接线、合并复核和文档。范围是下一轮冻结实施所需的准备，不是完整混合库隔离，也没有启用冻结写守卫或退役旧入口。

## 1. 执行配置与历史保全

沿用现有 `graph_project_packages(configuration_id,payload)` 表，不新增 schema 或版本迁移：

| 配置行 | 语义 | 本片读写规则 |
| --- | --- | --- |
| `current-execution` | 当前明确选择的执行配置 | 构造显式选择和 `packages.configure` 只写此行；新库默认也仅建立此行 |
| `project` | 既存历史精确根选择 | 作为未建立当前配置时的读取后备；不删除、重写、canonicalize、升级或剥除 compat |
| 两行均不存在 | 新库默认选择 | 使用原 `DEFAULT_PACKAGES`，成功后写当前行；缺默认精确包仍拒绝，不回落空目录 |

读取优先级为当前行、历史行、默认选择；只解析命中的 payload，未使用的损坏历史正文不会覆盖合法当前配置。显式 `{}` 是真实选择，不视作缺失；旧空选择、compat 选择及缺包原文均保留。显式新选择不需要先解析旧配置，但必须通过原精确包加载和类型契约检查。

构造和管理入口都将 `TypeContractStore.check_registry` 与当前配置写入置于同一事务；类型冲突或写入失败不留下部分新声明或替换已保存配置。旧类型 digest、精确 schema-version 意义、原包锁及业务命令 shape 不变，`packages.configure` 仍只接收 `enabled_packages`。

主要接线：[配置存储](../backend/src/phase1_agent/graph_package_selection.py)、[构造入口](../backend/src/phase1_agent/graph_service.py)、[管理入口](../backend/src/phase1_agent/graph_platform.py)。

## 2. 纯依赖解析与分类准备

`CapabilityPackageLoader.resolve` 提取原 `load` 的精确根选择、依赖闭包、冲突、环及 host protocol 校验，返回完整锁、原 manifest 声明及注册顺序，不调用 `register` 或装载执行实现。`load` 复用该解析，再按原顺序注册和检查实际导出；原错误码及 UI-only / execution lock 分离保持。纯 resolve 的 manifest 不是已接纳导出证据，尤其 compat 的原 `exports={}` 不代表实际无导出。

[图记录分类 helper](../backend/src/phase1_agent/graph_record_classification.py)仅接收图定义、纯解析结果、精确包来源及已接纳实际导出证据，准备 `current/frozen/blocked` 评估。判定核对完整锁与依赖闭包、确切组件版本及 owner 集合；不按节点前缀、schema 年龄、版本大小或 `registry.get` 猜测新旧。完整锁中已验证的 compat 使整份图判为 frozen，即使图中节点均为 current；未知包、缺失精确版本、无来源或未核实导出均为 blocked。共享类型保留 owner 集合，不将 `GLOBAL_RESOURCE_REF@1` 独占归属 compat。

这是图定义的内部纯评估准备，不是全记录家族分类、HTTP 新字段、运行授权或完整可用性证明。分类调用不读写 SQLite、不注册包、不加载旧执行器、不补类型基线、不迁移、恢复或裁锁；它也不核实全部历史事实闭包、持久类型 digest 或同进程执行现场。

## 3. 本片未启用的保护

以下工作必须与请求保护一起继续，不能因配置分流或 helper 到位而略过：

1. 记录级写守卫覆盖旧定义与传入新定义，防止删旧锁后保存；复制、分叉、接纳、对象和数据命令须检查全部源/目标/候选/生产者。
2. 启动恢复跳过已证明 frozen/blocked 的旧记录；原 status、revision、active、facts、manifest、预算和回执保持，不能伪造结算或解除占用。
3. 配置切换只忽略已证明冻结的数据库活动记录，并继续阻止 current 活动链、future、暂停帧和其他同进程现场；本片原全部 active 检查保持。
4. 纯档案与受作用域限制的 receipt-only 查询先行；原回执优先精确回放，无法核实的请求保持 unresolved。冻结拒绝不能让客户端清除此前结果未知的 pending。
5. 浏览器 schema1–6 frozen passthrough、静态旧键只读桥及正式旧入口替换与上述保护合并验收，不重 POST、不借 404/410/CAS 销项。

本片不改变 `_change` 的全局缺包阻断、`_recover`、状态投影、consumer exact shape、活动链配置限制或旧路由；没有旧档案混合库原事实不变且当前新图可运行的完整联合结论。纯分类也不绕过 GraphRecordStore 的全库契约校验。

## 4. 定向验证

合并验证在 `backend/` 执行：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_graph_package_configuration.py tests/test_graph_execution_selection_custody.py tests/test_graph_record_classification.py tests/test_capability_packages.py tests/test_graph_package_selection.py tests/test_graph_platform.py tests/test_graph_application.py tests/test_graph_fresh_http.py tests/test_current_runtime_isolation.py tests/test_type_contract_store.py -q --basetemp ..\.local\pytest-selection-final-237afb5-8408fd23
```

最终结果：**10 个文件，160 passed，33.67 秒，退出码 0**。包括新增配置/解析 23 项、保全 29 项、分类 39 项及既有受影响 69 项，范围互不重叠；不与子代理、首轮或其他切片重复累计。

覆盖：

- [配置与解析](../backend/tests/test_graph_package_configuration.py)：有效行读取、显式空选择、历史 raw 不变、事务写入边界、精确依赖/版本/拓扑/错误码、resolver 不注册且不改变 loader 契约缓存，新进程禁止旧实现导入。
- [服务保全](../backend/tests/test_graph_execution_selection_custody.py)：构造/configure/current 优先、显式跨过旧坏 payload、缺包无 fallback、SQL 写失败和类型冲突共同回滚；原生 SQLite 只读完整原行快照核对，以及 current running/paused 继续阻止换包。只涉及新建测试库配置和 current 范围，不外推旧 frozen active 已保护。
- [图分类](../backend/tests/test_graph_record_classification.py)：完整锁、依赖闭包、精确来源、实际导出、未知/dormant 身份阻断、compat 优先级、共享 owner 及输入/返回隔离。纯 fixture 的注册回调全部拒绝，fresh 进程禁止旧执行/存储导入；另四个真实包夹具只在准备阶段注册，分类阶段禁止 load/注册，确认 current Agent 三版、真实 compat 30 节点/3 类型及 UI-only 锁边界。
- 既有 capability/selection/platform/application、fresh HTTP、当前 Agent 新进程多轮/完成分叉/重开隔离和持久类型契约的受影响范围。HTTP 完成运行及精确历史重开不构造兼容 registry 或旧 private runtime，新库只生成当前配置行。

保留初跑与修正过程，不重复累计：

- 主代理五文件首跑 **48 passed / 1 failed，30.65 秒**。唯一失败在 fresh HTTP 末尾仍查询旧 `project` 行，运行及历史重开已经成功；仅把断言改为查询当前行并检查历史行缺席。该文件与六项持久类型回归随后 **7 passed，4.43 秒**，与最终合并范围重叠。
- 分类最初 **33 passed，1.67 秒**；两名代理独立只读发现手工构造 `PackageDependency` 的非法身份会在 `set(order)` 抛 `TypeError`。改为先调用原 `to_dict` 严格校验，再建立拓扑坐标，新增两个畸形身份回归；不宽泛吞异常。中间 **35 passed，1.48 秒** 不追加累计。
- 四项真实包夹具初跑 **36 passed / 3 failed，10.76 秒**；失败都是新增断言错误地把 `workflow.frontend-business` 当成纯 UI。它含真实业务节点和类型，本来就须保留执行锁；仅修正夹具，最终 focused **39 passed，4.24 秒**，不为断言改变产品锁算法。
- 配置/解析子代理 **23 passed，0.87 秒**，保全子代理 **29 passed，4.19 秒**；它们及上述重跑全部与最终合并范围重叠，不累计为新案例。

主代理 AST 对照 `237afb5` 确认：精确选择与完整依赖遍历原样迁入 resolve，`_change`、`_recover`、`_view` 及 configure 的原 active 检查保持。三代理只读交叉核配置事务、保全及分类证据；只读核查不计测试通过。

只使用新建临时 SQLite 和 ignored `.local` basetemp，不读取用户数据库；原 prepared 历史超时不在本片重新运行或销项。没有全量测试、前端类型/构建或新增浏览器证据。

## 5. 后续与操作边界

下一步接上述保护并提供纯档案/回执及客户端隔离，再退役剩余旧 host/runtime/路由/UI/store。REPLACE-02/03/04/05、阶段 B/C 和 1.0 尚未完成；配置分流不是通用多项目能力，纯分类不是冻结已经生效。

本片没有前端产品修改、浏览器新证据、真实模型调用、常驻服务变更、用户数据清理、全量回归或追加提交/推送。既有服务保持不动；上一切片的已知等待失败和原门禁局部对照证据仍归原记录，不将新增通过累计为其修复。

DEMO-05/06、COND、LATER 继续在 1.0 之后按需安排；节点打组及历史展示正文编辑/删除继续暂停。固定基线 SHA256 保持 `0690253BEC23A2E377245E6F570B2814C3320CEA2C384A4DE927534678C6FCC4`。

本片主要模块 SHA256：

```text
087B790491D8705773A3B533B1A7A6709039A31E17FAC3E5A709425E6B2DA22D  backend/src/phase1_agent/graph_package_selection.py
3CA2325DEBF0CDB9BFCA7718F860E8992E62701BB867431845B78E79C11D0CC7  backend/src/phase1_agent/graph_record_classification.py
13B8D2B4C995D20DF7D7648F7D1E19867A13303CFA7311D79369662C5ED41D95  backend/src/phase1_agent/capability_packages.py
648BED14BE059EC4D97ADE6AF275226B3E676338C1D732F5141D279595026DED  backend/src/phase1_agent/graph_service.py
ABCD57D4DD199C67CD292B7E67237572041A7C2C844313C21A5C22731C268472  backend/src/phase1_agent/graph_platform.py
```

最终检查：七份相关文档的 151 个本地行内链接通过；整个未提交工作区 37 个改动/未跟踪文件的空白、结尾换行及冲突标记检查通过，包含普通 `git diff --check` 不覆盖的新增文件。固定基线及两份切片记录中九个模块 SHA256 均核实。三个子代理及本片测试命令已收尾；HEAD 仍为 `237afb5`，本片未追加提交或推送。
