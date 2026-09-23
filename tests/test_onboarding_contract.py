# -*- coding: utf-8 -*-
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _strip_js_comments(src: str) -> str:
    """只留代码。**注释里写了什么不算数**——「不许再出现某句旧文案」这类禁语断言，
    必须先把注释剥掉，否则「我们为什么删掉了那句话」的复盘注释本身就会被判违规
    （test_act_frontend 里同一个坑已经踩过一次）。"""
    return re.sub(r"(?m)^\s*//.*$", "", re.sub(r"/\*.*?\*/", "", src, flags=re.S))


def test_static_asset_cache_token_is_single_source():
    """缓存令牌单一真源守卫：index.html 里所有本地静态资源必须共享同一个 ?v= 令牌，且每个
    本地 css/js 引用都必须带令牌（`/vendor/` 固定件除外——它们刻意不带令牌、极少变，已被
    javascript-syntax 门白名单排除）。防的真 bug：只 bump 了一部分 ?v= → 老客户端拿到缓存里
    的旧 JS/HTML 组合、页面静默错乱（本项目真踩过）。用关系断言（全等 + 覆盖）替代写死具体
    令牌值，使每次 bump 只改 index.html 一处、测试无需跟改，而漏 bump 立刻红。只解析 index.html
    的 href/src 属性，不扫 JS（避免 cards.js 等运行期 `?` 拼串被误命中）。"""
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    # 匹配任意 href/src 指向的本地 css/js（单双引号皆可，路径不必字面以 /static/ 开头），捕获 (引号, 路径, ?v=令牌?)。
    refs = re.findall(r"""(?:href|src)\s*=\s*(["'])([^"']+?\.(?:css|js))(\?v=[^"']*)?\1""", html)
    assert refs, "index.html 未发现任何本地 css/js 资源引用（解析失真？）"
    tokens = set()
    for _quote, path, tokenpart in refs:
        if path.startswith(("http://", "https://", "//")):
            continue  # 外部 CDN：不归本项目令牌管
        if "/vendor/" in path:
            continue  # 固定件：刻意不带令牌
        assert tokenpart.startswith("?v="), f"本地资源 {path} 缺少 ?v= 缓存令牌（每次改静态资源必须 bump 令牌）"
        tokens.add(tokenpart)
    assert len(tokens) == 1, (
        f"存在多个不一致的缓存令牌 {sorted(tokens)}（只 bump 了一部分？所有本地资源必须共享同一令牌）"
    )


def test_onboarding_is_first_run_optional_and_replayable():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "web/static/js/core/onboarding.js").read_text(encoding="utf-8")
    css = (ROOT / "web/static/css/app.css").read_text(encoding="utf-8")
    boot = (ROOT / "web/static/js/core/boot.js").read_text(encoding="utf-8")
    assert 'id="onboarding"' in html and 'id="tutorialReplay"' in html
    assert 'id="onboardingSkip"' in html and 'id="onboardingClose"' in html
    assert "localStorage.getItem(LS.onboarding)" in js
    assert "localStorage.setItem(LS.onboarding" in js
    assert 'event.key === "Escape"' in js
    assert "initOnboarding()" in boot
    assert "maybeAutoOpenSettings()" not in boot
    assert ".onboarding {" in css and "z-index: var(--z-tour)" in css   # Round3 V5：z 层叠令牌化（原 2147483000 魔法数 → --z-tour:1000，定义在 :root）
    assert ".onboarding.onboarding-settings" in css
    assert "left: 50%; top: 50%" in css
    assert 'id="onboardingSurface"' in html and ".onboarding-surface" in css
    assert "max-height: min(78dvh, 620px)" in css
    assert "position: sticky; bottom: 0" in css
    assert '"#apiConfigBody"' in js and '"#modelInstallRow"' in js
    assert 'classList.toggle("onboarding-settings"' in js
    # Round3 V1：导览独立紫（oklch hue 274）收敛为品牌青族——--tour-accent 改从全局 accent 派生，oklch 紫字面量已清零
    assert "--tour-accent: var(--accent)" in css and "oklch(56% 0.18 274)" not in css
    assert ".onboarding-focus-ring" in css and "z-index: var(--z-tour-ring)" in css   # V5：原 2147482999 → --z-tour-ring:999
    assert "pointer-events: none" in css
    assert ".onboarding-focus { outline: none !important; }" in css
    assert "function syncOnboardingFocusRing()" in js
    assert 'document.addEventListener("scroll", syncOnboardingFocusRing, true)' in js
    assert "requestAnimationFrame(syncOnboardingFocusRing)" in js
    assert 'disclosure: "#apiConfig"' in js
    assert "captureOnboardingDisclosures();" in js
    assert "syncOnboardingDisclosures(step);" in js
    assert "restoreOnboardingDisclosures();" in js
    assert "disclosure.open = step.disclosure === selector ? true : wasOpen" in js
    assert "教程已经展开真实配置表单" in js
    assert 'focusTarget: "#cfgProvider"' in js and 'nextLabel: "填好了，继续"' in js
    assert "function initOnboardingDrag()" in js
    assert "function initOnboardingSizeObserver()" in js
    assert 'typeof ResizeObserver !== "function"' in js
    assert "onboardingSizeObserver.observe(panel);" in js
    assert "if (window.innerWidth <= 780)" in js
    assert 'panel.style.right = `${edge}px`' in js
    assert "setPointerCapture" in js and "placeDraggedOnboarding" in js
    assert 'addEventListener("lostpointercapture", finish)' in js
    assert 'window.addEventListener("pointerup", finish)' in js
    assert "resetOnboardingPosition();" in js


