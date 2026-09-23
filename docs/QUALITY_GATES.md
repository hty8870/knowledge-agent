# 质量门与验证矩阵

验证应与变更风险匹配。不能用“跑了一个测试”替代所有门，也不能为了文档改动无意义地运行整套昂贵评测。

## 1. 先解析真实 Python

所有 PowerShell 命令中的 `$Python` 指一个实际存在、版本不低于 3.10、且安装了项目依赖的解释器绝对路径；`$RepoRoot` 指 Git 仓库根绝对路径。不要假设 `py`、`python` 或 `python3` 一定存在，也不要假设当前目录就是仓库根。

解析与检查方法见 [Windows、Python 与中文路径约定](WINDOWS_ENVIRONMENT.md)。如果没有合格环境：停止验证并报告，不得把命令未找到写成测试通过，也不得未经授权污染系统 Python。

## 2. 统一入口

`automation/quality-gates.json` 是本地与 CI 共用的机器可读单一真源，`scripts/quality_gate.py` 是执行入口。runner 会校验清单、解析真实工具，清空常见密钥以及 `PYTHONPATH`、`NODE_OPTIONS`、代理等进程注入变量，指向空的专用 LLM env 文件，禁用模型下载并设置网络 tripwire；它不会安装依赖。真实子进程测试会验证密钥/注入变量不能回填、非零和超时会 fail-closed。缺少必需工具、超时或命令失败同样 fail-closed。

```powershell
Set-Location -LiteralPath $RepoRoot
& $Python scripts\quality_gate.py --list
& $Python scripts\quality_gate.py --profile full --dry-run
```

| Profile | 用途 | 门 |
|---|---|---|
| `fast` | 每个受支持平台的快速确定性门 | Python AST 编译、浏览器 JS 语法、自动化/CI/发布/项目 skill 契约测试 |
| `full` | 主 Windows/Python 环境的权威公开离线门 | `fast` 全部内容，加 `pip check`、全量 pytest、项目/Web/MCP smoke、冻结+dev 评测、公开 validation manifest、安装器契约 |

本地执行并保留报告：

```powershell
& $Python scripts\quality_gate.py --profile fast --report-json artifacts\quality-fast-local.json
& $Python scripts\quality_gate.py --profile full --report-json artifacts\quality-full-local.json
```

默认 profile 是 `fast`，交付候选包前必须显式运行 `full`。`full` 使用当前 `$Python` 启动真实 MCP selfcheck，因此该环境需要用 `--require-hashes --only-binary=:all:` 从 `requirements/requirements-ci.lock` 安装完整依赖；同时必须能解析 Node.js 与 PowerShell。门内只执行经清单审查的离线路径并设置 tripwire，但这不是操作系统级网络沙箱；首次安装哈希锁依赖可能需要网络。依赖锁、CI 矩阵、候选打包和回滚证据见[自动化质量门与候选发布](AUTOMATION_AND_RELEASE.md)。

仓库中的 `.github/workflows/*.yml` 是配置，不是远程运行证据。没有对应 GitHub Actions 记录时，不得声称 CI 已实跑；当前候选流程也不等于 GitHub Release 或生产部署。

## 3. 正式基线

### 推荐冻结门

```powershell
Set-Location -LiteralPath $RepoRoot
& $Python scripts\evaluate_recommendation.py
if ($LASTEXITCODE -ne 0) { throw '冻结推荐评测失败。' }
```

正式基线：

| 指标 | 期望 |
|---|---:|
| base 数据集数 | 784 |
| Top1 | 97.7 |
| Top5 | 97.7 |
| 硬违规 | 0 |
| FASTQ 违规 | 0 |
| NoResult | 10 / 10 |

未经明确授权不得修改评测数据或阈值以“修绿”。若任务本身是受控重基线，必须同时记录授权、理由、前后差异和新基线证据。

> **受控重基线的内容守卫同步项**：`database/base/` 现有内容级完整性守卫 `tests/test_loader.py::test_base_corpus_file_set_and_content_are_frozen`（补冻结评测抓不到的「改非评分字段」盲区）。**任何合法改动 `database/base/` 内容的受控重基线，必须在同一批次同步更新该测试里的 `FROZEN_BASE_SHA256` 常量**（以及计数 784、`scripts/evaluate_recommendation.py` 的 `FROZEN_TOP1/5`）。指纹是**行尾归一化**后的 SHA-256（`\r\n→\n`、`\r→\n` 后哈希），故对 LF/CRLF checkout 差异免疫、只锁内容——重算见该测试的 `_normalized_content_sha256`（勿用 `Get-FileHash` 裸字节，签入 blob 是 LF、本机工作树可能是 CRLF，裸字节会跨环境不符）。

