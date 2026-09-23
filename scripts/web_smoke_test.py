from __future__ import annotations

import io
import json
import re
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def fail(message: str) -> None:
    print(f"WEB SMOKE TEST FAILED: {message}")
    raise SystemExit(1)


def assert_no_mojibake(text: str, field: str) -> None:
    for token in ("æ", "ç", "â", "å", "�"):
        if token in text:
            fail(f"{field} contains mojibake token `{token}`")


def main() -> int:
    from dataset_recommender.app.webapp import app  # delayed import

    client = TestClient(app, base_url="http://127.0.0.1")

    # 1) 首页与关键区块（新版多视图）
    home = client.get("/")
    if home.status_code != 200:
        fail(f"/ returned {home.status_code}")
    html = home.text
    # 前端已从单文件拆成 index.html（结构）+ /static/js/*.js（逻辑模块，可并行维护）。
    # 关键 token（API 端点 / fetch 等）现分散在 JS 模块里 → 针对「整份前端 bundle」校验；
    # 「用户可见术语」类 forbidden 仍只查 index.html（JS 注释是开发者向、允许含内部术语）。
    # 模块清单的单一真源 = index.html 的 <script> 标签列表：自动解析、不再手工登记
    # （旧手写清单曾漏掉 reuse_pack/accounts——这两个文件 404 时 smoke 依然全绿）。
    _JS_MODULES = re.findall(r'<script(?:\s+type="module")?\s+src="/static/js/([A-Za-z0-9_/-]+)\.js(?:\?[^"]*)?"', html)
    if len(_JS_MODULES) < 10:
        fail(f"parsed too few /static/js modules from index.html: {_JS_MODULES}")
    # 2026-08-08 起 js 入子文件夹：捕获段带子目录前缀（fetch 直接用全段；加载序断言也带）
    if _JS_MODULES[0] != "core/core" or _JS_MODULES[-1] != "core/boot":
        fail(f"load-order contract violated: core must be first, boot last, got {_JS_MODULES}")
    js_parts = []
    for _m in _JS_MODULES:
        _r = client.get(f"/static/js/{_m}.js")
        if _r.status_code != 200:
            fail(f"/static/js/{_m}.js not served (status {_r.status_code})")
        js_parts.append(_r.text)
    if client.get("/static/css/app.css").status_code != 200:
        fail("/static/css/app.css not served")
    bundle = html + "\n".join(js_parts)
    required_home = [
        "BioData Agent",
        "智能查询",             # nav
        "数据集浏览",           # nav / 浏览页
        "上传后即可检索",       # 上传（C32：上传动作动词统一「上传」，入库提示改「已加入 N 条」）
        "我的库",               # 我的库（导航：追踪+收藏双页签浮窗）
        "历史记录",             # 历史记录（导航：独立浮窗）
        "帮助 / 关于",          # 帮助
        'id="libWin"',          # 我的库浮窗（追踪+收藏双页签；骨架属主 shell.js initLibWin）
        'id="histWin"',         # 历史记录独立浮窗（骨架属主 shell.js initHistWinSkeleton）
        'id="cfgPolish"',       # AI 润色开关（独立开关，API 门控）
        'id="cfgRerank"',       # AI 重排开关（维度 A 三并列之一，API 门控）
        'id="cfgRecall"',       # 本地精准重排开关（维度 A，本地语义、默认关、优雅降级）
        'id="cfgAgentExec"',    # AI 执行开关（维度 C，2026-08-03 合并旧「说了就直接做」+「Agent 规划执行」）
        'id="cfgStrategy"',     # 自动选择排序策略（维度 B，开则隐藏 A 的手动项）
        'id="cfgRerankTopN"',   # 参与排序的候选数量（原「候选池」rerank_top_n）
        'id="dateFrom"',        # 发表时间范围筛选（「时间维度」）
        'id="dateTo"',
        'id="sourceChips"',     # 智能查询：按数据来源自由勾选
        'id="fSource"',         # 数据集浏览：来源筛选（所有库并列）
        'id="searchTrace"',     # 结果后面向用户的实际检索步骤
        'id="condBoard"',       # 条件板：出结果后列出实际在用的筛选条件，可点可改
        "/api/board/plan",      # 条件板的规划端点（只规划、不检索）
        "/api/utterance",       # 统一对话窗口的后端混合路由（只路由、不执行）
        "ubSubmit",             # 统一对话窗口：唯一输入框 #queryInput 的提交入口（#cbInput 已退役）
        'id="taskPackBtn"',     # 一句话任务包入口：结果清单 + 下载脚本 + FAIR + 引文
        'id="taskPackPanel"',
        "/api/task-pack/preview",   # 先看清单
        "/api/task-pack/build",     # 确认后才产文件
        "/api/curate/plan",         # 管护预览（零写盘，search_online 的 plan 会联网并记账本）
        "/api/curate/apply",        # 回传 confirm_token 才真执行（前端 runner 链式直推，问卷已退役）
        "执行明细不可用（请重启后端）",  # 新前端遇到旧后端时不得猜成规则检索
        "规则+本地精准重排", #覆盖策略修复：非活动备选批的排序层标注（results.js _batchRankSuffix）
        'id="onboarding"',      # 首次进入轻量导览
        'id="tutorialReplay"',  # 帮助页可重放
        'id="onboardingProgress">1 / 14<',  # 14 步教程：第0步反馈承诺+高质量查询引导 + 第2屏「一句话交代整件事」能力心智 + 真实 API 配置表单 + 排序说明（规则开箱即用/两种增强随时补）+ 润色/细化/详情页实拍/条件板 + 我的库介绍 + 接进你自己的 AI 助手 + 使用反馈指路
        'id="nodeUsage"',                  # 设置里的使用反馈开关（默认态按版本分叉：benchfb 构建开/主线关，2026-08-13 起）
        'data-onboarding-visual="ranking"',
        'data-onboarding-visual="agent"',  # 教程「接进你自己的 AI 助手」视觉块（后移至结尾、文案精简）
        "AI 润色只改说明，不改结果",
        "用细化筛选收窄，再查看数据集介绍",
        "/api/sources",         # 可选来源清单端点
        "AI 接入方式",           # 主流品牌预设按组展示，同时保留兼容/本地入口
        "DeepSeek",
        "Kimi",
        "Qwen（通义千问）",
        "GLM（智谱）",
        "OpenRouter（多模型聚合）",
        "OpenAI",
        "兼容接口",              # 仍可手工接入未单列的 OpenAI 兼容服务
        "选择主流服务商后会自动填入接口地址和推荐模型",
        "接口地址",              # OpenAI 兼容 base_url 输入（用户界面弱化内部字段名）
        "模型名称",              # 任意兼容模型名输入
        "查看全部",              # 卡片「查看全部 N 个文件」展开入口
        "/api/recommend",
        "放宽方式",             # 2026-08-01：coverage_caveats 展开开关文案——多档放宽策略选择（results.js）
        "cov-strat",            # 策略按钮类名（前端没接 → 展开后两档策略静默失效）
        "relaxDimFully",        # 第二档「不按 X 筛选」入口函数
        "/api/upload",
        "/api/health",
        "/api/diagnose",
        "/api/datasets",
        "/api/introduction",  # 浏览列表保持轻量，点击后再取单个数据集介绍
        "/api/files",           # 按需拉取某数据集全部真实文件直链
        "/api/citations/download",  # cd1：环内 cite.export 引文文件的浏览器下载端点（core.js API 集中声明）
        "fetch(",
    ]
    for token in required_home:
        if token not in bundle:
            fail(f"frontend bundle missing token: {token}")
    # 版本分叉：benchfb 构建带 benchmark 采集（导出反馈包），主线版没有。
    # 静态清单按 index.html 里有没有 benchfbExportBtn 自动分叉，各自钉各自的反馈入口。
    if 'id="benchfbExportBtn"' in html:
        variant_tokens = [
            'id="benchfbModal"',        # 反馈包导出弹窗（大白话清单 + 原文预览 + 单文件下载）
            'id="bfMarkBar"',           # 有用条目标注浮条
        ]
    else:
        variant_tokens = [
            'id="usageReportBtn"',      # 主线版：生成反馈（可复制）按钮
            'id="usageModal"',          # 主线版：聚合文字反馈弹窗（所见即所发）
            'id="usageText"',
        ]
    for token in variant_tokens:
        if token not in bundle:
            fail(f"frontend bundle missing variant token: {token}")
    assert_no_mojibake(html, "html")

    # 1.5) 数据集介绍详情页：「查看介绍」改独立标签页 /dataset。GET 200 + 子标签骨架 + dataset_page.js 可服务。
    ds = client.get("/dataset")
    if ds.status_code != 200:
        fail(f"/dataset returned {ds.status_code}")
    ds_html = ds.text
    for token in ('id="dsTabs"', 'id="dsPanel-intro"', 'id="dsPanel-compare"', 'id="filesModal"', "dataset_page.js"):
        if token not in ds_html:
            fail(f"/dataset page missing token: {token}")
    ds_js = client.get("/static/js/search/dataset_page.js")
    if ds_js.status_code != 200:
        fail("/static/js/search/dataset_page.js not served")
    for token in ("数据集对比", "元数据兼容的数据集"):   # 子标签文案由 dataset_page.js 生成、不在 HTML 骨架里
        if token not in ds_js.text:
            fail(f"dataset_page.js missing subtab label: {token}")
    assert_no_mojibake(ds_html, "dataset.html")

    # 2) 死按钮 / 假数据 / 外部依赖 / 迷惑开关必须已清除
    forbidden = [
        "研究员小林",          # 假用户卡片
        "下载管理",            # 无实现的死导航
        "dna-helix",           # 已移除的无限动画
        "createParticles",     # 已移除的粒子
        "fonts.googleapis.com",  # 外部字体依赖（改本地字体栈）
        "使用 Mock LLM",       # 独立 mock 开关已移除（合并成单一 LLM 开关）
        "填入示例查询",        # 已改为不改变查询内容的内联视觉示意
        # 弱术语化（设计原则：用户只看结果、无需知道底层逻辑）—— 用户界面不得暴露内部检索术语
        "向量召回", "LLM 重排", "LLM 润色", "cross-encoder", "bge-reranker",
        "hard_filter", "确定性规则检索", "结构化约束", "硬过滤", "终检",
    ]
    for token in forbidden:
        if token in html:
            fail(f"index.html still contains removed token: {token}")

    # 3) health
    health = client.get("/api/health")
    if health.status_code != 200 or not health.json().get("ok"):
        fail("/api/health not ok")
    if health.json().get("service") != "dataset-recommender-web":
        fail("unexpected health service name")

    # 4) recommend（mock）—— 结果来自确定性检索，须带真实 reason/score
    rec = client.post(
        "/api/recommend",
        json={"query": "推荐有 FASTQ 的人类乳腺癌数据", "provider": "mock", "use_llm": True, "mock_llm": True},
    )
    if rec.status_code != 200:
        fail(f"/api/recommend returned {rec.status_code}: {rec.text}")
    ctype = str(rec.headers.get("content-type", "")).lower()
    if "application/json" not in ctype or "charset=utf-8" not in ctype:
        fail(f"/api/recommend content-type not utf-8 json: {ctype}")
    rec_data = rec.json()
    if not rec_data.get("ok"):
        fail("/api/recommend ok=false")
    if not rec_data.get("search_trace") or not rec_data.get("interpretation"):
        fail("/api/recommend missing shared interpretation/search_trace")
    if str(rec_data.get("provider", "")).lower() != "mock":
        fail(f"unexpected provider: {rec_data.get('provider')}")
    assert_no_mojibake(str(rec_data.get("markdown", "")), "markdown")

    results = rec_data.get("results", [])
    if not isinstance(results, list) or not results:
        fail("results is empty")
    if not any("✅ 包含 FASTQ" in str(r.get("raw_data_status", "")) for r in results):
        fail("results has no `✅ 包含 FASTQ` row")
    if not any(str(r.get("reason", "")).strip() for r in results):
        fail("results carry no reason (structured retriever payload not surfaced)")
    # 阶段二：结果须带真实文件下载直链（cf.10xgenomics.com 或 s3 10x.files），非数据集页面 url。
    if not any(
        ("cf.10xgenomics.com" in str(r.get("download_url", "")) or "s3-us-west-2" in str(r.get("download_url", "")))
        for r in results
    ):
        fail("results carry no real stage-2 file download link")
    for idx, item in enumerate(results):
        assert_no_mojibake(str(item.get("raw_data_status", "")), f"results[{idx}].raw_data_status")
        assert_no_mojibake(str(item.get("reason", "")), f"results[{idx}].reason")

    # 4.2) 生产者字段集契约（N2）：cards.js / results.js 消费的这组「客户关键字段」必须由后端真发出。
    #      web_smoke 只做前端静态字符串在场检查、从不执行 JS；后端删/改一个这些字段名，三门全绿但
    #      卡片会在浏览器里静默崩（渲染空白）。此处断言字段**存在**（值可空/None），改这组即视为
    #      破坏性契约变更，须按 MODULES.md「字段→消费点」表同步前端消费文件。
    customer_item_fields = {
        "dataset_name", "dataset_uid", "url", "download_url", "source",
        "species", "tissue", "disease", "platform", "sample_size",
        "published_date", "raw_data_status", "reason", "n_files",
    }
    for idx, item in enumerate(results):
        missing = customer_item_fields - set(item.keys())
        if missing:
            fail(f"results[{idx}] 缺少前端消费的关键字段 {sorted(missing)}"
                 "（后端删/改字段名会让 cards.js/results.js 在浏览器里静默崩；属破坏性契约变更，须同步前端消费点）")

    # 4.5) 向量召回（dense）契约：本地模型通常未就绪 → 必须优雅降级为规则顺序，
    #      仍 200 + ok + 非空结果，绝不因缺依赖/缺模型而崩（结果正确性不受影响）。
    recall = client.post(
        "/api/recommend",
        json={"query": "推荐有 FASTQ 的人类乳腺癌数据", "provider": "mock", "use_llm": False, "recall": "dense"},
    )
    if recall.status_code != 200:
        fail(f"/api/recommend (recall=dense) returned {recall.status_code}: {recall.text}")
    recall_data = recall.json()
    if not recall_data.get("ok") or not recall_data.get("results"):
        fail("recall=dense should degrade gracefully to a non-empty rule-ordered result")

    # 5a) 跨维歧义否定 → 解析仍弃权，但 2026-09-12 起不再沉默：向量语义兜底 +
    #     「未经条件核验」标注（resolution_status="vector_fallback"）。
    #     嵌入器不可用的环境回退为诚实弃权（abstained + 0 结果）。
    abstain = client.post(
        "/api/recommend",
        json={"query": "不要小鼠的原始数据", "provider": "mock", "use_llm": True, "mock_llm": True},
    ).json()
    status = abstain.get("resolution_status")
    if status == "vector_fallback":
        if not abstain.get("results"):
            fail("vector_fallback should carry semantic results")
        if not abstain.get("vector_fallback") or "未经条件核验" not in str(abstain["vector_fallback"].get("note", "")):
            fail("vector_fallback missing honest note")
    elif status == "abstained":
        if abstain.get("results"):
            fail("abstained (embedder unavailable) should have 0 results")
        if not str(abstain.get("fallback_reason", "")):
            fail("abstain path missing fallback_reason")
    else:
        fail(f"expected vector_fallback/abstained, got {status}")

    # 5b) 可执行否定 → include human + exclude mouse：有结果、resolution_status=results、无 mouse 违规
    neg = client.post(
        "/api/recommend",
        json={"query": "不要小鼠的人类数据", "provider": "mock", "use_llm": False, "mock_llm": True},
    ).json()
    if neg.get("resolution_status") != "results" or not neg.get("results"):
        fail(f"executable negation should return results, got status={neg.get('resolution_status')}")
    for r in neg.get("results", []):
        if "mouse" in str(r.get("species", "")).lower():
            fail(f"executable negation leaked mouse: {r.get('dataset_name')}")
    if not any(c.get("filter_id") == "raw:forbidden" or c.get("filter_id") == "exclude:species"
               for c in neg.get("query_constraints", [])):
        fail("executable negation missing polarity filter_id in query_constraints")

    # 5c) 不需要fastq → clarification 第三态（不与"没有匹配"混同）
    clar = client.post(
        "/api/recommend",
        json={"query": "不需要fastq的人类数据", "provider": "mock", "use_llm": False, "mock_llm": True},
    ).json()
    if clar.get("resolution_status") != "clarification_required":
        fail(f"expected clarification_required, got {clar.get('resolution_status')}")
    if not (clar.get("clarification") or {}).get("options"):
        fail("clarification missing options")

    # 6) datasets 浏览接口
    ds = client.get("/api/datasets")
    if ds.status_code != 200:
        fail(f"/api/datasets returned {ds.status_code}")
    ds_data = ds.json()
    if not ds_data.get("ok") or int(ds_data.get("count", 0)) <= 0:
        fail("/api/datasets empty")
    records = ds_data.get("records", [])
    if len(records) != int(ds_data.get("count", -1)):
        fail("/api/datasets count != len(records)")
    platform_values = {f.get("value") for f in ds_data.get("facets", {}).get("platform", [])}
    for expected in ("chromium", "visium", "xenium"):
        if expected not in platform_values:
            fail(f"/api/datasets platform facet missing: {expected}")
    if not ds_data.get("facets", {}).get("species"):
        fail("/api/datasets species facet empty")
    # 浏览页并列所有来源：source 分面须含 10x + 外部平台库
    source_values = {f.get("value") for f in ds_data.get("facets", {}).get("source", [])}
    if "10x Genomics" not in source_values:
        fail("/api/datasets source facet missing base source")
    if "CELLxGENE Discover" not in source_values:
        fail("/api/datasets source facet missing external source (run scripts/ingest_cellxgene.py?)")

    # 浏览列表不复制 5,000+ 份长介绍；介绍必须能按数据集标识即时取回。
    sample = records[0]
    if "description" in sample or "introduction" in sample:
        fail("/api/datasets should keep descriptions and introductions on demand")
    intro = client.get(
        "/api/introduction",
        params={
            "uid": sample.get("dataset_uid", ""),
            "url": sample.get("url", ""),
            "name": sample.get("dataset_name", ""),
            "source": sample.get("source", ""),
        },
    )
    if intro.status_code != 200 or not intro.json().get("introduction", {}).get("summary"):
        fail(f"/api/introduction failed: {intro.status_code} {intro.text}")

    # 6.5) 数据来源清单 + 智能查询按来源过滤（并列/自由勾选的后端契约）
    src = client.get("/api/sources")
    if src.status_code != 200 or not src.json().get("ok"):
        fail("/api/sources not ok")
    src_values = [s.get("value") for s in src.json().get("sources", [])]
    if not src_values or src_values[0] != "10x Genomics":
        fail("/api/sources should list 10x Genomics first")
    if "CELLxGENE Discover" not in src_values:
        fail("/api/sources missing external source")

    def _rec_sources(sel):
        r = client.post(
            "/api/recommend",
            json={"query": "人类", "provider": "mock", "use_llm": False, "sources": sel},
        )
        if r.status_code != 200 or not r.json().get("ok"):
            fail(f"/api/recommend (sources={sel}) failed: {r.status_code}")
        return {str(x.get("source")) for x in r.json().get("results", [])}

    if _rec_sources(["10x Genomics"]) - {"10x Genomics"}:
        fail("sources=['10x Genomics'] leaked non-10x results")
    cxg_only = _rec_sources(["CELLxGENE Discover"])
    if not cxg_only or cxg_only - {"CELLxGENE Discover"}:
        fail("sources=['CELLxGENE Discover'] should return only external results")

    # ENCODE 第六来源（2026-08-01 正式提升）：/api/sources 可达 + sources 过滤不串库
    enc = [s for s in src.json().get("sources", []) if s.get("value") == "ENCODE"]
    if not enc or enc[0].get("count") != 40:
        fail(f"/api/sources should list ENCODE with count=40, got {enc}")
    enc_only = _rec_sources(["ENCODE"])
    if not enc_only or enc_only - {"ENCODE"}:
        fail("sources=['ENCODE'] should return only ENCODE results")

    # 7) 上传 UTF-8 BOM JSON（带自定义来源）。每次使用唯一标记，并从响应记录精确
    #    落盘路径；即使中间断言失败，finally 也不会误删并发用户上传。
    from dataset_recommender.corpus.corpus import invalidate_external_cache

    upload_dir = PROJECT_ROOT / "database" / "external"
    upload_root = upload_dir.resolve()
    upload_marker = f"bom_test_web_smoke_{uuid.uuid4().hex}"
    created_upload: Path | None = None
    try:
        bom_payload = [
            {"dataset_name": "BOM Upload Dataset", "species": "Human", "url": "https://example.org/x"}
        ]
        bom_bytes = json.dumps(bom_payload, ensure_ascii=False).encode("utf-8-sig")
        files = {"file": (f"{upload_marker}.json", io.BytesIO(bom_bytes), "application/json")}
        upload = client.post("/api/upload?source=冒烟测试来源", files=files)
        if upload.status_code != 200 or not upload.json().get("ok"):
            fail(f"/api/upload failed: {upload.status_code} {upload.text}")
        upload_data = upload.json()
        if int(upload_data.get("record_count", -1)) != 1:
            fail(f"/api/upload unexpected record_count: {upload_data.get('record_count')}")
        # 确定性安全：上传必须落到 database/external/（外部库），绝不进 database/base/（冻结基准）
        saved_to = str(upload_data.get("saved_to", "")).strip()
        if "database/external/" not in saved_to:
            fail(
                "/api/upload must save under database/external/ (not base database/base/), "
                f"got: {saved_to!r}"
            )
        if not Path(saved_to).name.startswith("upload_"):
            fail(f"/api/upload must reserve the upload_ filename namespace, got: {saved_to!r}")
        created_upload = (PROJECT_ROOT / saved_to).resolve()
        if created_upload.parent != upload_root or upload_marker not in created_upload.name:
            fail(f"/api/upload returned an unexpected saved path: {saved_to!r}")
        # 来源打标：自定义来源被记入
        if upload_data.get("sources", {}).get("冒烟测试来源") != 1:
            fail(f"/api/upload should stamp source, got sources={upload_data.get('sources')}")
        if "warnings" not in upload_data:
            fail("/api/upload response should carry a warnings array")
        # 即时可见：上传后 /api/sources 立刻能看到该来源
        src_after = client.get("/api/sources").json().get("sources", [])
        if "冒烟测试来源" not in {s.get("value") for s in src_after}:
            fail("uploaded source not visible in /api/sources (cache not invalidated?)")
    finally:
        cleanup = set(upload_dir.glob(f"*{upload_marker}*.json"))
        if created_upload is not None:
            cleanup.add(created_upload)
        for created in sorted(path.resolve() for path in cleanup):
            if created.parent != upload_root:
                fail(f"refusing to clean upload outside external directory: {created}")
            created.unlink(missing_ok=True)
        invalidate_external_cache()

    print("home sections present: true")
    print("dead buttons / fake data removed: true")
    print("external font dependency removed: true")
    print("recommend results carry real reason: true")
    print("negation abstains: true")
    print("datasets browse endpoint ok: true")
    print("multi-source select + browse facet ok: true")
    print("upload BOM ok: true")
    print("WEB SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