def test_tutorial_has_early_task_chain_and_transform_only_resize_motion():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "web/static/js/core/onboarding.js").read_text(encoding="utf-8")
    css = (ROOT / "web/static/css/app.css").read_text(encoding="utf-8")
    # 2026-07-29 起 9 步；2026-08-14 ux3 起 10 步：「细化筛选」步后插入「数据集详情页里有什么」
    # （详情页实拍截图 + 六子标签一句话），新用户不必真开页面就知道详情页含哪些信息。
    # 2026-08-19 tu1 起 11 步：数组头部新增「第0步」（使用反馈承诺 + 高质量查询引导，BENCHFB_BUILD 分叉）。
    # om1：把 ob3 两个尾部能力步骤合并前移为第 2 屏，总步数 13→12；用户先知道能交代完整任务，
    # 再学习查询细节，避免只把产品理解成普通检索页。
    # fx2（2026-08-22）：在「出结果后可以接着改条件」之后插入「接进你自己的 AI 助手」步
    # （可选，复用帮助页 copyAgentPrompt 单一真源），总步数 12→13。
    # 任务C（2026-08-23）：①「我的库」介绍步插到「出结果后可以接着改条件」之后（追踪+收藏双页签、
    # 只存本机浏览器），②「接进你自己的 AI 助手」按产品方向后移至结尾（走 MCP/技能包会绕开
    # react agent 侧数据采集、不主动推，文案精简），总步数 13→14；使用反馈步仍收尾。
    assert 'id="onboardingProgress">1 / 14<' in html
    assert js.count('target: "') == 14
    for visual in ("query", "scope", "provider", "ranking", "polish", "trace", "refine", "detail", "board", "react", "agent"):
        assert f'data-onboarding-visual="{visual}"' in html
        assert f'visual: "{visual}"' in js
    # fx2「接进你自己的 AI 助手」步视觉块 + 复制按钮接线：copyAgentPrompt 单一真源
    #（interactions.js 提为 export，onboarding.js 委托复用），「下载技能包」纯 <a download> 无需 JS。
    assert 'id="tourAgentPromptCopyBtn"' in html
    assert 'copyAgentPrompt' in js
    assert 'event.target.closest("#tourAgentPromptCopyBtn")' in js
    assert 'href="/api/guide/skill.zip"' in html
    # 详情页步骤的实拍截图：随包静态资产、同源绝对路径（首页/详情页都能解析）、有 alt。
    assert 'src="/static/assets/onboarding-dataset-detail.png?v=' in html
    assert (ROOT / "web/static/assets/onboarding-dataset-detail.png").is_file()
    assert "填入示例查询" not in html
    assert "onboardingExample" not in html and "onboardingExample" not in js
    assert 'panel.dataset.size = step.size || "compact"' in js
    assert 'size: "wide"' in js and 'size: "medium"' in js and 'size: "compact"' in js
    assert "function animateOnboardingResize(panel, previousRect)" in js
    assert "function cancelOnboardingResize()" in js
    assert "onboardingResizeStartTimer = setTimeout(() =>" in js and "}, 16);" in js
    assert "onboardingViewportClampTimer = setTimeout(() =>" in js and "}, 80);" in js
    assert 'surface.style.transition = "transform 240ms cubic-bezier(.22, 1, .36, 1)"' in js
    resize_fn = js.split("function animateOnboardingResize", 1)[1].split("function setOnboardingDone", 1)[0]
    assert "scaleX" in resize_fn and "scaleY" in resize_fn and 'surface.style.transform = "none"' in resize_fn
    assert "style.width" not in resize_fn and "style.height" not in resize_fn
    # UX1：FLIP 缩放动画的减弱动效判据从加载时取一次的 REDUCE_MOTION 常量，改成实时读 matchMedia 的
    # onboardingReduceMotion()（会话中途开启也即时生效）；助手函数里仍以 REDUCE_MOTION 为基。
    assert "onboardingReduceMotion()" in resize_fn
    assert "function onboardingReduceMotion()" in js and 'matchMedia("(prefers-reduced-motion: reduce)")' in js
    assert '.onboarding[data-size="wide"]' in css
    assert ".tour-ranking" in css and ".tour-polish" in css and ".tour-refine" in css
    assert ".tour-visual[hidden] { display: none !important; }" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_tutorial_copy_explains_ranking_polish_trace_and_refinement_boundaries():
    js = (ROOT / "web/static/js/core/onboarding.js").read_text(encoding="utf-8")
    for text in (
        "规则排序始终开启",
        "本地精准重排不需要 API Key",
        "AI 重排适合复杂描述",
        "不会把不符合必选条件的数据排回来",
        "AI 润色只改说明，不改结果",
        # 「不把打算用的说成已经用了」这条诚实语义必须留在教程里；2026-07-21 文案校对把它从
        # 「不会把计划使用的能力冒充成已经成功执行」改写成下面这句大白话，语义不变、更好懂。
        "不会把「本来打算用」写成「已经用了」",
        "用细化筛选收窄，再查看数据集介绍",
        "即使不开 AI 也能使用",
    ):
        assert text in js


