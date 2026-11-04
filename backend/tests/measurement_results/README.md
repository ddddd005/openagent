# S4-00 第 1-3 步后新成本基线（测量结果）

日期：2026-11-04。负责人：protocol-analyst（task-19）。
脚本：`backend/tests/measurement_harness.py`（应用层测量，隔离临时 SQLite，不启停服务、不调模型、不读写产品库）。

## 复现

在 `backend/` 下（`--workdir` 必须是新的空目录）：

```powershell
..\.venv\Scripts\python.exe tests\measurement_harness.py --workdir <新临时目录> --scenario all --repeat 5 `
  --json tests\measurement_results\s4-00-baseline-<日期>.json
```

单场景/计时重复：

```powershell
..\.venv\Scripts\python.exe tests\measurement_harness.py --workdir <新目录> --scenario chain,config --repeat 5
..\.venv\Scripts\python.exe tests\measurement_harness.py --workdir <新目录> --scenario history,evidence,q08,poll
```

最近一次实测：两次 `--scenario all --repeat 5` 均 exit 0（2026-11-04 22:52，本机 AMD Ryzen 7 9700X / Windows 11 / Python 3.12.8）。
本目录两个 S4-00 JSON 是同一命令的两次独立运行（同一代码快照，两轮对照用于区分结构性指标与受机器负载影响的耗时）。

## 文件

| 文件 | 内容 |
| --- | --- |
| `s4-00-baseline-2026-11-04.json` | 第 1 次完整运行（机器同时有其它并行任务，耗时偏高，可作上界） |
| `s4-00-baseline-2026-11-04-run2.json` | 第 2 次完整运行（较干净，报告以本次为主，并以两次范围标注耗时） |
| `s4-01-evidence-narrow-2026-11-04.json` | S4-01 前后对照：`chain`（全历史读）vs `chain-narrow`（分层初始化+点查）、`chain-history`（owner 已有 100 行历史时同一次保存的前后对照）、`evidence`（含新窄读取 `narrow_reads`） |
| `s4-02-after-2026-11-04*.json` | S4-02（Q08 第二遍 MAX）后复测（backend-finisher 产出；历史扫描对照见 `s4-05-compare-2026-11-04.json`） |
| `s4-05-exit-2026-11-04.json` | 第 4 步清理后全量测量（12 场景：chain/chain-narrow/chain-history/config/history/evidence/q08/session/fault/poll/management/preview），含代码快照哈希 |
| `s4-05-compare-2026-11-04.json` | 与 `s4-00-baseline-2026-11-04-run2.json` 的机读 before/after 对照 |
| `s4-05-exit-materials.md` | 第 4 步整合复测与出口材料（行为复核、对照表、退出码、能力清单、保留接口、限制、S5 前置） |

两个 S4-00 JSON 都含：`environment.code_snapshot_sha256_16`（被测源码快照哈希）、`limitations`、
每个场景的原始行数据（含逐次应用调用明细）。结构性指标（请求数、字节、扫描次数、行数、
`validate_graph_document` 次数）是确定值；`seconds` 是应用层 `perf_counter` 值。

S4-01 复现命令：

```powershell
..\.venv\Scripts\python.exe tests\measurement_harness.py --workdir <新临时目录> `
  --scenario chain,chain-narrow,chain-history,evidence --repeat 3 `
  --json tests\measurement_results\s4-01-evidence-narrow-2026-11-04.json
```

S4-05（第 4 步出口）复现命令：

```powershell
..\.venv\Scripts\python.exe tests\measurement_harness.py --workdir <新临时目录> `
  --scenario all --repeat 3 `
  --json tests\measurement_results\s4-05-exit-2026-11-04.json
```


## 口径

- 只测应用层：`GraphApplication.command/query` 的服务端处理，**不含** HTTP 头、网络 RTT、服务调度、前端真实等待与编辑锁窗口。
- 字节 = `canonical_bytes` 的请求参数 / 响应体 JSON 大小。
- 正常配置保存 = 5 个串行应用调用：`request-evidence.write` → `request-evidence.read` → `definition.update` → `request-evidence.read` → `request-evidence.resolve`（通道首次使用另加 1 次初始化读）。
- 管理命令 = 6 次证据调用 + 1 次业务调用（write、确认读、业务、两次 current 读、结案确认读、resolve）。
- `definition.preview` / `document.contract` 是独立只读查询，不在这场五步链内。
- running 场景由进程内 gate 节点阻塞构造（无模型、无工具、无外部 IO）。
- `unknown` 路径只测后端真实操作：只读 `receipt.read` 核实；未做“提交后丢响应”的故障注入。
