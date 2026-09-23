# BioData Agent 开发指南

本文面向需要本地运行、调试、扩展或集成 BioData Agent 的开发者。用户操作请先阅读 [README](README.md)。

> **阅读导航（渐进披露）**：本文件较长，建议先 `grep -n '^#' DEVELOPMENT.md` 取小节目录，再按行区间读。常用落点：环境搭建 → §1；运行入口 → §2；HTTP API → §6；MCP 接口 → §7；前端开发 → §8；测试与验收 → §10；交付检查 → §11。

## 1. 运行环境

- Python 3.10 或更高版本
- Windows PowerShell，或 macOS / Linux shell
- 生产运行的最小依赖见 `requirements/requirements.txt`（不含 pytest）
- 测试/MCP 等 CI 直依赖见 `requirements/requirements-ci.txt`
- 可复现 CI/候选发布环境见带哈希的 `requirements/requirements-ci.lock`
- 本地语义重排的可选依赖见 `requirements/requirements-embeddings.txt`
- MCP 服务使用独立的 MCP SDK 环境，安装方法见[教程](使用教程/MCP安装/MCP_安装教程.md)

建议为项目使用独立虚拟环境，不要把依赖直接安装到系统 Python。双击启动器默认也只自动使用/创建项目内 `.venv`；不会猜测并复用 workspace 或 MCP 环境。高级用户可以用 `BIODATA_PYTHON` 显式选择其它解释器，此时依赖隔离由调用者负责。

Windows PowerShell：

```powershell
Set-Location -LiteralPath 'C:\你的路径\biodata-agent'
python -m venv .venv
$Python = (Resolve-Path '.\.venv\Scripts\python.exe').Path
& $Python -m pip install -r requirements\requirements.txt
```

macOS 或 Linux：

```bash
cd /path/to/biodata-agent
python3 -m venv .venv
PYTHON="$(pwd)/.venv/bin/python"
"$PYTHON" -m pip install -r requirements/requirements.txt
```

如果系统中的 `python` 不是 Python 3.10+，请改用实际解释器绝对路径。Windows 双击启动器也支持通过 `BIODATA_PYTHON` 指定解释器。

## 2. 运行入口

| 入口 | 命令 | 用途 |
|---|---|---|
| Web | `& $Python scripts\run_web.py` | 启动前端与 FastAPI 服务 |
| CLI | `& $Python src\dataset_recommender\app\cli.py --query '...'` | 命令行检索 |
| MCP | `& $McpPython src\dataset_recommender\app\mcp_server.py` | 启动 stdio MCP 服务 |
| MCP 自检 | `& $McpPython src\dataset_recommender\app\mcp_server.py --selfcheck` | 验证协议、工具和数据状态 |
| 推荐评测 | `& $Python scripts\evaluate_recommendation.py` | 验证检索质量基线 |

Web 默认监听 `127.0.0.1:7860`，两个官方启动入口都显式锁定 `workers=1`。下载任务、桌面壳活动状态和部分响应缓存当前是进程内状态；在完成状态外置前，不支持用多个 Uvicorn worker 横向扩展。可在当前进程设置 `PORT` 改端口：

```powershell
$env:PORT = '7861'
& $Python scripts\run_web.py
```

表中的 `$McpPython` 指 MCP 教程创建的专用环境。Windows 默认位置可这样取得：

```powershell
$McpPython = Join-Path $env:LOCALAPPDATA 'BioDataAgent\mcp-venv\Scripts\python.exe'
```

服务启动后可访问：

- 首页：<http://127.0.0.1:7860/>
- 健康检查：<http://127.0.0.1:7860/api/health>
- OpenAPI 文档：<http://127.0.0.1:7860/docs>

## 3. 系统结构

```text
数据装载与归一化
  data_loader.py / normalizer.py / corpus.py / downloads.py
            ↓
查询理解
  search_request.py / query_parser.py / query_lexicon.py（语言级词法；领域词表在 domains/biodata/vocabulary.py）
            ↓
过滤与排序
  retriever.py / strategy.py / vector_recall.py / rerank.py
            ↓
流程编排
  workflow.py
            ↓
接口
  webapp.py / cli.py / mcp_server.py
```

设计原则：