> **基线变更史（2026-08-17）**：base 774→**784**。此前由同步流程自动入库的 10 条 10x 记录经
> `scripts/promote_uploads.py` 晋升进 tracked 快照 `database/base/10x-synced.json`（uid 排序写出；
> `10x-Visium.json` 逐位未动、SHA 前后一致）。Top1/Top5 **97.7→97.7（不变）**；Avg_Matched_Fields 1.71 不变；
> Duplicate_Family 0.5% 不变；硬违规/FASTQ 违规仍 0；NoResult 仍 10/10（复跑 54 题全 PASS，
> 输出首行「加载 784 条记录」）——新增记录不改变既有 54 题的最优答案，指标逐位持平即预期行为。
> 同批同步：`tests/test_loader.py` 计数 774→784 + `FROZEN_BASE_SHA256` 增 `10x-synced.json` 指纹、
> `tests/test_modality.py` 582→583（晋升含 1 条 flexv2 chromium）、`tests/test_task_pack.py` items 夹具
> 显式取 10x-Visium.json 前 5 条（文件序变化，`records[:5]` 语义漂移防护）、`README.md`
> （774→784、8,012→8,022）、`MODULES.md` 数据边界行、本表。
>
> **基线变更史（2026-08-03）**：base 767→**774**。7 个 2026-04 新上架的 Visium HD CytAssist 数据集追加进
> `database/base/10x-Visium.json`（11mm：human-breast-cancer-tma、human-TA、human-colon-cancer-HE、
> human-ovarian-cancer-IF、mouse-embryo；6.5mm：human-colon-cancer、human-heart）。元数据一律取自
> 10x 官方数据集页（不猜值；页面未陈述疾病状态的 mouse-embryo 与 human-heart 取保守值 `unknown`），
> 7 条均含 FASTQ 直链。**前→后**：Top1/Top5 **97.7→97.7（不变）**；Avg_Matched_Fields 1.71 不变；
> Duplicate_Family 0.5% 不变；硬违规/FASTQ 违规仍 0；NoResult 仍 10/10。评测查询集（54 题）与
> `FROZEN_TOP1/5=97.7` 阈值均未动——新增记录不改变既有 54 题的最优答案，指标逐位持平即预期行为。
> 同批同步：`tests/test_loader.py` 计数 767→774 + `FROZEN_BASE_SHA256` 重算、本表、含 767/语料总数
> 事实陈述的文档。
> ⚠️ 已知问题：其中两个 TMA 行（breast-cancer-tma ↔ human-TA）的名称与链接在原始整理来源中被互换，
> supplement 按 URL join 会把这两条卡片展示的 Spots 数对调（两页 metrics CSV 的 Number of Cells
> 117,730 vs 506,400 可为证）；修复需在源表对调链接后重跑 `scripts/build_sample_supplement.py`。

> **基线变更史（2026-07-25）**：`eval/eval_queries.json` 的 adv05（人或小鼠的脑数据）、adv06（肺癌或肝癌的数据）、adv07（最好是 Xenium 的黑色素瘤数据）
> 由 `no_result_expected: true` 改为可命中题——这三档弃权本来就与代码和评测数据自相矛盾：
> ① `retriever.passes_hard_filter` 逐字写着「正向：须含任一 target」——**同维度多值本来就是「或」**，弃权是白弃的；
> ② 负向侧「命中任一 forbidden 即淘汰」＝ ¬A∧¬B ＝ ¬(A∨B)，「不要小鼠或大鼠」也一直可以精确执行
>（反倒是语义更含糊的「不要小鼠**和**大鼠」因为「和」是虚词一直在照做）；
> ③ adv07 的 `nice_to_have: {"technology": "xenium"}` **本身就把「最好」建模成软偏好**，弃权与本文件自相矛盾。
> 实测对照：「优先 Xenium 的黑色素瘤数据」55 条 vs「最好是 Xenium 的黑色素瘤数据」**0 条 + 0 放宽选项 + 0 降级**，两句除标记词逐字相同。
> **前→后**：NoResult 13/13→10/10；Top1/Top5 97.6→**97.7**（分母 41→44，三道新题**全部 Top1 命中**）；
> Avg_Matched_Fields 1.74→1.71（新题的 must_match 维度数偏少，属分母效应，非排序回退）；Duplicate_Family 0.5% 不变；硬违规/FASTQ 仍 0。
> 仍然明确保护的是「硬条件筛完为空时不要硬凑结果」（`resolution_status == "no_match"`，如 adv08/09/10），本次未改动。
> 同批同步：`scripts/evaluate_recommendation.py` 的 `FROZEN_TOP1/5`、本表。

