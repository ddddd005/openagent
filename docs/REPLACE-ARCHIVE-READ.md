# 退役前归档与旧上下文纯读取

> 历史切片：下文的实现、节点版本、未推送状态和验证结果指登记时快照，不是当前入口。最新使用与工程说明见[文档索引](README.md)，当前退役范围见[节点目录](NODE-DIRECTORY-2026-10-09.md)；不按本页重建旧 API。

承接 [固定基线](BASELINE-2026-10-06.md)、[1.0 规划](PLAN-1.0.md)、[支持与处置矩阵](REPLACE-SUPPORT-MATRIX.md)和 [旧客户端原请求保全](REPLACE-LEGACY-PENDING.md)。本轮按用户同意采用 Git 留档、仓库外备份和按依赖逐批移除；归档不是保留长期双架构的正式运行入口。

## 1. 退役前快照

前四个未提交切片已合并提交为 `a94d560dcd2292b4f8f792623a8f75e13262fe1f`，共 91 个文件，8514 行新增、686 行删除。覆盖公共资源/普通请求边界、执行配置与分类准备、当前应用回执保护及旧客户端原请求保全。提交前核实固定基线、旧客户端六个产品哈希和 staged diff 空白检查；沿用各切片已记录的验证，不冒称本轮重跑过全部范围。

退役前 annotated tag 为 `legacy-retirement-2026-10-07`，指向上述提交。原 `00970f7` 基线和 `237afb5` 的提交、证据及历史记录不改写；前片文档中的未提交状态是当时事实，本页更新当前提交状态。没有推送。

仓库外归档目录：`D:\agent rp\方案1\_archive\openagent-legacy-2026-10-07`。

| 文件 | 范围与核实 |
| --- | --- |
| `source-a94d560.zip` | 标签处完整 Git tracked 源码，含新旧实现、测试、文档及许可；597 个文件路径与 Git tree 完全一致，逐项流式解压成功 |
| `history-a94d560.bundle` | 标签及全部可达历史，`git bundle verify` 确认 complete history、完整性通过 |
| `MANIFEST.md` | 记录提交、标签、范围、SHA256 和独立目录恢复方法；不放入运行仓库 |

```text
3CFFDC81A1127A51C7D53EF0096433923AABCB246727E286B1C102ABDEF61DC7  source-a94d560.zip
8672E324DBF8AA4683113E3D4102FEFCDDE7A1E4F21B07E3E93C19A6C1911D56  history-a94d560.bundle
```

归档是源码备份，不是用户数据备份。不包含 ignored 数据库、运行目录、浏览器存档、已安装依赖或环境凭据；本轮没有读取、搬移、转换或删除用户数据。未执行恢复或回滚，不把仓库外回收站重新纳入构建、测试或运行搜索目录。

后续移除按独立提交推进；需要回查用 `git show`，需要恢复在独立目录检出，不覆盖活跃工作区。固定归档及标签不会随之后的代码改变。

## 2. 解除旧读取依赖