def test_step0_true_branch_final_copy_with_accent_and_bold_marks():
    """ob2/S5：第0步反馈强化版（BENCHFB_BUILD=true 分支）终稿契约；ux1 起文案如实化。

    - 正文逐字含「默认开启本地数据采集」「每个账户首次发送前独立确认」
      「经明文 HTTP 上传到项目所有者的服务器（保留 90 天）」「不采集 API Key、密码和账户名」
      （2026-08-22 ux1：旧的「安全 HTTPS 通道」虚口径已如实改写为明文 HTTP + 90 天 + 结构性去标识；
      2026-08-22 ov1-fix2：结构性去标识 → 尽力过滤（手机号/证件号/邮箱自动遮蔽、API Key 等直接
      标识删除，并非匿名化），与 consent v2 口径一致）；
    - 评分频率新口径入正文（每会话最多两次）+ 隐私提醒（勿输入患者隐私/样本信息/未公开课题）；
    - 旧「导出反馈包 / 手动发给开发者」口径已清除（导出仅作兜底，不再出现在 step0 正文）；
    - 样式只走 additive 字段 titleAccent/boldText（与 BENCHFB_BUILD 绑定），渲染端 classList.toggle
      挂类、CSS 用主题色变量（--tour-accent = 全局 --accent）——主线版 false 分支文案与视觉一律不动。
    """
    js = (ROOT / "web/static/js/core/onboarding.js").read_text(encoding="utf-8")
    css = (ROOT / "web/static/css/app.css").read_text(encoding="utf-8")
    assert "默认开启本地数据采集" in js
    assert "每个账户首次发送前独立确认" in js
    assert "记录经尽力过滤（手机号/证件号/邮箱自动遮蔽、API Key 等直接标识删除，并非匿名化）后经明文 HTTP 上传到项目所有者的服务器（保留 90 天）" in js
    assert "配置安全 HTTPS 通道后才会脱敏自动上传" not in js
    assert "不采集 API Key、密码和账户名" in js
    assert "请尽管用真需求考验它" in js
    assert "评分只在你最关注的检索/执行轮主动出现（每个会话最多两次）" in js
    assert "请勿输入患者隐私、样本信息或未公开的研究内容" in js
    assert "导出的.json程序包手动发给开发者" not in js
    assert "若您愿意贡献您的使用与评价记录用于改善产品" not in js
    assert "titleAccent: BENCHFB_BUILD" in js and "boldText: BENCHFB_BUILD" in js
    assert 'classList.toggle("onboarding-title-accent"' in js
    assert 'classList.toggle("onboarding-text-bold"' in js
    assert ".onboarding-copy h3.onboarding-title-accent { color: var(--tour-accent); }" in css
    assert ".onboarding-copy > p.onboarding-text-bold { font-weight: 700; }" in css
    # 主线版 false 分支：标题与正文文案保持 tu1 原样（ob2 不改动）
    assert 'title: BENCHFB_BUILD ? "你的使用，在帮它变好" : "尽管用真需求考验它"' in js
    assert "检索、下载、入库、对比、导出引文，一句话都能交代。" in js


