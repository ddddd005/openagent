# 当前资产交付与来源差异

日期：2026-10-22。接续 [157 号 R6](中期检查2/157-六项优先遗留勘察与分批执行规划-2026-10-22.md#36-r6q14-资产与交付)。本篇记录交付组织和可复核来源，不作项目整体许可结论。

## 1. 两件交付

后端 wheel 包含 `phase1_agent/static` 节点页及 `phase1_agent/tavern/frontend` 的既有资产，范围由 [pyproject.toml](../backend/pyproject.toml) 的 package-data 决定。工作台单独构建，Vite 基址为 `/workbench/`，由服务器显式 `--workbench-dist` 指向该构建目录。不将前端 dist 隐式塞入 wheel。

构建应将当前后端源码复制到新的隔离 staging，再用已有构建环境运行 `pip wheel --no-deps --no-build-isolation --no-index`；不得让 egg-info 写入工作树。安装验收使用全新 `--target`，明确记录实际导入路径，不能让当前源码或可编辑安装掩盖漏包。缺依赖须报告，不自动下载或安装构建依赖。

前端类型检查、具名测试、构建、安装后 HTTP 及浏览器是不同证据。静态引用可达不等于浏览器显示或交互验收；本批实际结果另记实施回执。

## 2. 旧复制清单与当前资产

[COPY-MANIFEST.json](../backend/src/phase1_agent/tavern/frontend/vendor/sillytavern/COPY-MANIFEST.json) 保留原始 source、source hash、destination hash 与修改记录。本次没有覆盖该清单来消除两项失配。13 项中 11 项当前字节匹配；两项原 hash 和当前 hash 均能由本仓 Git 对象复核：

| 资产 | 原清单 destination SHA-256 / 对应本仓提交 | 当前 SHA-256 / 对应本仓提交 | 两版本差异 |
| --- | --- | --- | --- |
| `index.html` | `6c656d089799d2cc4182a4292c94b5dd24710045e094426af9ee45bf925d15b8` / `8e7074b2d3ea1d866337bb2cd5274d043658cc6f` | `afdc855da97678c49103c3994bd923952aac1b0d67daafb133672926d4b8d8d3` / `c8af63e4ee15452aae8eba369342710086881d1a` | 移除旧完成回合分叉按钮 |
| `chat.js` | `acc9043fd6980eaf8fe58e5b3f5fc9ddd89420016fd8b47f15608f24de7c88d9` / `cca89a8b69a9d88290c42b06e9c687837a250c06` | `cd79ac3fddc1ea7115b17531af5de07e164b5dce5a04a372a1f917afe62465c3` / `c8af63e4ee15452aae8eba369342710086881d1a` | 移除 `onFork` 构造参数和 `renderForks` 方法 |

本次核对的是本仓历史 destination 对象与当前字节，不声称重新下载或核验了所有上游 source。旧页面入口仍退役，保留 HTML/JS 通过纯文本来源路径读取；许可证与 provenance 路径继续保留。当前记录是原拷贝记录之后的补充，不恢复退役功能，也不重写原复制时点。

## 3. 节点页自持 Lucide

节点页使用 [static/vendor/lucide/lucide.min.js](../backend/src/phase1_agent/static/vendor/lucide/lucide.min.js) 和同目录 [LICENSE](../backend/src/phase1_agent/static/vendor/lucide/LICENSE)，不再从 `/tavern/` 取得节点页图标。当前制品与旧 vendor 中登记的 `lucide@0.577.0` 字节相同：

- JavaScript SHA-256：`0d4402f4813c6ea9d57db2b1cd7af54864e1a49aea5f1873635ed5a819df904b`。
- 许可证 SHA-256：`668dcc52803480e0a026b31140a4cae668772663cd764e5991d252eef03f98db`；文件保留 Lucide ISC 和 Feather MIT 原文。

项目自有头像位于 `static/assets/`；旧酒馆原资产、许可及清单保持原样。安装验收应逐字节核对上述制品和许可证，检查节点页源码无 `/tavern/` 引用、实际资源响应及 `nosniff`。

## 4. 根告知待补登记

[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) 仍处于单独只读保护，本批没有解除或改写。当前哈希为 `fdbdbdc48f92b92485a8434ebe1344708130677a435bc40441824b3bf836e9a1`。以下是可供审阅的精确追加内容，尚未安装到根文件：

```markdown
## Current Node Page and Retained Tavern Assets

The current node page ships a local copy of lucide@0.577.0 under
backend/src/phase1_agent/static/vendor/lucide/. Its LICENSE preserves the
Lucide ISC and Feather MIT notices. The JavaScript and license bytes match
the copies recorded in the retained tavern asset provenance.

Selected adapted SillyTavern files and third-party assets remain under
backend/src/phase1_agent/tavern/frontend/. Their original licenses,
PROVENANCE.md and COPY-MANIFEST.json are retained. This is distinct from
shipping the complete reference checkout. The old page entry is retired;
retained source and legal routes do not restore it.

See docs/ASSET_DELIVERY.md for the two destination files changed after the
original copy record, their exact Git revisions, and current hashes.
This supplementary record does not replace the original source hashes or
make an OpenAgent-wide license determination.
```

上述登记和实际构建/安装证据分别裁定；产物可验证不代表根告知已获准补写。

## 5. 本次交付验证

实际结果见 [158 号实施回执](中期检查2/158-六项优先遗留实施与定向验证回执-2026-10-22.md)。2026-10-22 已执行独立工作台构建、后端隔离 staging 构建 wheel、全新 target 安装及安装后临时 HTTP；没有安装构建依赖、修改本篇列出的原资产或解除根告知保护。

最终 wheel 为 `phase1_agent-0.2.0-py3-none-any.whl`，1,012,008 字节，SHA-256 `3ec1848ec93a488757346478ff280c66ecb43e5e3a42cf6fd6d65b66beb12069`。工作台 index/CSS/JS 三文件身份另列本批 `lead/final-evidence-01.json`，dist 不在 wheel 内。安装探针以 `-I -S` 排除源码/可编辑安装泄漏，460 checks 全过，覆盖当前 Python/运行资产字节、许可、真实导入目录、有限 HTTP 映射与 nosniff、退役入口 410 及 `/workbench/` 引用；临时服务器自建自关，未创建业务数据库。

本批证据位于 `.local/execute157-six-20261022/`，Git 忽略，不保证跨环境携带。首轮探针误纳仓库 `.gitattributes` 的失败原件保留；补重复投影分类 P2 后重建第二份后端产物，未覆盖第一份。未执行浏览器显示/交互验证，根告知第 4 节追加仍需单独授权，不能据安装通过称 Q14 无条件关闭。