1. 明确条件先做结构化硬过滤，排序层只处理已经通过过滤的候选。
2. LLM 和本地语义模型都是可选增强；缺少 Key、网络、依赖或模型时应回退到确定性路径。
3. Web、CLI 和 MCP 复用同一套查询理解与推荐工作流，不在接口层复制检索逻辑。
4. 基础库与外部库保持物理隔离，用户上传不得写入基础库。

## 4. 主要目录

| 路径 | 内容 |
|---|---|
| `src/dataset_recommender/` | 后端业务模块 |
| `web/static/` | 前端 HTML、CSS 和 ES Module 脚本 |
| `database/base/` | 10x Genomics 基础数据，默认检索与评测使用 |
| `database/external/` | 其他公开来源及用户上传数据 |
| `src/dataset_recommender/data/` | 文件级下载索引等运行数据 |
| `scripts/` | 启动、MCP 安装、模型下载、评测和冒烟测试 |
| `tests/` | 自动化测试 |
| `eval/` | 评测查询、基线和审计报告 |
| `使用教程/` | 面向使用者的专项教程 |
| `automation/` | `quality-gates.json`：质量门清单，本地与 CI 共用同一份 |
| `.github/workflows/` | CI 与候选发布工作流定义（配置存在不等于远程已执行） |
| `.agents/skills/` | 可供 AI 助手安装的行为规范，见 `使用教程/Skill安装/` |
| `docs/` | 发布流程等工程文档 |

`database/base/` 是稳定基准，不应接收上传或普通数据修订。外部数据写入 `database/external/`，装载时允许跳过单个损坏文件，不影响其他来源。

## 5. 配置

### 5.1 基础配置

`.env.example` 是模板，程序不会直接把它当作运行配置。需要服务端 LLM 配置时，复制为 `.env` 后填写：

```powershell
Copy-Item .env.example .env
```

常用变量：

| 变量 | 作用 |
|---|---|
| `DATA_DIR` | 基础数据目录，默认 `database/base` |
| `TOP_K` | 默认返回数量 |
| `ENABLE_LLM` | 是否允许使用 LLM |
| `LLM_PROVIDER` | `zhipuai`、`openai-compatible` 或 `mock` |
| `LLM_API_KEY` | 通用 API Key |
| `LLM_BASE_URL` | OpenAI Chat Completions 兼容根地址 |
| `LLM_MODEL` | 模型名 |
| `LLM_TIMEOUT` | 请求超时秒数 |
| `BIODATA_LLM_ENV_FILE` | 项目外 LLM 配置文件路径 |

同一变量的优先级：进程环境变量最高；其次是「配置来源文件」——一旦设置 `BIODATA_LLM_ENV_FILE`（须为存在的绝对路径），即以它为准，**此时不再读取**项目 `.env` / `.env.zhipu`（外置文件是独占来源，不与项目文件逐项叠加）；未设置时才回落到项目 `.env` 或 `.env.zhipu`；最后是程序默认值。

不要提交 `.env`、外部密钥文件、浏览器导出的配置或任何真实 Key。

### 5.2 Web 请求中的 Key

Web 设置页可以把 API Key 随单次 `/api/recommend` 请求发送到本地服务。后端不会把该 Key 写入项目文件。Key 默认只在页面会话内存中；前端只有在用户同时主动开启“记住非敏感设置”和独立的“也记住 API Key”后，才会把 Key 保存到当前浏览器的本地存储。没有独立密钥授权的旧版残留会在设置加载时删除。

前端把 DeepSeek、Kimi、Qwen、GLM、OpenRouter 与 OpenAI 暴露为一键预设；这些展示值统一映射到后端既有的 `zhipuai` 或 `openai-compatible` 协议，不增加后端 provider 枚举。预设只负责填入可编辑的 `base_url` 与 `model`，自定义兼容接口和本地模型入口继续保留。

共享环境建议使用服务端环境变量或项目外密钥文件，不要依赖浏览器保存。

## 6. HTTP API

当前 Web API 版本为 `3.0.0`。FastAPI 会在 `/docs` 生成当前版本的请求和响应模型。以下表格用于快速定位（**与 `webapp.py` 的路由装饰器一一对应，共 65 个**；改路由时同步这张表）：