def test_tutorial_task_chain_is_second_screen_and_uses_plain_product_language():
    """om1：不用 React/RAG 术语，第二屏即用真任务链建立“检索只是起点”的心智。"""
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "web/static/js/core/onboarding.js").read_text(encoding="utf-8")
    css = (ROOT / "web/static/css/app.css").read_text(encoding="utf-8")
    assert 'data-onboarding-visual="react"' in html
    assert 'visual: "react"' in js
    for q in (
        "下载 Xenium 人类乳腺癌",
        "检查一下 10x Genomics 有没有新发布的数据集，有的话帮我更新入库",
        "搜乳腺癌数据，对比前两条，检查投稿可用性，再导出引文",
    ):
        assert q in html, f"示例 query 未进教程视觉：{q}"
    assert "搜乳腺癌数据，对比前两条，检查投稿可用性，再导出引文" in js
    first = js.index('title: BENCHFB_BUILD ? "你的使用，在帮它变好"')
    capability = js.index('title: "不只找数据：一句话交代整件事"')
    query = js.index('title: "一句话写清实验需求"')
    assert first < capability < query
    assert "系统会先找到符合条件的数据" in js
    assert "做不到或拿不准的" in js
    assert "不假装成功" in js
    assert "React" not in _strip_js_comments(js) and "RAG" not in _strip_js_comments(js)
    assert ".tour-react .tour-query-line + .tour-query-line" in css


def test_api_key_is_filled_in_the_real_form_during_onboarding():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "web/static/js/core/onboarding.js").read_text(encoding="utf-8")
    assert 'id="cfgProvider"' in html and 'id="cfgApiKey" type="password"' in html
    assert 'target: "#apiConfigBody"' in js and 'disclosure: "#apiConfig"' in js
    assert 'focusTarget: "#cfgProvider"' in js
    assert "API Key；默认只在本次会话使用" in js
    assert 'nextLabel: "填好了，继续"' in js
    assert "field.focus({ preventScroll: true })" in js


def test_api_key_persistence_requires_separate_explicit_opt_in():
    """ah-c1：普通设置可记住，但密钥必须有独立、显式的持久化授权。
    2026-08-25 夜 ux 精简批：界面只留「记住设置」「记住api key」两项八个字，
    「key 落盘以设置落盘为前提」改由联动行为表达（勾 key 带设置、撤设置撤 key）。"""
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    shell = (ROOT / "web/static/js/core/shell.js").read_text(encoding="utf-8")
    interactions = (ROOT / "web/static/js/core/interactions.js").read_text(encoding="utf-8")
    assert 'id="cfgRememberApiKey"' in html
    assert ">记住设置</span>" in html and ">记住api key</span>" in html
    assert "在本浏览器记住非敏感设置" not in html, "旧长文案应已精简移除"
    assert "if (rk.checked) $(\"cfgSaveSession\").checked = true;" in interactions
    assert "if (!ss.checked) $(\"cfgRememberApiKey\").checked = false;" in interactions
    assert "c.save === true && c.rememberApiKey === true" in shell
    assert "if (rememberApiKey) stored.apiKey = c.api_key" in shell
    assert "delete c.apiKey" in shell, "旧版无独立授权的残留 Key 必须主动清理"
    assert "apiKey: c.api_key" not in shell, "不得把 Key 混入普通设置的无条件持久化对象"


def test_tutorial_explains_api_and_ranking_modes_in_plain_language():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    for text in ("API 在哪里配置", "规则排序", "本地精准重排", "AI 重排", "AI 润色推荐说明"):
        assert text in html
    assert 'id="cfgAutoLlm"' in html