> **基线变更史（2026-07-12）**：否定/排除语法落地，`eval/eval_queries.json` 的 adv03（不要小鼠的人类数据）、adv04（除了脑以外的人类组织数据）由「弃权/`no_result_expected`」改为可执行负向约束（adv03 = include human + `must_not_match` mouse；adv04 = include human + `must_not_match` brain）。**前→后**：NoResult 15/15→13/13；Top1/Top5 97.4→97.6（分母 39→41，新增两道命中题）；硬违规/FASTQ 仍 0。裁判新增 `must_not_match`（负向进 Top1/Top5/support/违规率同一口径），且 `scripts/evaluate_recommendation.py` 现在**指标不达标即非零退出**（原先只打印 PASS/FAIL、`$LASTEXITCODE` 门形同虚设）。

### Held-out 泛化看门狗（2026-08-06 新增；held-out 集不随公开仓发布）

主集 54 条同时充当开发回归门与发布门、没有 train/dev/test 划分——为补这个洞，曾**盲建** held-out 集（50 条，id h01–h50；作者全程未读解析器/词表/检索源码与主集条目）。**held-out 集与 recall/rerank 的 graded 分级答案不随公开仓发布**：holdout 公开即不再是 holdout，对可见集过拟合后宣称的指标没有公信力。首跑基线留档如下；想复现看门狗，用 `scripts/evaluate_recommendation.py --queries <自建集>` 配合 `--expect-*` 阈值参数即可。

首跑基线（2026-08-06，base 774）：Top1 77.8（35/45）、Top5 80.0（36/45）、硬违规 0.6（1 项，霍奇金 vs 小淋巴细胞淋巴瘤亚型混淆——真实缺陷，留 dev 集修复）、FASTQ 违规 0、NoResult 5/5。

制度口径：

- **`eval/eval_queries.json` 仍是唯一发布门**（上节正式基线不变）；held-out 是泛化看门狗，只暴露主集外的过拟合，不用于放行发布。
- **held-out 只用一次**：首跑发现的问题已留档（逐条清单与泛化 gap 讨论），修复走下一轮 dev 集，禁止据本集结果回改解析器/词表后再拿本集邀功。
- 脚本侧配套：`scripts/evaluate_recommendation.py` 阈值参数化（`--expect-top1/--expect-top5/--expect-max-violation/--expect-max-fastq-violation/--expect-min-noresult`，默认值=冻结基线常量，主集默认行为逐位不变），读 JSON 跳过 `_comment` 等无 `query` 字段的元信息项。

### Public validation 输入合同（2026-09-02）

公开仓不发布私有 holdout，但也不能因此把 Windows 主腿降成 fast。`eval/evaluation-manifest.json`
显式列出实体缺口报告使用的主集、dev 集和 `eval_queries_public_validation.json`；缺任一文件、
路径越界或去重样本低于 manifest 阈值均 fail-closed。public validation 是公开、可反复运行的
回归输入，不冒充未见测试集；private 另有自己的 manifest，把一次性 holdout 加回内部看门狗。

### Dev 集回归门（2026-08-06 新增）

held-out 首跑发现的措辞盲区按纪律**不据 holdout 回改**，修复走 dev 集路线：`eval/eval_queries_dev.json`（55 条，id dv01–dv55）系统覆盖否定后缀句（「X的不要」「X的就不用给了」「别带X」）、「或」句式（「或者…哪个都行」）、fastq 口语（「有fastq吗」「能下到」「得含」「里头有」）、V(D)J（VDJ/TCR/免疫受体库）、非人模式生物（玉米/maize/拟南芥等）、亚型辨析（霍奇金 vs 小淋巴细胞淋巴瘤、急髓 vs 急淋），另含 10 条典型正常用例对照与 5 条 no_result。设计与支撑数口径见文件头 `_comment`。

```powershell
Set-Location -LiteralPath $RepoRoot
& $Python scripts\evaluate_recommendation.py --queries eval\eval_queries_dev.json `
    --expect-top1 99.5 --expect-top5 99.5 --expect-max-violation 0.0 `
    --expect-max-fastq-violation 0.0 --expect-min-noresult 1.0
if ($LASTEXITCODE -ne 0) { throw 'dev 集回归门失败。' }
```

制度口径：

- dev 集是**允许反复跑、据以修解析器**的调参集（与 held-out 定位相反）；门的作用是锁定修复成果防回退。
- 阈值口径：Top1/Top5 = 修复后实测 100.0 小幅下浮至 99.5（49 条计分题下单条失手=98.0 即红）；硬违规/FASTQ pin 0.0；NoResult 5/5。后续「调低阈值」视同放宽门，须按受控重基线流程记录授权与理由。
- 自动化接线：`automation/quality-gates.json` 新增 `dev-recommendation-evaluation`（offline，同安全 posture），挂入 `full` profile。
- 修复后复测：主集逐位不变（97.7/97.7/0/0/10-10）；held-out 唯一一次复测 Top1 77.8→97.8、Top5 80.0→97.8、硬违规 0.6→0.0、NoResult 5/5；首跑未命中 10 条修复 9 条，h28「原位检测」措辞留作下一轮候选。