| 方法 | 路径 | 作用 | 是否写数据 |
|---|---|---|---|
| GET, HEAD | `/` | 返回前端页面 | 否 |
| GET, HEAD | `/dataset` | 返回数据集详情独立页 | 否 |
| GET | `/favicon.ico` | 返回站点图标（内联 SVG） | 否 |
| GET | `/api/health` | 服务健康状态 | 否 |
| GET | `/api/domain/manifest` | 活动领域清单（品牌/维度/基础来源；骨肉分离架构的前端品牌真源，additive 只读） | 否 |
| GET | `/api/local-model/status` | 可选本地语义模型的安装/就绪状态（只回状态、阶段、体积，不暴露本机路径/uv 输出/下载源原始错误） | 否 |
| POST | `/api/local-model/install` | 显式启动可选本地语义模型后台在线安装（独立 venv + 固定模型，单飞，失败不影响规则排序） | 是（写本机数据目录） |
| POST | `/api/local-model/cancel` | 取消进行中的本地模型安装（失败/取消不写 READY） | 是（写本机数据目录） |
| POST | `/api/account/register` | 注册本地账户（scrypt） | 是 |
| POST | `/api/account/login` | 登录，签发会话 cookie | 是 |
| POST | `/api/account/logout` | 注销当前会话 | 是 |
| POST | `/api/account/switch` | 一键切换账号（校验记住的会话 token → 重设 cookie） | 是 |
| GET | `/api/account/whoami` | 返回当前登录账户 | 否 |
| GET | `/api/account/trial-quota` | 限量试用通道当日额度回显（可用性/锁定模型/已用/剩余；仅护栏形态，本机 404） | 否 |
| POST | `/api/account/mcp-token` | 铸在线 MCP 接入令牌（返回明文一次 + 即用的 url/headers 配置；落盘只存 sha256 摘要；仅护栏形态，本机 404） | 是 |
| GET | `/api/account/mcp-tokens` | 列出当前账户的在线 MCP 令牌（摘要视图：id/label/prefix/时间，无明文；仅护栏形态） | 否 |
| POST | `/api/account/mcp-token/revoke` | 吊销当前账户的一枚在线 MCP 令牌（仅属主；仅护栏形态） | 是 |
| POST | `/api/interpret` | 解析来源、时间和生物条件，不执行检索 | 否 |
| POST | `/api/recommend` | 执行推荐主管线 | 否 |
| POST | `/api/feasibility` | 研究问题→可行性概览（候选数/总细胞量下限/分布/缺口） | 否 |
| POST | `/api/board/plan` | 条件板：把「再说一句话改条件」规划成一次改动（只规划、不检索） | 否 |
| POST | `/api/action/plan` | 执行层：把一句话归一化成封闭动词表里的一个动作（**只出计划、不执行**；产物由前端另一次显式请求产生） | 否 |
| POST | `/api/utterance` | 统一对话路由（turn pipeline，无并行短路）：规则匹配（一切指令都过；零命中/弃权 ≠ 无效）→ LLM 分流（带原话+命中概览+当前查询与条件）→ search（带 effective_query）/ tool（EXEC plan）/ none（如实回音）；LLM 缺席时规则兜底（动作词→tool 规则档；search_shaped→search；其余→none）。**AI 执行开启时 EXEC 动词经 agent 图在本端点内真实执行**（search_online/sync_updates 写外部库 upload_*，记账+回收站可撤+写入汇强制流水账） | 是（agent 执行档） |
| POST | `/api/dream` | dream 记忆整理：历史对话 → 封闭 JSON 记忆候选（generated:true；解析失败=空清单，绝不编造；LLM 配置请求级、不持久化） | 否 |
| POST | `/api/task-pack/preview` | 任务包第一步：先给清单（会收录什么、装不了什么、缺什么） | 否 |
| POST | `/api/task-pack/build` | 任务包第二步：产出 14 个文件（ZIP 或逐文件文本） | 否 |
| POST | `/api/upload` | 上传 JSON 到外部数据目录 | 是 |
| POST | `/api/curate/plan` | 对话式数据库管护第一步：preview + confirm_token（零写盘；search_online 联网查官方源并记请求账本） | 否 |
| POST | `/api/curate/apply` | 管护第二步：回传 confirm_token 真执行（token 不符零写入；写外部库 upload_* 与回收站） | 是 |
| POST | `/api/curate/check-updates` | 检查来源更新（只读无 token：ArrayExpress/ENCODE/10x 在线比对最新清单，拉不到如实降级 snapshot；其余离线快照源如实报告本地快照+官网入口） | 否 |
| POST | `/api/curate/sync-updates` | 检查更新→有新增则自动入库的复合流（`curate.sync_updates`；无 token——原子调用无信任边界，写侧走 uploads 管线+账本+回收站可撤；每源最多 10 条、全请求总预算 30 条，超预算如实标注；guard on 走异步——启动/附着进程内单飞 job、202 返回 `{ok, job, async:true}`；guard off 阻塞逐字节不变） | 是 |
| GET | `/api/curate/sync-updates/status` | 语料同步 job 状态轮询（登录即可、无 token 闸；guard off 下 job 恒 idle） | 否 |
| POST | `/api/admin/corpus-sync` | 定时任务用：启动/附着语料同步 job（恒全源，202 立即返回不阻塞；双闸自认证=env BIODATA_ADMIN_TOKEN 经 X-Admin-Token 头 hmac 比对 + 仅 loopback 对端，未配置 token → 403 fail-closed） | 是 |
| GET | `/api/admin/corpus-sync/status` | 定时任务用：语料同步 job 状态查询（同双闸） | 否 |
| POST | `/api/curate/status` | 数据库状态汇报（`curate.db_status`；只读离线：各源条数/快照日期+外部库与回收站清单+近期审计摘要；与 agent 图内 observe 同一 `corpus_status.db_status` 真源） | 否 |
| GET | `/api/curate/sync-status` | 实例级同步状态（上次同步时间/上次 operation/是否 busy；只读不写盘，busy 实时探测 sync 整任务锁；「上次同步」是实例级事实，不得存 per-profile） | 否 |
| POST | `/api/curate/recall` | 按 operation_id 整次撤回一次 sync 的全部成功写入（回收站语义可逆，可重入，单文件失败不连累其余；operation 不存在 → 400） | 是 |
| POST | `/api/watch/check` | 追踪更新检查的确定性重跑（入参为保存的确定性检索 spec，强制 fixed/off/off/false 重跑零 LLM；返回 result_total/≤200 无序 uids/语义指纹/truncated/executed_spec/checked_at） | 否 |
| POST | `/api/artifacts/export-pack` | 追踪导出中心（入参为追踪当前状态快照（uid+状态+理由+核验时间+check_condition+provenance+导出类型），数据集元数据由服务端从本地语料解析；返回研究材料 ZIP——manifest/纳入排除表/下载任务包（复用 task_pack）/三格式引文（RIS、BibTeX、GB/T 7714-2015 [DS/OL]）/检索与核验溯源/「数据发现与筛选方法」草稿/recipe.json；kind=download_list/citations/screening_record 为单项轻量导出，full=全部；目录版本经 X-Biodata-Export-Meta 响应头回传写台账） | 否 |
| POST | `/api/agent/search-rescue` | 零命中/跑偏查询的「换词重检」救回（`search.rerun`；agent rescue 收敛面只许 search.rerun/none，机械择优闸裁定采纳；采纳回 /api/recommend 同形 payload，任何异常 200 fail-open 不落盘） | 否 |
| GET | `/api/curate-examples/pending` | 成功操作样例候选池清单（用户挑选入库；只读本分区——会话账户+端点指纹双键全等） | 否 |
| POST | `/api/curate-examples/approve` | 候选勾选迁入正式库（注入侧只读正式库；库内去重计 duplicated） | 是 |
| POST | `/api/curate-examples/dismiss` | 候选忽略（只清池不进库） | 是 |
| POST | `/api/act/summary` | 执行结果的 LLM 中文总结（只总结不执行，事实行由调用方上报，ok=False 绝不说「已」，fail-open） | 否 |
| POST | `/api/search/reply` | 检索回执的 LLM 中文改写（确定性事实句先行上屏、本端点只改写不检索，建议只许从 can_suggest 白名单挑，fail-open） | 否 |
| GET | `/spec/upload` | 返回上传规范 | 否 |
| POST | `/api/diagnose` | 用 JSON 请求体诊断 LLM 配置和网络 | 否 |
| GET | `/api/sources` | 返回来源及记录数 | 否 |
| GET | `/api/datasets` | 返回全库浏览数据与分面（可选 `limit`/`offset` 只截 records 当前页，count/facets 恒按全库算；默认整库响应缓存已序列化 bytes，带弱 ETag/`Cache-Control: no-cache`，客户端支持 gzip 时压缩传输） | 否 |
| GET | `/api/introduction` | 返回指定数据集的结构化介绍 | 否 |
| GET | `/api/fair` | 返回指定数据集的 FAIR 元数据自检 | 否 |
| POST | `/api/reuse-pack` | 选中数据集→复用出处清单（英文段落+清单+RIS/BibTeX） | 否 |
| GET | `/api/citations/download` | 把环内 `cite.export` 落盘在 `.userdata/citations/` 的引文文件发回浏览器（additive——入参 `f` 只接受裸文件名，`/`/`\`/`..` 拒绝，resolve 后必须仍在 `.userdata/citations/` 内，不存在 404；Content-Disposition attachment） | 否 |
| GET | `/api/compatible` | 返回与指定数据集元数据兼容的其它数据集 | 否 |
| GET | `/api/files` | 返回指定数据集的文件清单 | 否 |
| POST | `/api/download/plan` | 服务端真下载第一步：uids → 可下载清单（不落盘、零网络） | 否 |
| POST | `/api/download/start` | 真下载第二步：磁盘预检 → 建目录 → 起后台线程真下载（逐数据集子文件夹 + README/manifest） | 是（写本机 Downloads） |
| GET | `/api/download/status` | 真下载任务状态轮询（逐文件进度与校验结果） | 否 |
| POST | `/api/download/cancel` | 取消真下载（.part 保留可续传） | 否 |
| GET | `/api/guide/agent-prompt` | 返回 MCP 本地接入提示词全文（text/markdown；复制给自家 agent 代完成接入；只读资源经 RESOURCE_ROOT 解析、同源闸、ASCII 文件名） | 否 |
| GET | `/api/guide/online-prompt` | 在线 MCP 接入提示词模板（text/markdown；含 `__BIODATA_MCP_URL__`/`__BIODATA_MCP_TOKEN__` 占位符，前端铸币成功后代入真值再复制给用户） | 否 |
| GET | `/api/guide/skill.zip` | 随包 skill 目录现场打成 zip 附件返回（内存构建零落盘、确定性构建、`X-SHA256` 摘要头） | 否 |

最小推荐请求：

```powershell
$Body = @{
    query = '近三年 CELLxGENE 中的人类肝脏单细胞数据'
    use_llm = $false
    strategy = 'auto'
    auto_parse_sources = $true
    top_k = 5
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri 'http://127.0.0.1:7860/api/recommend' `
    -ContentType 'application/json; charset=utf-8' `
    -Body $Body
```

`/api/diagnose` 从 `1.2.0` 起只接受 POST JSON；GET 会返回 405。最小诊断请求示例：

```powershell
$DiagnoseBody = @{
    provider = 'openai-compatible'
    use_llm = $true
    base_url = 'https://api.example.com/v1'
    model = 'example-model'
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri 'http://127.0.0.1:7860/api/diagnose' `
    -ContentType 'application/json; charset=utf-8' `
    -Body $DiagnoseBody
```

所有 HTTP 请求都只接受 loopback Host；解释、推荐、上传、诊断四个浏览器 POST 携带 `Origin` 时还必须同源。不带 `Origin` 的本机 CLI/MCP 客户端保持兼容，但同样不能伪造非 loopback Host。该入口约束阻断“恶意域名重绑定到 127.0.0.1 后让 Host/Origin 同为恶意域名”的入站绕过。自定义公网端点只允许 HTTPS；明文 HTTP 只允许显式 loopback（`localhost`、`127.0.0.1`、`::1`）。端点不得含凭据、查询串或 fragment；私有、链路本地、保留、多播和未指定的字面 IP，以及常见内部域名后缀会被拒绝。出站 endpoint 域名这里只做语法校验、不会预解析 DNS，因此公开部署还必须在网络层限制出站目标并防范出站 DNS rebinding。调用方指定的自定义端点不会继承服务端共享 Key；如需临时 Key，应只放在本次 JSON 请求的 `api_key` 字段，不写入源码、日志或命令历史。服务端禁止 LLM HTTP 重定向，把供应商成功响应限制为 8 MiB，并对外清理/截断错误文本；mock 和 CI offline 诊断不会触发 DNS/TCP/HTTP。但这些校验不能把当前本地服务变成可安全公开的多用户服务，边界见 [SECURITY.md](SECURITY.md)。

集成时应以 `/docs` 中的当前 schema 为准。前端会读取 `interpretation` 和 `search_trace` 显示实际解析条件、执行步骤和回退信息；不要根据请求参数推测某个增强排序一定执行成功。

## 7. MCP 接口

MCP 服务提供 19 个工具（与网页端能力对齐；16 个只读 + 3 个写盘：`upload_dataset` 写外部库、`provision_dataset` 写调用方指定目录、`curate_datasets` 管护外部库 upload_* 与回收站）：

| 工具 | 用途 |
|---|---|
| `recommend_datasets` | 解析查询并返回推荐结果（可选 `facet_filters` / `suppressed_constraints` / `date_from` / `date_to`，对齐网页「数据细化 / 忽略已命中 / 发表时间范围」） |
| `get_file_manifest` | 按 `dataset_uid` 获取文件清单 |
| `parse_constraints` | 只解析查询约束，不执行推荐 |
| `browse_datasets` | 全库浏览：所有来源并列 + 物种/平台/来源/年份分面 + 分页（对齐 `/api/datasets`） |
| `get_dataset_introduction` | 按 `uid` / `url` / `name` 返回单数据集确定性介绍（对齐 `/api/introduction`） |
| `assess_dataset_fair` | 按 `uid` / `url` / `name` 返回单数据集 FAIR 元数据自检 + 投稿数据可用性说明（DAS，对齐 `/api/fair`；确定性、离线） |
| `lookup_identifier` | 标识符精确反查：UUID / E-XXXX-N / DOI 直达本目录记录；GEO/SRA 号如实告知不在本目录、指向原库（`/api/recommend` 的 identifier_lookup 同源） |
| `find_compatible_datasets` | 给一数据集找元数据兼容（同物种 + 兼容 chemistry/platform）的其它数据集，**始终附兼容≠可整合 caveat**（对齐 `/api/compatible`） |
| `assess_feasibility` | 研究问题 → 可行性概览（候选数 / 总细胞量下限 / 分布 / 缺口；对齐 `/api/feasibility`） |
| `plan_query_edit` | 接着改条件：一句话 → 一次具体改动（只规划、不检索；对齐 `/api/board/plan`） |
| `plan_action` | 一句话要做什么：归一化成封闭动词表里的一个动作（只出计划、不执行；`quoted` 保证是原话字面子串；对齐 `/api/action/plan`） |
| `build_task_pack` | 一句话任务包：一次检索 → 结果清单 + 下载脚本 + FAIR 自检 + 引文（先预览、确认后再产包）（对齐 `/api/task-pack/*`） |
| `build_reuse_pack` | 选中数据集 → 复用出处清单（英文出处段 + 数据集清单 + 待核实项 + RIS/BibTeX；对齐 `/api/reuse-pack`） |
| `verify_local_assets` | 扫本地目录树，按 md5 与本目录文件清单比对成实验室资产台账（**只读、纯校验和、不联网、不改文件**；对齐 CLI `scan_lab_assets.py`） |
| `provision_dataset` | **按需真下载**到调用方指定的 `dest_dir`（download_executor 单一真源：https+白名单、md5/大小核对、`.corrupt` 留证据；**写工具 ②**，只在给定 dest_dir 时写盘、fail-closed 绝不写 `database/`；默认 `scope=primary` 只下主文件，`max_files` 默认 50 硬上限 500 超限报错，`dry_run` 预演不写；对齐 CLI `provision_dataset.py`） |
| `curate_datasets` | **对话式数据库管护**（写工具 ③；对齐 `/api/curate/*` 与 CLI `curate_datasets.py`，共用 `corpus_curation.run_curate_action` 单一真源）：action=list/import/search_online/remove/restore；plan 默认 `dry_run=true` 零写盘，apply 回传 `confirm_token` 才写盘/联网、token 不符零写入；remove 是回收站式可逆移动 |
| `upload_dataset` | **上传数据集 JSON**（`records` 结构化数组/对象 **或** `path` 本地 .json 二选一）进 `database/external/`（对齐 `/api/upload`；**写工具 ①**，只落外部库、绝不碰冻结基准 `database/base/`，摄取核心与 Web 共用 `uploads.ingest_dataset`） |
| `biodata_status` | 返回语料、下载索引和服务状态 |
| `biodata_llm_status` | 返回脱敏的 LLM 配置与可选连通性状态 |

直接运行 `mcp_server.py` 会进入 stdio 协议循环，不会显示普通交互提示。开发和排错时优先使用：

```powershell
& $McpPython src\dataset_recommender\app\mcp_server.py --selfcheck
```

涉及真实 LLM 配置时，可在明确允许联网的测试环境使用 `--llm-check`。输出只应保留脱敏状态和错误码。

## 8. 前端开发

前端位于 `web/static/`，全部是原生 ES Module（`<script type="module">` + 两页头部单一 importmap），跨模块引用一律 `import { … } from "#xxx"`，无共享全局。当前不变量是：

1. `#xxx` specifier 在两页 importmap 与根 `package.json` 的 `imports` 里同键登记。
2. import 的名字必须在目标模块的 `export` 里真实存在（写错即加载期 SyntaxError）。
3. 可变共享状态只许属主模块写，他人经 `setXxx` setter。
4. 模块顶层（求值期）不触碰别模块的绑定；import 环上的绑定只在函数体内使用。
5. `boot.js` 是唯一入口（DOMContentLoaded 起 `init`）；新增模块须同键登记并由既有模块 import，同时加入 Web smoke 检查。

修改 `/api/recommend` 字段时，应同时搜索所有前端消费者：

```powershell
Select-String -Path web\static\js\*.js -Pattern '字段名'
```

Web smoke 主要检查资源和接口契约，不能代替真实浏览器交互测试。布局、教程、设置和动态状态有变化时，应在浏览器中验证桌面和窄屏状态，并检查控制台错误。

## 9. 常见扩展方式

### 增加数据来源

如果来源数据已经整理成统一 JSON，优先放入 `database/external/`，不要修改基础库。格式见[上传规范](使用教程/数据集上传/数据集上传规范.md)。新增专用采集或转换逻辑时，应保留来源标记，并测试单个坏文件不会阻断其他来源。

### 增加查询别名

物种、组织、疾病、平台和实验技术别名集中在 `domains/biodata/vocabulary.py`（biodata 领域包词表；查询词法在 `retrieval/query_lexicon.py`）。新增别名时应同时添加解析与否定语义测试，避免短词造成子串误命中。

### 修改排序策略

硬过滤位于 `retriever.py`，自动策略位于 `strategy.py`，本地语义排序位于 `vector_recall.py`，AI 重排位于 `rerank.py`。排序层不得绕过硬条件过滤，失败路径必须回退到已有候选顺序。

### 修改公共接口

修改 HTTP、MCP、CLI 或数据字段时，应在同一变更中更新生产者、所有消费者、测试和说明文档。优先增加兼容字段；字段改名或删除需要明确迁移方案。

## 10. 测试与验收

统一入口由 `automation/quality-gates.json` 定义，开发者和 CI 使用同一个 runner。先查看或解析计划：

```powershell
& $Python scripts\quality_gate.py --list
& $Python scripts\quality_gate.py --profile full --dry-run
```

日常快速检查与交付前权威检查：

```powershell
& $Python scripts\quality_gate.py --profile fast --report-json artifacts\quality-fast-local.json
& $Python scripts\quality_gate.py --profile full --report-json artifacts\quality-full-local.json
```

`fast` 包含 Python/JavaScript 语法和自动化契约测试；`full` 还包含依赖一致性、全量 pytest、项目/Web/MCP smoke 与冻结推荐评测。runner 不安装依赖，会清除密钥和 Python/Node/代理注入变量、禁用模型下载，并用离线环境与网络 tripwire 约束清单中的受审查命令；它不是操作系统级网络沙箱。缺工具、超时或命令失败都会终止并返回非零。`full` 环境必须已用 `--require-hashes --only-binary=:all:` 安装 `requirements/requirements-ci.lock`，并能找到 Node.js 与 PowerShell。完整说明见[自动化质量门与候选发布](docs/AUTOMATION_AND_RELEASE.md)。

需要定位单项失败时，可直接运行组成门：

```powershell
& $Python -m pytest tests\ -q
& $Python scripts\smoke_test.py
& $Python scripts\web_smoke_test.py
& $McpPython src\dataset_recommender\app\mcp_server.py --selfcheck
```

推荐或数据逻辑变化还需运行：

```powershell
& $Python scripts\evaluate_recommendation.py
```

本地语义排序变化可运行相应评测：

```powershell
& $Python scripts\evaluate_recall.py
```

LLM 重排评测会产生非确定性结果，只在已配置测试凭据并允许联网时执行：

```powershell
& $Python scripts\evaluate_rerank.py
```

测试失败时不要修改评测数据或降低阈值来消除失败。先区分环境缺失、既有问题和本次回归，再修复实际原因。

仓库内 CI 工作流使用 Windows 3.12 的 `full` 和 Ubuntu 3.10/3.14 的 `fast`，但工作流文件存在不构成 GitHub 运行证据。把 `gate` 配为受保护分支 required check、检查真实 Actions 记录和部署目标配置，都属于远程仓库侧操作。

## 11. 交付检查

对外提交前至少确认：

- README、开发指南、MCP 教程和上传规范中的命令能在目标系统执行。
- 没有 `.env`、真实 Key、个人绝对路径、浏览器配置、代理信息或本地缓存。
- 没有 `.git`、`.venv`、`__pycache__`、`.pytest_cache`、模型权重和临时测试输出。
- 提交包中的相对链接都指向包内文件。
- ZIP 重新解压后仍能运行测试和启动入口。
- 数据来源、记录数和功能说明与当前提交包一致。

候选包使用 `scripts\build_release.py` 的 allowlist 构建并逐文件写入 SHA-256 manifest；解包 smoke 必须在仓库外的全新临时目录执行。当前工作流只生成候选 ZIP 与验证证据，不创建 GitHub Release、也不部署生产环境；构建、验证和回滚证据步骤见[自动化质量门与候选发布](docs/AUTOMATION_AND_RELEASE.md)。

若维护同源的 private/public 两仓，public 必须由 `scripts/build_public_mirror.py` 确定性生成，不得人工清洗。构建器会要求每个 tracked path 在 `packaging/public-mirror/files.txt` 或 `.deliveryignore` 中明确分类，并对公开树做精确路径与字节比较。完整操作合同见[确定性镜像](docs/AUTOMATION_AND_RELEASE.md#11-私库到公开仓的确定性镜像)。

提交包不应包含个人汇报、开发过程记录或未发布材料。该规定由机制保证，不靠人工记忆：

- 交付前运行 `<python> scripts/make_delivery.py --check`：交付集与 git 已跟踪集对账后，对**留在包里**的
  文本文件复核敏感词（真名 / 内部专名 / 本机个人绝对路径），命中即拒绝、逐条报告 `file:line`。
- 复核通过后用 `--out <zip>` 打包，或 `--list` 人工核对纳入/排除清单。持续护栏见 `tests/test_delivery_safety.py`。
- `scripts/build_release.py` 组装候选后同样复用 `make_delivery` 的敏感词扫描做 fail-closed 兜底（内部 `scan_forbidden` 调用），两套机制共用同一份禁词表，杜绝「allowlist 漏网、内部专名进候选包」。

### 《使用说明书》的界面图

`docs/使用说明书/图/*.png` 是交付物的一部分，界面一改就会过期。研发主仓里有 Playwright 自动重拍脚本（驱系统 Edge，前置状态写死、可复现取景），该脚本属内部研发工具、未随本公共仓发布；在此仓中如需更新插图，请手工截图并保持取景口径一致。
图号连续、图片文件存在、每张都有 `alt`，由 `tests/test_release_version_contract.py` 的插图门守着。