def test_settings_group_strategy_controls_and_keep_copy_concise():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    css = (ROOT / "web/static/css/app.css").read_text(encoding="utf-8")
    shell = (ROOT / "web/static/js/core/shell.js").read_text(encoding="utf-8")
    assert '<details class="api-config" id="apiConfig">' in html
    assert '<summary id="apiConfigToggle">' in html
    assert '<details class="api-config" id="apiConfig" open>' not in html
    assert 'id="apiConfigStatus"' in html and 'id="apiConfigBody"' in html
    assert '<span class="api-config-chevron" aria-hidden="true">' in html
    assert '<svg viewBox="0 0 16 16" focusable="false">' in html
    assert '<path d="m4.5 6.25 3.5 3.5 3.5-3.5"></path>' in html
    assert '<span class="api-config-chevron" aria-hidden="true">⌄</span>' not in html
    assert 'class="strategy-panel ranking-control auto-owned"' in html
    assert 'id="strategyAutoRow"' in html and 'id="strategyModeHint"' in html
    # 2026-08-03 agent2 设置三维度化：维度 A＝规则排序（恒开标识）∥ 本地精准重排 ∥ AI 重排
    # 三张并列卡；AI 重排从已退役的大模型面板回归排序面板。
    assert html.count('class="strategy-choice strategy-tip"') == 2
    assert 'strategy-choice-rule strategy-rule strategy-tip active' in html
    assert "规则排序" in html and "本地精准重排" in html and "AI 重排" in html
    assert 'id="nodeRule"' in html and 'id="cfgRule"' not in html
    assert 'id="nodeRerank"' in html and 'id="cfgRerank"' in html
    assert "strategy-choice-icon" not in html
    assert html.count('data-tooltip=') >= 4
    # 大模型总开关面板已退役（API 可用性门控取代之）；维度 C「AI 执行」是一等开关。
    assert 'llm-control' not in html and 'id="llmMasterRow"' not in html and 'id="llmSub"' not in html
    assert 'id="cfgLlm"' not in html and 'id="cfgAutoAct"' not in html and 'id="cfgAgent"' not in html
    assert 'id="nodeAgentExec"' in html and 'id="cfgAgentExec"' in html and 'id="cfgPolish"' in html
    assert 'id="strategyDetail"' in html and 'id="rerankDetail"' in html
    assert 'id="modelInstallRow"' in html and 'id="modelInstallBtn"' in html and 'id="modelCancelBtn"' in html
    assert "约下载 3 GB" in html and "安装后约占 5 GB" in html
    assert "strategyOwnership" not in html and "ownership-badge" not in html
    for removed_copy in (
        "下方均为可选项",
        "按候选压力和语义信息量",
        "纯本地、不联网、不额外花钱",
        "默认关时直接给结果",
    ):
        assert removed_copy not in html
    assert ".api-config[open] .api-config-chevron svg" in css
    assert "grid-template-columns: minmax(0, 1fr) auto 26px" in css
    assert ".strategy-choice-grid" in css
    assert ".ranking-control:not(.auto-owned) .strategy-choice.active" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    assert ".strategy-choice-rule" in css and ".strategy-choice-icon" not in css
    assert ".strategy-auto-ai.off, .strategy-rerank-detail.off" in css
    # 2026-08-03 agent2：B（自动选择）开 → 隐藏 A 的手动项（.auto-owned 单一收口，不再逐个 disabled）
    assert ".ranking-control.auto-owned .strategy-choice-grid { display: none; }" in css
    assert ".ranking-control:not(.auto-owned) .strategy-auto-ai" in css
    # 自动模式下「AI 重排候选数」保留可调，不隐藏整区
    assert ".ranking-control.auto-owned .strategy-rerank-detail" not in css
    assert 'detail.classList.toggle("off", !on && !$("cfgRerank").checked)' in shell
    # AI 门控标注三件套（未配 key 禁点 / 降级标注 / 技术细节「?」钮）
    assert ".gate-tag" in css and ".ai-gated" in css and ".ai-degraded" in css and ".help-dot" in css
    assert ".llm-sub" not in css, "大模型子开关组样式必须随面板一起删干净"
    assert 'control.classList.toggle("auto-owned", on)' in shell
    assert "自动模式：规则排序始终启用，系统按每次查询的匹配情况自动决定" in shell
    assert "手动模式：规则排序始终启用；可按需叠加本地精准重排或 AI 重排。" in shell
    # 开关语义统一（P0-3）：已配 key 必可开关——不再有 masterOn/auto 叠加的 disabled 锁
    assert '$("cfgRerank").disabled' not in shell, "AI 重排开关不得再被第二道闸锁死（P0-3 根因）"
    assert '$("cfgRecall").disabled' not in shell


