"""版本 / 工具数的**跨文件单一真源**门。

2026-07-17 全盘审计的一批文档漂移，根因都在这里：这个门原本只钉 `webapp.py` ↔ `launch_web.ps1`
两处，**没钉面向客户的文档**。于是代码一路走到 1.4.0，而 README 与 DEVELOPMENT 还写着「当前 Web API
版本为 1.2.0」；MCP 工具从 5 个长到 9 个，README 却还列着 5 个、并明确重申「MCP 工具数仍为 5」——
**唯一会写盘的工具 `upload_dataset` 对客户完全不可见**。

所以这里补的不是「把数字改对」（改一次、下次接着漂），而是**把文档纳入同一个门**：
数字从代码里读，文档必须与之相符。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _web_api_version() -> str:
    webapp = (ROOT / "src" / "dataset_recommender" / "app" / "webapp.py").read_text(encoding="utf-8")
    m = re.search(r'^WEB_API_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', webapp, re.MULTILINE)
    assert m, "webapp.py 里找不到 WEB_API_VERSION"
    return m.group(1)


def _expected_tools() -> list[str]:
    src = (ROOT / "src" / "dataset_recommender" / "app" / "mcp_server.py").read_text(encoding="utf-8")
    m = re.search(r"^_EXPECTED_TOOLS = \(\n(.*?)^\)$", src, re.MULTILINE | re.DOTALL)
    assert m, "mcp_server.py 里找不到 _EXPECTED_TOOLS"
    return re.findall(r'"([a-z_]+)"', m.group(1))


def test_launcher_expected_version_matches_web_health_version() -> None:
    webapp = (ROOT / "src" / "dataset_recommender" / "app" / "webapp.py").read_text(encoding="utf-8")
    ps_launcher = (ROOT / "scripts" / "launch_web.ps1").read_text(encoding="utf-8-sig")
    sh_launcher = (ROOT / "scripts" / "launch_web.sh").read_text(encoding="utf-8")
    web_match = re.search(r'^WEB_API_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', webapp, re.MULTILINE)
    ps_match = re.search(r"^\$ExpectedVersion = '([0-9]+\.[0-9]+\.[0-9]+)'$", ps_launcher, re.MULTILINE)
    sh_match = re.search(r"^EXPECTED_VERSION_FALLBACK='([0-9]+\.[0-9]+\.[0-9]+)'$", sh_launcher, re.MULTILINE)
    assert web_match and ps_match and sh_match
    # 2.1.0：`results[]` additive 新增 `gene_count`（10x 平台信息补充旁挂表；无补充为 ""），
    #        卡片与介绍页关键事实同步展示「检测基因数」。
    # 2.0.0：`/api/utterance` 重写为 turn pipeline（breaking：响应 {route,query,plan,echo_zh,retrieval,via}，
    #        route ∈ search/tool/none；规则匹配一切指令都过、零命中≠无效，LLM 分流带原话+命中概览+
    #        当前条件；检索动词携带 effective_query）。入参裁掉条件板上下文字段（refine 改由 LLM 分流）。
    # 1.9.0：新增 `/api/act/summary`（执行结果的 LLM 中文总结；只总结不执行，fail-open，
    #        护栏写进 user prompt——ok=False 绝不说「已」；LLM 配置链与 /api/action/plan 同径）。
    # 1.8.0：新增 `/api/curate/plan` + `/api/curate/apply`（对话式数据库管护两步确认；
    #        与 MCP curate_datasets / CLI curate_datasets.py 共用 corpus_curation 单一真源）。
    # 1.7.0：`search_trace.steps[]` additive 新增 `fallback_note`——某一层回退时「该怎么对用户说」由后端出话，
    #        前端不再自己写死措辞（此前一律说成「本次未启用」，把故障说成了选择）。
    # 1.6.0：新增 `/api/action/plan`（一句话执行层，只出计划不执行）。
    # 2.2.0：新增 `/api/curate/sync-updates`（2026-08-06 `curate.sync_updates`：
    #        检查更新→有新增则自动入库的复合流；无 token——原子调用无信任边界，回收站可撤）。
    # 2.3.0：`/api/utterance` 入参 additive 新增 `req_id`（2026-08-08：断流重发幂等——
    #        服务端按 req_id 占用，同号在途等 owner 收尾回缓存体，不再二次执行写工具；
    #        无 req_id 行为逐位不变；响应体形状零变化）。
    # 2.4.0：新增 `/api/curate-examples/pending` + `/approve` + `/dismiss`（2026-08-13 操作样例库
    #        改用户挑选入库：机械收录只进候选池，勾选迁入正式库，注入侧只读正式库）。
    # 2.5.0：新增 4 端点：
    #        `/api/curate/sync-status`（实例级同步状态）+ `/api/curate/recall`
    #        （按 operation_id 整次撤回一次 sync）+ `/api/watch/check`（课题更新检查的确定性
    #        重跑，≤200 无序 uid+语义指纹+truncated）+ `/api/artifacts/export-pack`（课题研究
    #        材料 ZIP 导出：manifest/纳入排除表/下载任务包/三格式引文/溯源/方法草稿/recipe）。
    # 2.6.0：新增 `/api/download/update`（2026-08-24 在途增删：对运行中的下载队列做
    #        add/remove——remove 排队=跳过、remove 正在下载=中止并清理未完成部分后继续下一条、
    #        remove 已完成=如实拒绝、add=追加进队列尾部；additive）。
    # 2.8.0：新增 `/api/account/mcp-token`（铸币）+ `/api/account/mcp-tokens`（列表）+
    #        `/api/account/mcp-token/revoke`（吊销）（2026-08-28：网页版护栏形态
    #        下用户可铸 Bearer 令牌，把 `/mcp` 端点直接配进任意 MCP 客户端，免装本地包；
    #        令牌落盘只存 sha256 摘要；additive）。
    # 2.7.0：新增 `/api/admin/corpus-sync` + `/api/admin/corpus-sync/status` + `/api/curate/sync-updates/status`
    #        （2026-08-26：进程内单飞语料同步 job，admin 双闸端点 + 用户侧异步
    #        状态查询；`/api/curate/sync-updates` guard on 改 202 异步、guard off 逐字节不变；additive）。
    # 2.9.0：新增 `/api/search/reply`（2026-08-30：检索回执 LLM 原位改写——纯检索轮的
    #        确定性事实句先上屏、LLM 成功才替换并挂「AI 总结」标，fail-open 留事实句；
    #        混合轮由 actPending 抑制模板回执、检索事实并入执行总结，全轮单泡；additive）。
    # 3.0.0：Wave 3 行为合同收口——前端 plan-only 动词不再绕过 Agent 图；
    #        rescue 成为第四套件；退役 rerank_audit/degrade_with_llm/action_audit
    #        三条环外 LLM 通道及串行 RAG 副本。响应/请求面因而 breaking。
    assert web_match.group(1) == ps_match.group(1) == sh_match.group(1) == "3.0.0"
    assert "src\\dataset_recommender\\app\\webapp.py" in ps_launcher
    assert "src/dataset_recommender/app/webapp.py" in sh_launcher
    assert "src\\dataset_recommender\\webapp.py" not in ps_launcher
    builder = (ROOT / "scripts" / "build_release.py").read_text(encoding="utf-8")
    assert '"product_version": resolved_product_version' in builder
    assert '"--expected-version"' in builder


def test_customer_docs_state_the_real_web_api_version() -> None:
    """README / DEVELOPMENT 里「当前 Web API 版本为 X」必须等于代码里的 WEB_API_VERSION。

    只匹配「**当前**版本」这种断言句；`/api/diagnose` 从 `1.2.0` 起只接受 POST 那类**历史陈述**
    刻意不在匹配范围内（它讲的是某个变更发生在哪个版本，与当前版本无关）。
    """
    real = _web_api_version()
    for rel in ("README.md", "DEVELOPMENT.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        claims = re.findall(r"当前 Web API 版本为 `([0-9]+\.[0-9]+\.[0-9]+)`", text)
        assert claims, f"{rel} 里找不到「当前 Web API 版本为 …」的断言句"
        for c in claims:
            assert c == real, f"{rel} 声称当前版本 {c}，代码是 {real}"


def _mcp_server_version() -> str:
    src = (ROOT / "src" / "dataset_recommender" / "app" / "mcp_server.py").read_text(encoding="utf-8")
    m = re.search(r'^_SERVER_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', src, re.MULTILINE)
    assert m, "mcp_server.py 里找不到 _SERVER_VERSION"
    return m.group(1)


def _route_decorator_count() -> int:
    """`webapp.py` 里的路由装饰器条数（`@app.get/post/...` + `@app.api_route`）。

    刻意不 import app 去数 `app.routes`：那会把 FastAPI 自带的 `/docs`、`/openapi.json`、
    `/redoc`、`/static` 一起数进来，与文档表里「我们自己声明了哪些路由」不是同一件事。
    """
    src = (ROOT / "src" / "dataset_recommender" / "app" / "webapp.py").read_text(encoding="utf-8")
    return len(re.findall(r"^@app\.(?:get|post|put|delete|head|patch|api_route)\(", src, re.MULTILINE))


def test_developer_doc_route_table_counts_every_decorator() -> None:
    """DEVELOPMENT.md §6 自称「与 webapp.py 的路由装饰器一一对应，共 N 个」。

    2026-07-26 抓到：那句话说 24，实际 25 —— 少的是 `/dataset`（详情页独立路由，那轮加的）
    表里**整行缺失**。「一一对应」这种自我声明没有门就是一句空话。
    """
    text = (ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8")
    m = re.search(r"与 `webapp\.py` 的路由装饰器一一对应，共 (\d+) 个", text)
    assert m, "DEVELOPMENT.md §6 里找不到「与 webapp.py 的路由装饰器一一对应，共 N 个」"
    real = _route_decorator_count()
    assert int(m.group(1)) == real, f"DEVELOPMENT.md 说 {m.group(1)} 条路由，实际装饰器 {real} 条"


@pytest.mark.skipif(
    not (ROOT / "docs" / "agent" / "FRONTEND.md").exists(),
    reason="私有工程文档树公开仓不含；有它时照常校验版本一致性",
)
def test_frontend_doc_states_the_real_web_api_version_and_route_count() -> None:
    """`docs/agent/FRONTEND.md` 是**前端改动的第一读物**，它写错版本会一路带偏。
    2026-07-26 抓到它停在 `1.4.0` / 20 条路由——落后两个版本。"""
    text = (ROOT / "docs" / "agent" / "FRONTEND.md").read_text(encoding="utf-8")
    m = re.search(r"Web API 版本 `([0-9]+\.[0-9]+\.[0-9]+)`，(\d+) 条路由", text)
    assert m, "FRONTEND.md 里找不到「Web API 版本 `X`，N 条路由」"
    assert m.group(1) == _web_api_version(), f"FRONTEND.md 声称 {m.group(1)}，代码是 {_web_api_version()}"
    assert int(m.group(2)) == _route_decorator_count()


def test_acceptance_note_states_the_real_web_api_version() -> None:
    """`测试说明.txt` 是**客户拿到包之后照着做的第一件事**。

    它让客户访问 `/api/health` 确认 `version="X"`——这个数字一旦落后，客户照做会看到不一致，
    合理反应是「这个包是不是发错了」。2026-07-26 抓到它停在 1.5.0 而代码已是 1.6.0。
    """
    real = _web_api_version()
    text = (ROOT / "docs" / "测试说明.txt").read_text(encoding="utf-8")
    claims = re.findall(r'version="([0-9]+\.[0-9]+\.[0-9]+)"', text)
    assert claims, "测试说明.txt 里找不到 `version=\"…\"` 验收行"
    for c in claims:
        assert c == real, f"测试说明.txt 让客户核对 version={c}，代码是 {real}"
    for c in re.findall(r"--expected-version ([0-9]+\.[0-9]+\.[0-9]+)", text):
        assert c == real, f"测试说明.txt 的 --expected-version {c} 与代码 {real} 不符"


def test_customer_manual_states_the_real_versions_and_tool_count() -> None:
    """《使用说明书》是交付给客户的主文档，封面与末尾都自报版本与工具数。

    2026-07-26 抓到：正文已经写进了 1.6.0 才有的「说了就直接做」一节，封面却仍是
    网页 1.5.0 / MCP 1.27.0 / 16 个工具，工具表里也没有 `plan_action` 那一行。
    """
    web, mcp_ver, n = _web_api_version(), _mcp_server_version(), len(_expected_tools())
    text = (ROOT / "docs" / "使用说明书" / "使用说明书.html").read_text(encoding="utf-8")
    assert f"网页服务版本 {web}" in text, f"说明书封面没写「网页服务版本 {web}」"
    assert f"MCP服务版本 {mcp_ver}" in text, f"说明书封面没写「MCP服务版本 {mcp_ver}」"
    assert f"网页服务 {web}、AI 助手接入服务 {mcp_ver}（{n} 个工具）" in text, "说明书末尾的版本声明没对上"
    # 「除 upload_dataset、provision_dataset 与 curate_datasets 外，其余 N 个工具都不写任何东西」讲的是
    # **只读**工具数 = 总数 - 3（v1.30.0 起写盘工具有三个：upload_dataset 写外部库、provision_dataset
    # 写调用方指定目录、curate_datasets 管护 external 的 upload_* 与回收站），与其它「共 N 个工具」
    # 不是同一个数；分开钉，别把写工具声明混进去。
    assert f"其余 {n - 3} 个工具" in text, f"说明书没写「除三个写盘工具外，其余 {n - 3} 个工具…」"
    for m in re.finditer(r"(?:其余 )?(\d+) 个工具", text):
        expected = n - 3 if m.group(0).startswith("其余") else n
        assert int(m.group(1)) == expected, f"说明书里写着「{m.group(0)}」，应为 {expected} 个"
    missing = [t for t in _expected_tools() if f"<code>{t}</code>" not in text]
    assert not missing, f"说明书第 11 章的工具表漏了：{missing}（客户看不见这些能力）"


def test_customer_manual_figures_are_numbered_in_order_and_all_files_exist() -> None:
    """说明书的图是交付物，掉一张图 / 序号乱一位，客户拿到的就是残缺的 PDF。

    2026-07-26 这一轮往正文中间插图（§5.1 的「AI 没能完成」那张），后面四张的序号全要顺移——
    这类顺移是纯手工活，漏一处不会有任何东西报错。所以钉三件事：
    ① 图号从 1 连续递增；② 每个 `<img src="图/…">` 指向的文件真的在盘上；③ 每张图都有 alt。
    """
    manual = ROOT / "docs" / "使用说明书" / "使用说明书.html"
    text = manual.read_text(encoding="utf-8")

    numbers = [int(m.group(1)) for m in re.finditer(r"<figcaption>图 (\d+)　", text)]
    assert numbers, "说明书里一张带编号的插图都没有"
    assert numbers == list(range(1, len(numbers) + 1)), f"图号不连续：{numbers}"

    srcs = re.findall(r'<img src="(图/[^"]+)"', text)
    assert len(srcs) == len(numbers), f"{len(srcs)} 张图 / {len(numbers)} 条图注，对不上"
    missing = [s for s in srcs if not (manual.parent / s.replace("/", "\\")).exists()
               and not (manual.parent / s).exists()]
    assert not missing, f"说明书引用了不存在的图片：{missing}"

    no_alt = re.findall(r'<img src="图/[^"]+"(?![^>]*\balt=)[^>]*>', text)
    assert not no_alt, f"这些插图没有 alt（打印稿之外的可访问性）：{no_alt}"


def test_customer_docs_list_every_mcp_tool() -> None:
    """README 的 MCP 章节必须与 `_EXPECTED_TOOLS` 一致：数量对得上、**每个工具名都出现**。

    钉「每个名字都出现」而不只是数量：数量对但漏了 upload_dataset、多写一个不存在的，都要红。
    """
    tools = _expected_tools()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    n = re.search(r"项目提供 (\d+) 个 stdio MCP 工具", readme)
    assert n, "README 里找不到「项目提供 N 个 stdio MCP 工具」"
    assert int(n.group(1)) == len(tools), f"README 说 {n.group(1)} 个工具，实际 {len(tools)} 个"
    missing = [t for t in tools if f"`{t}`" not in readme]
    assert not missing, f"README 的 MCP 工具表漏了：{missing}（客户看不见这些能力）"


def test_selfcheck_tool_count_in_tutorial_matches_code() -> None:
    """安装教程里每一处 `SELFCHECK_OK tools=N` 都必须等于真实工具数。

    背景：v1.10.0 那次只改了 Windows 正文、漏了附录 A（macOS/Linux）——同一 commit 两处只改一处，
    于是 macOS 用户照教程验收会以为 tools=9 是异常。这条把「每一处」都纳进来。
    """
    n = len(_expected_tools())
    hits = []
    for md in (ROOT / "使用教程").rglob("*.md"):
        for m in re.finditer(r"SELFCHECK_OK tools=(\d+)", md.read_text(encoding="utf-8")):
            hits.append((md.relative_to(ROOT).as_posix(), int(m.group(1))))
    assert hits, "教程里找不到任何 SELFCHECK_OK tools=N 验收行"
    bad = [(p, v) for p, v in hits if v != n]
    assert not bad, f"教程验收行与真实工具数({n})不符：{bad}"


@pytest.mark.skipif(
    not (ROOT / "docs" / "QUALITY_GATES.md").exists(),
    reason="私有工程文档树公开仓不含；有它时照常校验工具数一致性",
)
def test_quality_gates_doc_states_the_real_tool_count() -> None:
    n = len(_expected_tools())
    text = (ROOT / "docs" / "QUALITY_GATES.md").read_text(encoding="utf-8")
    m = re.search(r"tools/list（(\d+) 个预期工具", text)
    assert m, "QUALITY_GATES.md 里找不到「tools/list（N 个预期工具」"
    assert int(m.group(1)) == n, f"QUALITY_GATES.md 说 {m.group(1)} 个，实际 {n} 个"


# ---------------------------------------------------------------- 文档工具表 ↔ _EXPECTED_TOOLS
#
# 2026-07-18 审查抓到：客户安装教程正文写「九个工具」+ 9 行表，实际 14 个——**穿过 v1.13→v1.22 五个
# 版本无人察觉**，因为唯一的守卫 test_selfcheck_tool_count_in_tutorial_matches_code 只看 `SELFCHECK_OK
# tools=N` 那一行，从不读正文和工具表。这里把「每个工具名都必须在文档里出现」纳入门：新增工具却漏改
# 这些表 → 立刻红。（数量措辞用中文数字，难以稳健正则；改钉「每个名字都在」，等价且更强。）

def _doc_missing_tools(rel_path: str) -> list[str]:
    text = (ROOT / rel_path).read_text(encoding="utf-8")
    return [t for t in _expected_tools() if t not in text]


def test_tutorial_lists_every_mcp_tool() -> None:
    missing = _doc_missing_tools("使用教程/MCP安装/MCP_安装教程.md")
    assert not missing, f"安装教程漏了 MCP 工具：{missing}（客户照它验收会看不到这些能力）"


def test_modules_doc_lists_every_mcp_tool() -> None:
    missing = _doc_missing_tools("MODULES.md")
    assert not missing, f"MODULES.md（权威契约文档）漏了 MCP 工具：{missing}"


def test_developer_doc_lists_every_mcp_tool() -> None:
    missing = _doc_missing_tools("DEVELOPMENT.md")
    assert not missing, f"DEVELOPMENT.md §7 MCP 工具表漏了：{missing}"


def test_all_first_party_static_assets_share_one_cache_generation() -> None:
    html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    versions = re.findall(r'/static/(?:css|js)/[^"?]+\?v=([a-zA-Z0-9-]+)', html)
    assert len(versions) == 89   # 2026-08-30 dl-browser-queue 实测 89（新增 #downloads importmap 条目；构成 = 1 css + script 标签 + importmap 条目，随模块增删以实测为准。历史： 88 / 87 / eng1 86 / b1 60）
    assert set(versions) == {CACHE_GENERATION}


def test_dataset_page_loads_the_usage_layer_it_actually_calls() -> None:
    """`dataset.html` 必须加载 usage_core.js + usage_log.js。

    2026-07-29 真机抓到：详情页只加载 core.js / cards.js / fav_folders.js / dataset_page.js，
    但 `core.js` 的 `toggleFav` 与 `cards.js` 的 `buildCard` 都**直接**调 `usageLog` / `USAGE_KINDS`
    （本仓库刻意不给跨模块调用加 `typeof` 守卫，见 FRONTEND.md §4.3）——于是在详情页点心形收藏
    抛 ReferenceError：收藏其实已写进 localStorage，但心形不变实心、不弹 toast、popover 不关，
    在用户看来就是「点了没反应」。首页三门全绿，因为它们都只读 index.html。
    """
    html = (ROOT / "web" / "static" / "dataset.html").read_text(encoding="utf-8")
    for js in ("core/usage_core.js", "core/usage_log.js"):
        assert f"/static/js/{js}?v=" in html, f"dataset.html 没加载 {js}，该页调 usageLog 会 ReferenceError"
    # 反向钉：这两个文件真的定义了详情页用到的那两个名字（改名就红，而不是运行时才炸）
    core_js = (ROOT / "web" / "static" / "js" / "core" / "usage_core.js").read_text(encoding="utf-8")
    log_js = (ROOT / "web" / "static" / "js" / "core" / "usage_log.js").read_text(encoding="utf-8")
    assert "USAGE_KINDS" in core_js and "function usageLog(" in log_js


def test_dataset_page_shares_the_same_cache_generation() -> None:
    """独立详情页 `dataset.html` 必须与首页**同一代**令牌。

    2026-07-21 实测漂移：首页已 bump 到 `20260720-ux4`，`dataset.html` 还停在 `20260720-ux3g`——
    上面那条只读 index.html，从不看详情页，所以三门全绿。两页**共用** core.js / cards.js /
    fav_folders.js：令牌不同代 = 详情页用户继续吃旧缓存，且首页新 JS 与详情页旧 JS 混用，
    正是本文件下方注释里那起「混合缓存 ReferenceError」事故的同一形态。
    """
    html = (ROOT / "web" / "static" / "dataset.html").read_text(encoding="utf-8")
    versions = re.findall(r'/static/(?:css|js)/[^"?]+\?v=([a-zA-Z0-9-]+)', html)
    assert versions, "dataset.html 里找不到任何带 `?v=` 的一方静态资源引用"
    assert set(versions) == {CACHE_GENERATION}, (
        f"dataset.html 的缓存令牌 {sorted(set(versions))} 与首页 {CACHE_GENERATION} 不同代"
    )


def test_every_first_party_asset_reference_carries_a_token() -> None:
    """两页里所有一方 css/js 引用都必须带 `?v=`——漏一个，那个文件就永远吃旧缓存。"""
    for rel in ("web/static/index.html", "web/static/dataset.html"):
        html = (ROOT / rel).read_text(encoding="utf-8")
        naked = re.findall(r'(?:src|href)="(/static/(?:css|js)/[^"?]+)"', html)
        assert not naked, f"{rel} 里这些一方资源引用没带缓存令牌：{naked}"


# ---------------------------------------------------------------- 静态资源 ↔ 缓存令牌
#
# **改了 web/static/{js,css} 的内容，就必须 bump index.html 的 `?v=` 令牌。**
#
# 为什么需要一道「内容指纹」门（2026-07-17 对抗评审抓到的真事故）：本项目原有两道令牌守卫 ——
# `test_onboarding_contract.py` 查「每个引用都带令牌」、上面那条查「所有令牌同一代」——
# 但它们都是**一致性**检查，**不是变更检测**：整轮不 bump，两道全绿。
# 于是那一轮改了 4 个 JS、令牌一动不动就差点合并，后果是：
#   1. `webapp.py` 用裸 `StaticFiles` mount、不发 Cache-Control（实测只有 etag/last-modified）
#      → 浏览器走**启发式**新鲜度 → 回访用户 URL 逐字节相同 → 直接用旧 JS，**修复等于没发布**；
#   2. 更糟：浏览器按**文件**独立淘汰缓存。新 `search.js` + 旧 `progress.js` 的混合缓存下，
#      `search.js` 会调到尚不存在的 `resetSubmitButton` → `ReferenceError`，且该调用在 try 之外
#      → unhandled rejection → 按钮照样卡死。**一个专治按钮卡死的修复反而制造按钮卡死。**
# 开发日志记过两次同类事故（一次 app.css、一次 tabindex，均为「真交付 bug」）。
#
# 这道门用与冻结基准 `FROZEN_BASE_SHA256` 相同的范式：把内容指纹钉成常量。改静态资源 → 指纹变 →
# **本条立刻红**，报错信息直接告诉你「bump 令牌 + 同步这两个常量」。指纹按行尾归一（`\r\n`/`\r` → `\n`）
# 后计算，故对 LF/CRLF checkout 差异免疫。

CACHE_GENERATION = "20260923-domainpack2"  # 骨肉分离批工艺轮（无用导入清理）；承接 domainpack1。
STATIC_ASSETS_SHA256 = "619d3d32230ee62d7e0d2011f4c337d1fb5e903cf9c379466f5a3a4e0993c53d"


def _static_assets_digest() -> str:
    """web/static/{css,js} 全部一方资源的内容指纹（行尾归一后按路径排序哈希）。"""
    import hashlib

    h = hashlib.sha256()
    base = ROOT / "web" / "static"
    files = sorted(
        (p for d in ("css", "js") for p in (base / d).rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(base).as_posix(),
    )
    for p in files:
        h.update(p.relative_to(base).as_posix().encode("utf-8"))
        h.update(b"\0")
        body = p.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        h.update(body)
        h.update(b"\0")
    return h.hexdigest()


def test_static_asset_changes_force_a_cache_token_bump() -> None:
    """静态资源内容一变 → 本条红 → 逼你 bump 令牌。这是「变更检测」，上面那条只是「一致性」。"""
    actual = _static_assets_digest()
    assert actual == STATIC_ASSETS_SHA256, (
        "web/static/{css,js} 的内容变了。这意味着你**必须**同时：\n"
        f"  1. 把 web/static/index.html 里全部 `?v=` 令牌从 {CACHE_GENERATION} 换成新代号；\n"
        "  2. 把本文件的 CACHE_GENERATION 改成同一个新代号；\n"
        f"  3. 把 STATIC_ASSETS_SHA256 改成 {actual}\n"
        "不 bump 令牌的后果：回访用户的浏览器按启发式缓存继续跑旧 JS（webapp 不发 Cache-Control），"
        "你的前端修复到不了他们；且新旧 JS 混合缓存会抛 ReferenceError。"
    )