### Python 测试

```powershell
Set-Location -LiteralPath $RepoRoot
& $Python -m pytest tests\ -q
if ($LASTEXITCODE -ne 0) { throw 'pytest 失败。' }
```

要求当前测试全部通过。不把 passed 数量写死进文档，因为新增有效测试会自然改变数量。

### Web smoke

```powershell
Set-Location -LiteralPath $RepoRoot
& $Python scripts\web_smoke_test.py
if ($LASTEXITCODE -ne 0) { throw 'Web smoke 失败。' }
```

期望输出包含 `WEB SMOKE TEST PASSED`，并确认拆分后的前端资源和相关端点可用。

## 4. 按变更类型选择门

| 变更类型 | 最低验证 |
|---|---|
| 普通客户/开发文档 | Markdown 链接、文件名、UTF-8、命令与事实快照；无需重启 Agent 客户端 |
| `.agents/skills` 项目 skill、规则或模板 | 普通文档检查 + skill 契约测试 + 目标 Agent 客户端新会话真实加载检查 |
| 单个后端工具函数 | 针对性 pytest + 受影响模块测试；交付前说明是否需要全量门 |
| 查询解析、检索、排序、workflow、base 数据 | 针对性测试 + 全量 pytest + 冻结推荐评测 |
| 外部数据装载或上传 | 外部语料/上传测试 + base 784 守卫 + 冻结评测 |
| 前端 JS/CSS/HTML | Web smoke；视觉/交互变化再做真实浏览器验收 |
| HTTP API（响应字段形状/改名） | 后端测试 + Web smoke + 按 `MODULES.md`「响应字段→前端消费点映射」用 `git grep`/`Select-String` 改齐每个消费文件（**三门测不出漏改的前端消费点**——web_smoke 只静态查 JS、不执行 JS，改名会静默显示空白） |
| MCP server | MCP 单测 + 真实 stdio `initialize → tools/list → tools/call` + 典型和错误路径 |
| CLI | 参数、退出码、stdout/stderr 和机器可读输出测试 |
| 公共 schema / `contract_change` | 生产者与全部消费者联合测试；兼容性与迁移说明 |

## 5. 文档级检查

文档改动至少确认：

- 相对链接指向存在文件；
- 代码块围栏成对，PowerShell/Bash/Python 示例可解析；
- 命令没有假设不存在的 launcher；
- 版本、端点数、测试数量等快照没有被误写成永久政策；
- 客户文档不包含个人路径、密钥、代理或内部材料。

## 6. MCP 真实验收

仅直接 import Python 函数不能证明 MCP 可用。至少通过真实客户端或专用 smoke harness 验证：

```text
spawn stdio process
→ initialize
→ tools/list（19 个预期工具，以 mcp_server.py 的 _EXPECTED_TOOLS 为准）
→ biodata_status
→ biodata_llm_status（默认只读配置、不联网）
→ recommend_datasets（典型查询）
→ get_file_manifest（有效 UID）
→ 错误调用后再次 status，确认进程仍可用
```

同时记录响应体积；文件清单可能很大，不应把无限制输出当作健康指标。

项目提供统一入口：`& $Python src\dataset_recommender\app\mcp_server.py --selfcheck`。它另起 stdio 子进程完成 initialize、tools/list、`biodata_status`、离线 `biodata_llm_status` 与 `parse_constraints`，90 秒超时，成功末行含 `SELFCHECK_OK`。它是最低协议门，不替代典型推荐、文件清单和错误后存活测试。涉及 LLM API 配置时，另在明确允许联网且使用测试/本人凭据的环境运行 `& $Python src\dataset_recommender\app\mcp_server.py --llm-check`；只记录脱敏状态与错误码，不记录 Key、路径或服务商原始错误正文。

## 7. 失败处理

质量门失败时：

1. 保存命令、退出码和最小相关输出。
2. 判断是环境缺失、既有失败还是本次回归。
3. 停止交付和合并。
4. 不修改基线、不删除失败测试、不覆盖用户改动。
5. 仅修复本任务范围内且能验证的原因；需要扩大范围时先请求授权并更新任务说明。
6. 最终如实报告未通过项，不得用“应该没问题”替代证据。

## 8. 合并后复验

分支各自通过不等于合并结果通过。协调者在目标分支至少重新运行：

- 受影响的全量门；
- 共享契约相关消费者测试；
- 合并冲突涉及文件的针对性验证。