def test_settings_provider_choices_are_grouped_presets_and_legacy_values_migrate():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    shell = (ROOT / "web/static/js/core/shell.js").read_text(encoding="utf-8")
    provider_block = html.split('id="cfgProvider"', 1)[1].split("</select>", 1)[0]
    # 2026-08-25 T3：+1「限量试用」（value=trial，免密钥每日限轮、服务端托管锁定）→ 10 个选项。
    assert provider_block.count("<option value=") == 10
    assert 'label="主流 API 服务商"' in provider_block
    assert 'label="高级接入"' in provider_block
    for value, label in (
        ("mock", "本地演示"), ("trial", "限量试用"),
        ("deepseek", "DeepSeek"), ("kimi", "Kimi"),
        ("qwen", "Qwen"), ("zhipuai", "GLM（智谱）"), ("openrouter", "OpenRouter"),
        ("openai", "OpenAI"), ("compatible", "兼容接口"), ("local", "本地模型"),
    ):
        assert f'value="{value}"' in provider_block and label in provider_block
    for base_url, model in (
        ("https://api.deepseek.com", "deepseek-chat"),
        ("https://api.moonshot.cn/v1", "kimi-k2.6"),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
        ("https://open.bigmodel.cn/api/paas/v4/", "glm-5.1"),
        ("https://openrouter.ai/api/v1", "openrouter/auto"),
        ("https://api.openai.com/v1", "gpt-4o-mini"),
    ):
        assert base_url in shell and model in shell
    for preset in ("deepseek", "kimi", "qwen", "openrouter", "openai"):
        assert f'{preset}: "compatible"' not in shell
    for legacy in ("gemini", "custom"):
        assert f'{legacy}: "compatible"' in shell
    assert 'glm: "zhipuai"' in shell
    assert 'id="providerPresetHint"' in html and 'hint.textContent = p.note' in shell
    assert "接口地址 base_url" not in html and "模型名 model" not in html
    # 缓存令牌的不变量已上移到 test_static_asset_cache_token_is_single_source（关系断言：所有本地
    # 资源共享同一 ?v= 令牌 + 都带令牌）。不再在此写死具体令牌值/枚举旧令牌——那是每次 bump 都要
    # 手改的脆弱源；关系断言自维护，漏 bump 或不一致同样会红。


def test_sidebar_resize_has_pointer_keyboard_and_persistence_contract():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    css = (ROOT / "web/static/css/app.css").read_text(encoding="utf-8")
    shell = (ROOT / "web/static/js/core/shell.js").read_text(encoding="utf-8")
    boot = (ROOT / "web/static/js/core/boot.js").read_text(encoding="utf-8")
    assert 'id="sideResizer"' in html and 'role="separator"' in html
    assert 'aria-valuemin="240"' in html and 'aria-valuemax="420"' in html
    assert "biodata_sidebar_width_v1" in (ROOT / "web/static/js/core/core.js").read_text(encoding="utf-8")
    assert "function initSidebarResize()" in shell and "setPointerCapture" in shell
    assert 'event.key === "ArrowLeft"' in shell and 'event.key === "ArrowRight"' in shell
    assert 'resizer.addEventListener("lostpointercapture", finish)' in shell
    assert 'window.addEventListener("resize", () => { if (window.innerWidth <= 780) finish(null); });' in shell
    assert "initSidebarResize();" in boot
    assert ".side-resizer" in css and "body.side-resizing" in css