- [legacy_context_contracts](https://github.com/ddddd005/openagent/blob/7ff72afd1e31481e04f7b37b58de961bef7d823f/backend/src/phase1_agent/legacy_context_contracts.py) 接管 basic/prepared 的闭合 Turn 投影和完整冻结准备校验主体。它不构造工作流、Context、Kernel、执行器、数据库或目录；普通 basic 投影也不加载准备算法。
- prepared 读取沿用共用的 frozen correspondence 校验，包括 schema1/2/3、S0/已存 observations、配置、parent/binding、变量事务/seed/重派生及程序结果，不重新执行宏或上下文正则。共享声明及纯验证算法按裁决保留，不以清理为由削弱协议。
- [legacy_archive](https://github.com/ddddd005/openagent/blob/7ff72afd1e31481e04f7b37b58de961bef7d823f/backend/src/phase1_agent/legacy_archive.py) 只使用调用者提供的 `get_record` 读取确切 Turn/InputSnapshot/NodeInput 和成功 owner，保留 run_record/node_run 后备及未知 Context 拒绝。它负责闭合记录校验，**不自行授予会话读取权限**；原冻结 refs 和源节点作用域检查仍由调用者执行。
- [GraphAgentHost._legacy_archive](https://github.com/ddddd005/openagent/blob/7ff72afd1e31481e04f7b37b58de961bef7d823f/backend/src/phase1_agent/graph_agent_host.py) 已委托纯 reader，不再为档案读取导入 `workflow`、`PreparedPromptContext` 或 `BasicContext`。旧 Context 类和公开 frozen wrapper 委托同一主体，原请求门禁的单次结构校验及每次完整 frozen 校验保持。

AST 对照退役前标签：完整 frozen core 和 BasicContext 原投影函数体一致；prepared 投影只把 BasicContext 的 fallback 换成纯函数调用。最初核对脚本误取同名 Protocol 方法而断言失败，修正为精确类方法后核实通过；未为该脚本修改产品算法。

这是解除旧历史读取依赖的限定切片。`GraphWorkflowService` 仍继承旧宿主，迁移本身仍有 `workflow` 身份导入；现档案入口仍由调用者构造 RW store。没有完整旧会话只读门面、全库冻结/恢复跳过、可信历史包来源授权或 schema1–6 frozen passthrough 已完成的结论。旧专用路由/UI/store/host/runtime 的实际移除继续待做。

## 3. 定向验证

新增 [纯档案回归](https://github.com/ddddd005/openagent/blob/7ff72afd1e31481e04f7b37b58de961bef7d823f/backend/tests/test_legacy_archive_contracts.py) 覆盖 basic、prepared1、prepared2、变量重派生和 prepared3；只读 reader、原 owner、确切 Context、输入/根身份、完整冻结变量/程序证据和返回隔离均有拒绝或保全案例。五个 fresh 进程阻断旧工作流、宿主、上下文类、bindings、Kernel、Runtime、SQLite store 及旧资源 store 导入，仍能读取；basic 另阻断准备算法导入。

首批在 `backend/` 执行纯档案、bindings、prepared request gate 三文件：**104 passed，13.47 秒，退出码 0**。这是追加变量拒绝回归前的中间范围，不追加累计。

随后六文件首批：**216 passed / 3 failed，85.85 秒，退出码 1**。三项均为旧迁移夹具未显式选择 compat，在 `_new_session` 遇到 `private_state_not_declared`；新库默认不再加载兼容包是既定支持边界。只修这组旧测试的服务构造，显式选择 `workflow.compat@1.0.0`，不恢复产品默认或放宽私有状态校验。

最终命令：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_legacy_archive_contracts.py tests/test_bindings.py tests/test_prepared_request_gate.py tests/test_graph_legacy_migration.py tests/test_current_runtime_isolation.py tests/test_graph_agent_contracts.py -q -p no:cacheprovider --basetemp ..\.local\pytest-legacy-read-verified-a94d560-617e3
```

最终结果：**6 文件、219 passed，106.14 秒，退出码 0**。新增纯档案 71 项、bindings/门禁 46 项、迁移档案 6 项、当前 Runtime 隔离 1 项和既存纯 Agent 契约 95 项互不重叠。覆盖原范围授权、源会话后来新增历史不进入冻结引用、实际迁移档案/复制读取和当前 Agent 新进程多轮/分叉/重开隔离；首批及复测不重复累计。旧迁移测试仅用于当前仍存在的档案调用者回归，不把它转为本期保留新迁移写入的承诺。

## 4. 操作边界

本轮单代理执行，无新增子任务；仅使用源码、Git 和新建临时测试库。无真实模型调用、用户数据库/浏览器存档访问、常驻服务变更、故障注入、全量回归、旧 prepared 历史超时专项、前端修改/构建或浏览器验收。DEMO-05/06、COND、LATER 继续后置；节点打组及历史正文编辑/删除继续暂停。

最终检查：13 个改动/新增文件的 UTF8、结尾换行、空白及冲突标记通过；六份文档的 154 个本地链接通过；五个产品源码及两个归档文件 SHA256 核实。所有测试命令已结束。纯读取与记录作为归档之后的独立提交，不移动退役前标签，也不把之后源码回填固定 ZIP/bundle。

固定基线 SHA256 保持 `0690253BEC23A2E377245E6F570B2814C3320CEA2C384A4DE927534678C6FCC4`。本片产品源码：

```text
F97FA4481F7A429D8777C25569AEEC2ADEDC095A11B65DA87061D7BA03FC5793  backend/src/phase1_agent/legacy_archive.py
A069CB370E3FCA0272BD0203582829BD6647863FB5E2C60229F3DD2228F55015  backend/src/phase1_agent/legacy_context_contracts.py
E8557C8EF421F65E4B0E5EC791A7D9008DE88D0D1C091378573EC64C5B5C2850  backend/src/phase1_agent/bindings.py
59BE98BEA6089E77CCC49CE0B9C65F64CF51ABA9910F8EE329A601092ECB4C8A  backend/src/phase1_agent/prepared_context.py
3F3FACBF08915BD8787CB0BCD26C4EA2416FEB56AD1158B743B22A8D7C13AA9F  backend/src/phase1_agent/graph_agent_host.py
```