def test_result_summary_is_built_from_real_backend_trace():
    """ux3/ux3c：结果头三处合并成一段自然语言摘要（renderResultSummary）；逐步 trace「查看每一步」折叠已删（用户判冗余）。
    留下的诚实不变量：方法句只据真实 search_trace 的 used/fallback 状态生成，绝不把计划能力冒充成已执行；
    无 trace（新前端 + 未重启旧后端）如实说「执行明细不可用」；计数并入句子。"""
    results = (ROOT / "web/static/js/search/results.js").read_text(encoding="utf-8")
    assert "function renderResultSummary" in results
    assert "data.search_trace" in results
    assert 'status === "used"' in results and 'status !== "fallback"' in results
    assert 'used.has("local_semantic")' in results and 'used.has("llm_rerank")' in results
    # 2026-07-26：这里原来钉的是 `assert "本次未启用" in results`——**它把缺陷钉成了正确**。
    # 后端一直分得清 `skipped`（没启用）与 `fallback`（试过但没成），前端却把两者一律写成
    # 「本次未启用」，于是 provider 真返 400 的那几天，界面读起来像是系统自己选择不用这一层。
    # 现在措辞由后端 `step.fallback_note` 给（单一真源 workflow._fallback_note），
    # 前端只拼层名 → 这里改钉「前端不许自己写死这句措辞」，具体分档由
    # tests/test_fallback_wording_honesty.py 用真行为验证。
    assert "本次未启用" not in _strip_js_comments(results)
    assert "fallback_note" in results and "function fallbackLayerNotes" in results
    assert "data.result_total" in results             # 「库中共 N 条匹配」计数并入方法句、不再单独渲染 #resultsTotal
    assert "条关键信息" not in results
    assert "执行明细不可用（请重启后端）" in results    # 新前端 + 未重启旧后端：无 trace 时如实说、绝不猜方法
    # 逐步 trace 明细与「查看每一步」折叠已删（ux3c）：results.js 不再有 CUSTOMER_TRACE_IDS / customerTraceSteps；
    # index.html 不再有 #searchTraceToggle / #searchTraceBody（#searchTrace 摘要卡容器仍在，web_smoke 亦钉）。
    assert "CUSTOMER_TRACE_IDS" not in results and "customerTraceSteps" not in results
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    assert 'id="searchTraceToggle"' not in html and 'id="searchTraceBody"' not in html
    assert 'id="searchTrace"' in html


def test_query_input_previews_source_and_time_with_shared_parser():
    core = (ROOT / "web/static/js/core/core.js").read_text(encoding="utf-8")
    interactions = (ROOT / "web/static/js/core/interactions.js").read_text(encoding="utf-8")
    assert 'interpret: "/api/interpret"' in core
    assert "function scheduleInterpretationPreview()" in interactions
    assert "fetch(API.interpret" in interactions
    assert "data.interpretation" in interactions
    assert "scheduleInterpretationPreview();" in interactions


def test_tutorial_provider_buttons_drive_the_real_settings_form():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "web/static/js/core/onboarding.js").read_text(encoding="utf-8")
    provider_block = html.split('id="cfgProvider"', 1)[1].split("</select>", 1)[0]
    # 教程卡里的服务商按钮 data 值必须严格等于真实 select 的 option value（值不同点按钮就填错预设）。
    for value in ("deepseek", "kimi", "qwen", "zhipuai", "openrouter"):
        assert f'data-tour-provider="{value}"' in html
        assert f'value="{value}"' in provider_block
    # 五个按钮都可点、带 aria-pressed，且「GLM」对应 zhipuai 预设（不是独立 brand）。
    brands = html.split('class="tour-provider-brands"', 1)[1].split("</div>", 1)[0]
    assert brands.count('class="tour-provider-brand"') == 5
    assert brands.count('aria-pressed="false"') == 5
    assert 'data-tour-provider="zhipuai" aria-pressed="false">GLM</button>' in brands
    # 事件委托把按钮接到真实表单：设 value + dispatch change（applyPreset 自动填地址/模型），
    # 并同步 aria-pressed、把焦点移到 API Key 框。
    assert '$("onboardingVisuals").addEventListener("click"' in js
    assert 'event.target.closest("[data-tour-provider]")' in js
    assert 'dispatchEvent(new Event("change", { bubbles: true }))' in js
    assert "syncTourProviderPressed();" in js
    assert 'setAttribute("aria-pressed"' in js
    assert '$("cfgApiKey")' in js and 'key.focus({ preventScroll: true })' in js
    assert "也可以直接点上面的服务商名" in js


def test_tutorial_detail_image_has_cache_token_and_text_fallback():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "web/static/js/core/onboarding.js").read_text(encoding="utf-8")
    assert 'src="/static/assets/onboarding-dataset-detail.png?v=' in html
    assert 'class="tour-detail-fallback" hidden' in html
    fallback = html.split('class="tour-detail-fallback"', 1)[1].split("</div>", 1)[0]
    for label in ("介绍", "全部文件", "元数据兼容", "FAIR 自检", "导出引文", "数据集对比"):
        assert label in fallback
    assert "initOnboardingDetailFallback();" in js
    assert 'addEventListener("error"' in js
