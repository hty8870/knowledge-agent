"use strict";

/* 本文件是 ES Module：core/shell/usage_log 经 import 取。boot 经 import 取
   initOnboarding（绞杀桥全退役）。import #interactions 的 copyAgentPrompt
   （教程「接进你自己的 AI 助手」步复制按钮，与帮助页同一实现；
   2026-08-23 按产品方向后移并精简——走 MCP/技能包会绕开 react agent 侧的数据采集、
   不主动推，仍复用 copyAgentPrompt 单一真源；单边无环）。 */
import { LS, REDUCE_MOTION, $ } from "#core";
import { closeSettings, openSettings, showView } from "#shell";
import { isBenchfbBuild } from "#usage_log";
import { copyAgentPrompt } from "#interactions";

/* 版本分叉：反馈强化版默认开、主线版默认关——同一份 onboarding.js 服务两个版本，
   文案各自如实。构建判定收口在 usage_log.isBenchfbBuild（2026-08-15：显式 meta 标记优先，
   不再各文件自探 DOM）。 */
const BENCHFB_BUILD = isBenchfbBuild();

const ONBOARDING_STEPS = [
    {
        // 2026-08-19 新增「第0步」：开场先讲数据采集承诺 + 引导发高质量 query。
        // 2026-08-19 终稿：只改反馈强化版（true 分支）——标题「你的使用，在帮它变好」
        // 渲染成主题绿色、正文整体加粗；用 additive 字段 titleAccent/boldText 按 BENCHFB_BUILD
        // 绑定（false 分支字段为 false，主线版视觉与文案一律不动），渲染端据此挂样式类。
        // 2026-08-21：默认本地采集、per-profile consent、安全通道配置后上传（旧「导出反馈包手动
        // 发给开发者」口径清除，导出保留作兜底），与设置区使用反馈 tooltip 及末步指路一致。
        // 2026-08-22 如实化：明文 HTTP + 90 天保留 + 结构性去标识口径写进正文（与 consent 弹窗、
        // 设置页采集卡片同一份事实）；评分频率改口「最关注的轮次、每会话最多两次」。
        // 2026-08-22：结构性去标识 → 尽力过滤（手机号/证件号/邮箱自动遮蔽、API Key 等
        // 直接标识删除，并非匿名化），与 consent v2 文案口径一致。
        target: "#queryInput",
        title: BENCHFB_BUILD ? "你的使用，在帮它变好" : "尽管用真需求考验它",
        titleAccent: BENCHFB_BUILD,
        text: BENCHFB_BUILD
            ? "这个版本默认开启本地数据采集，每个账户首次发送前独立确认；记录经尽力过滤（手机号/证件号/邮箱自动遮蔽、API Key 等直接标识删除，并非匿名化）后经明文 HTTP 上传到项目所有者的服务器（保留 90 天），用于改进推荐排序与制作评测基准，不采集 API Key、密码和账户名。请尽管用真需求考验它——评分只在你最关注的检索/执行轮主动出现（每个会话最多两次），不满意就写一句哪里不对；请勿输入患者隐私、样本信息或未公开的研究内容。"
            : "检索、下载、入库、对比、导出引文，一句话都能交代。复杂、冷门、专业的需求尽管上：物种、组织、疾病、技术、时间等约束写多、写具体，效果更好；描述模糊也没关系，可以追问、改条件，系统不会擅自替你猜。",
        boldText: BENCHFB_BUILD,
        visual: "",
        size: "medium",
    },
    {
        // 能力心智必须前置：不讲 React/RAG 术语，直接用一条完整任务说明“先找数据，再完成后续工作”。
        // 原先的两步在第 11/12 屏，用户容易错过；本步吸收其能力例与诚实边界并前移到第 2 屏。
        target: "#queryInput",
        title: "不只找数据：一句话交代整件事",
        text: "可以直接说「搜乳腺癌数据，对比前两条，检查投稿可用性，再导出引文」。系统会先找到符合条件的数据，再把对比、核验、下载或引文整理成有顺序的步骤继续完成；做不到或拿不准的，会明确停在哪一步，不假装成功。",
        visual: "react",
        size: "wide",
    },
    {
        target: "#queryInput",
        title: "一句话写清实验需求",
        text: "可以同时写物种、组织、疾病、技术、原始数据、来源和时间。系统只把能确认的内容转成必须满足或必须排除的条件；看不懂的歧义会明确提示，而不是擅自猜测。",
        visual: "query",
        size: "medium",
    },
    {
        target: "#heroScopeBtn",
        title: "来源和时间默认自动识别",
        text: "查询里的来源名称和时间会自动进入检索范围。需要固定范围时，点输入框右端的范围圆钮展开「数据来源 / 发表时间」，改成手动选择。识别落地时圆钮会亮一下并浮出摘要；结果上方的摘要条也会如实告诉你系统把你的话理解成了什么。",
        visual: "scope",
        size: "compact",
    },
    {
        target: "#apiConfigBody",
        title: "让复杂任务真正跑起来",
        text: "涉及多步判断、对比和执行时，需要 AI 帮手。教程已经展开真实配置表单：先选服务商，再填写自己的 API Key；默认只在本次会话使用，除非你主动勾选记住设置。不想现在配置可以直接下一步，基础检索仍然可用。也可以直接点上面的服务商名，右侧表单会自动填好地址和模型，你只需填 API Key。",
        settings: true,
        top: true,
        disclosure: "#apiConfig",
        focusTarget: "#cfgProvider",
        nextLabel: "填好了，继续",
        visual: "provider",
        size: "medium",
    },
    {
        target: "#modelInstallRow",
        title: "规则检索开箱即用，两种增强随时补",
        text: "规则排序始终开启。本地精准重排不需要 API Key，可在安装器里勾选在线下载，也可在这里稍后安装；AI 重排适合复杂描述，使用你刚才填写的接口。两层增强失败都会保留规则结果，不会把不符合必选条件的数据排回来。",
        settings: true,
        visual: "ranking",
        size: "wide",
    },
    {
        target: "#nodePolish",
        title: "AI 润色只改说明，不改结果",
        text: "开启后，AI 会把已经确定的推荐理由整理得更自然；数据集集合、排序顺序和必选/排除条件都不会改变。没有 API、调用失败或输出不可用时，页面继续展示原始说明。",
        settings: true,
        visual: "polish",
        size: "medium",
    },
    {
        target: "#submitBtn",
        title: "检索后，核对系统实际做了什么",
        // 2026-08-22：本步吸收结果页「下一步行动」与过宽收窄建议
        // ——风险分层如实写：确定性动作直接执行、多步任务先进可编辑任务卡确认。
        text: "结果上方有一段摘要，说明这次实际用了哪些排序、共匹配多少条、展示了前几条，以及是否有数据因字段未标注被排除。摘要写的是实际发生的事：某一层排序没能用上，它会直说改用了基础方式，不会把「本来打算用」写成「已经用了」。结果区还会给出一排「下一步行动」：套用筛选、导出这类一步能办的事，点了直接执行；需要联网或多步判断的，会先打开一张可编辑的任务卡，写清要做什么，你点「开始」才动手。结果明显偏多时，结果区顶部还会给一两条收窄建议，点一下就按那个维度继续筛。",
        visual: "trace",
        size: "medium",
    },
    {
        target: "#sideFacets",
        title: "用细化筛选收窄，再查看数据集介绍",
        text: "「细化筛选」面板要在完成首次检索、出现结果之后才会显示在左侧，所以现在可能还看不到高亮。有结果后，它会给出可继续筛选的物种、组织、疾病、平台等维度。每张结果卡的「数据集详情」会汇总已有结构化元数据，即使不开 AI 也能使用；需要文件级详情时再进入文件清单。",
        visual: "refine",
        size: "wide",
    },
    {
        // target 指结果网格：首次自动弹出时还没有结果（目标零尺寸 → 焦点环自动隐藏），
        // 无妨——这一步的主角是教程卡里那张详情页实拍截图，文案只说图，不指页面上的东西。
        target: "#resultsGrid",
        title: "「数据集详情」页里有什么",
        text: "点卡片上的「数据集详情 ↗」，在新标签页打开的就是这个页面：六个子标签各管一件事——介绍 · 全部文件 · 元数据兼容 · FAIR 自检 · 导出引文 · 数据集对比。",
        visual: "detail",
        size: "wide",
    },
    {
        // target 指向常驻可见的搜索框区域，而不是首屏还藏着的条件板本身——
        // 焦点环对尺寸为 0 的目标会直接隐藏，那样就会讲一段没有任何高亮的话。
        // （设计 §11）：本步吸收「存为追踪 / 我的库双页签 / 上下文 chip / 检查更新」——
        // 一级导航、更新检查绝不自动纳入、导出中心一句话带过；上下文 chip 按 §1.3 修订口径如实写
        // （发用户自己配置的 AI 服务商、随下一条消息发出后自动移除、发出前可移除；本地模型不出本机）。
        target: ".console",
        title: "出结果后可以接着改条件",
        text: "出结果后，搜索框下方会列出系统实际在用的每条筛选条件。每条右边可以改、可以不按它筛；也可以直接打字说「换成小鼠」「去掉组织限制」，还能一步步退回上一次的条件。这段完全在本地完成，不用大模型。一个方向要长期跟进时，点结果区的「存为追踪」把它存下来：之后在左侧导航「我的库」浮窗里管理——「追踪」页签管候选名单、随时检查有没有新数据（发现了也是你逐条决定要不要纳入）、导出引文和整包研究材料；「收藏」页签管理收藏的数据集（每条可更新、删除或在对话中使用）。追踪只存在这台电脑上；在对话里打开追踪或收藏数据集时，要点会做成上下文 chip 随消息发给你配置的 AI 服务商（配的是本地模型则不出本机）——发出前可见、可移除，随下一条消息发出后自动消失。",
        visual: "board",
        size: "wide",
    },
    {
        // （2026-08-23）：新增「我的库」介绍。与第 11 屏「出结果后可以接着改条件」衔接——
        // 那一屏讲怎么把检索「存为追踪」，这一屏讲存下来的东西去哪统一管理：左侧导航「我的库」
        // 浮窗（追踪 + 收藏双页签），只存本机浏览器。target 指向左侧常驻可见的「我的库」导航项，
        // 焦点环有明确落点；纯文字步（无 visual），一两句点到为止。
        target: "#libNav",
        title: "留下的，都进「我的库」",
        text: "前面「存为追踪」的方向，还有你点过收藏的数据集，都收在左侧导航「我的库」里，分成「追踪」「收藏」两页签：追踪管候选方向、随时能查有没有新数据，收藏管你存下来的数据集。都只在你本机浏览器，不会上传。",
        visual: "",
        size: "compact",
    },
    {
        // 2026-08-22 可选步「接进你自己的 AI 助手」；
        // 2026-08-23 起按产品方向后移至结尾并精简——走 MCP/技能包会绕开
        // react agent 侧的数据采集，不主动推；保持「完全可选，跳过不影响任何功能」措辞内核。
        // 接线不变：「复制接入提示词」走 interactions.js copyAgentPrompt 单一真源（与帮助页同实现），
        // 「下载技能包」纯 <a download> 无需 JS。
        target: "#queryInput",
        title: "接进你自己的 AI 助手",
        text: "BioData 的检索与数据管护能力能接进你常用的 AI 助手（Claude Code / Kimi Code / Codex）：到帮助页「接入 AI 助手」选一种方式，点「复制接入提示词」粘给你的助手发送，它会代你完成全部配置。本地版走本机 MCP 服务（功能最全）；网页版可直接生成令牌走在线接入（免安装）。这一步完全可选，跳过不影响任何功能。",
        visual: "agent",
        size: "medium",
    },
    {
        // 2026-08-13 起：当场开关按钮行退役，这一步只负责「指路」——打开设置抽屉、
        // 高亮「使用反馈」那一行，让用户知道以后去哪管。
        // 2026-08-21：默认本地采集、安全通道配置后上传（单版本化，无主线版分叉），
        // 教程里如实告知；导出反馈包保留作手动兜底。
        // 2026-08-22 如实化：明文 HTTP/90 天/结构性去标识口径与 consent 弹窗一致；
        // 「本机编号」行（删除已上传数据用）也在这一行下面，一并指路。
        // 2026-08-22：结构性去标识 → 尽力过滤（并非匿名化），与 consent v2 口径一致。
        target: ".usage-setting",
        title: "使用反馈，在这里管理",
        text: BENCHFB_BUILD
            ? "这个版本默认开启本地采集，每个账户独立确认；记录经尽力过滤（手机号/证件号/邮箱自动遮蔽、API Key 等直接标识删除，并非匿名化）后经明文 HTTP 上传（保留 90 天），用于改进推荐排序与制作评测基准。想关、清空本地待发记录、手动导出，或查本机编号（请求删除已上传数据时用），都在高亮这一行管理。"
            : "使用情况记录默认关着。愿意帮我们改进，就在高亮的这一行开启：只存在本机浏览器、绝不自动发送，随时可关可清空；想反馈时点「生成反馈」，把那段文字发给开发者。",
        settings: true,
        visual: "",
        size: "medium",
    },
];

let onboardingIndex = 0;
let onboardingTarget = null;
let onboardingFocusRing = null;
let onboardingDisclosureState = new Map();
let onboardingDrag = null;
let onboardingResizeStartTimer = null;
let onboardingResizeTimer = null;
let onboardingViewportClampTimer = null;
let onboardingSizeObserver = null;

function cancelOnboardingResize() {
    const surface = $("onboardingSurface");
    if (onboardingResizeStartTimer !== null) clearTimeout(onboardingResizeStartTimer);
    if (onboardingResizeTimer !== null) clearTimeout(onboardingResizeTimer);
    onboardingResizeStartTimer = null;
    onboardingResizeTimer = null;
    if (!surface) return;
    surface.style.removeProperty("transition");
    surface.style.removeProperty("transform");
    surface.style.removeProperty("will-change");
}

function resetOnboardingPosition() {
    const panel = $("onboarding");
    if (!panel) return;
    cancelOnboardingResize();
    if (onboardingViewportClampTimer !== null) clearTimeout(onboardingViewportClampTimer);
    onboardingViewportClampTimer = null;
    panel.classList.remove("onboarding-dragging");
    ["left", "right", "top", "bottom", "width", "transform"].forEach((name) => panel.style.removeProperty(name));
    onboardingDrag = null;
}

function placeDraggedOnboarding(left, top, width, height) {
    const panel = $("onboarding");
    const edge = 12;
    if (window.innerWidth <= 780) {
        const maxTop = Math.max(edge, window.innerHeight - height - edge);
        panel.style.left = `${edge}px`;
        panel.style.right = `${edge}px`;
        panel.style.top = `${Math.round(Math.max(edge, Math.min(maxTop, top)))}px`;
        panel.style.bottom = "auto";
        panel.style.transform = "none";
        return;
    }
    const maxLeft = Math.max(edge, window.innerWidth - width - edge);
    const maxTop = Math.max(edge, window.innerHeight - height - edge);
    panel.style.left = `${Math.round(Math.max(edge, Math.min(maxLeft, left)))}px`;
    panel.style.top = `${Math.round(Math.max(edge, Math.min(maxTop, top)))}px`;
    panel.style.right = "auto";
    panel.style.bottom = "auto";
    panel.style.transform = "none";
}

function clampOnboardingPosition() {
    const panel = $("onboarding");
    if (!panel || panel.hidden || !panel.style.left.endsWith("px")) return;
    const rect = panel.getBoundingClientRect();
    placeDraggedOnboarding(rect.left, rect.top, rect.width, rect.height);
}

function initOnboardingDrag() {
    const panel = $("onboarding"), handle = $("onboardingDragHandle");
    if (!panel || !handle || handle.dataset.dragBound) return;
    handle.dataset.dragBound = "1";
    handle.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || event.target.closest("button")) return;
        const rect = panel.getBoundingClientRect();
        onboardingDrag = { pointerId: event.pointerId, dx: event.clientX - rect.left, dy: event.clientY - rect.top, width: rect.width, height: rect.height };
        placeDraggedOnboarding(rect.left, rect.top, rect.width, rect.height);
        panel.classList.add("onboarding-dragging");
        handle.setPointerCapture(event.pointerId);
        event.preventDefault();
    });
    handle.addEventListener("pointermove", (event) => {
        if (!onboardingDrag || event.pointerId !== onboardingDrag.pointerId) return;
        placeDraggedOnboarding(event.clientX - onboardingDrag.dx, event.clientY - onboardingDrag.dy, onboardingDrag.width, onboardingDrag.height);
    });
    const finish = (event) => {
        if (!onboardingDrag || (event && typeof event.pointerId === "number" && event.pointerId !== onboardingDrag.pointerId)) return;
        onboardingDrag = null;
        panel.classList.remove("onboarding-dragging");
    };
    handle.addEventListener("pointerup", finish);
    handle.addEventListener("pointercancel", finish);
    handle.addEventListener("lostpointercapture", finish);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
    window.addEventListener("blur", () => finish(null));
    // 手柄可键盘聚焦 + 方向键微移（复用 placeDraggedOnboarding 的 12px 视口钳制），
    // 让不能用指针的用户也能把面板移开被它压住的高亮控件。
    if (!handle.hasAttribute("tabindex")) handle.setAttribute("tabindex", "0");
    if (!handle.getAttribute("role")) handle.setAttribute("role", "button");
    if (!handle.getAttribute("aria-label")) handle.setAttribute("aria-label", "移动教程面板（可用方向键微移）");
    handle.addEventListener("keydown", (event) => {
        let dxk = 0, dyk = 0;
        if (event.key === "ArrowLeft") dxk = -12;
        else if (event.key === "ArrowRight") dxk = 12;
        else if (event.key === "ArrowUp") dyk = -12;
        else if (event.key === "ArrowDown") dyk = 12;
        else return;
        event.preventDefault();
        const rect = panel.getBoundingClientRect();
        placeDraggedOnboarding(rect.left + dxk, rect.top + dyk, rect.width, rect.height);
        syncOnboardingFocusRing();
    });
}

function initOnboardingSizeObserver() {
    const panel = $("onboarding");
    if (!panel || onboardingSizeObserver || typeof ResizeObserver !== "function") return;
    onboardingSizeObserver = new ResizeObserver(() => {
        clampOnboardingPosition();
        syncOnboardingFocusRing();
    });
    onboardingSizeObserver.observe(panel);
}

function ensureOnboardingFocusRing() {
    if (onboardingFocusRing && document.body.contains(onboardingFocusRing)) return onboardingFocusRing;
    onboardingFocusRing = document.createElement("div");
    onboardingFocusRing.className = "onboarding-focus-ring";
    onboardingFocusRing.setAttribute("aria-hidden", "true");
    onboardingFocusRing.hidden = true;
    document.body.appendChild(onboardingFocusRing);
    return onboardingFocusRing;
}

function syncOnboardingFocusRing() {
    const ring = ensureOnboardingFocusRing();
    const panel = $("onboarding");
    if (!onboardingTarget || !panel || panel.hidden) {
        ring.hidden = true;
        return;
    }
    const rect = onboardingTarget.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) {
        ring.hidden = true;
        return;
    }
    const gap = 5;
    const edge = 4;
    const left = Math.max(edge, rect.left - gap);
    const top = Math.max(edge, rect.top - gap);
    const right = Math.min(window.innerWidth - edge, rect.right + gap);
    const bottom = Math.min(window.innerHeight - edge, rect.bottom + gap);
    const radius = parseFloat(getComputedStyle(onboardingTarget).borderTopLeftRadius) || 6;
    ring.style.left = `${Math.round(left)}px`;
    ring.style.top = `${Math.round(top)}px`;
    ring.style.width = `${Math.max(0, Math.round(right - left))}px`;
    ring.style.height = `${Math.max(0, Math.round(bottom - top))}px`;
    ring.style.borderRadius = `${Math.max(8, Math.round(radius + gap))}px`;
    ring.hidden = false;
}

function clearOnboardingTarget() {
    if (onboardingTarget) onboardingTarget.classList.remove("onboarding-focus");
    onboardingTarget = null;
    if (onboardingFocusRing) onboardingFocusRing.hidden = true;
}

function captureOnboardingDisclosures() {
    onboardingDisclosureState = new Map();
    ONBOARDING_STEPS.forEach((step) => {
        if (!step.disclosure || onboardingDisclosureState.has(step.disclosure)) return;
        const disclosure = document.querySelector(step.disclosure);
        if (disclosure) onboardingDisclosureState.set(step.disclosure, !!disclosure.open);
    });
}

function syncOnboardingDisclosures(step) {
    onboardingDisclosureState.forEach((wasOpen, selector) => {
        const disclosure = document.querySelector(selector);
        if (disclosure) disclosure.open = step.disclosure === selector ? true : wasOpen;
    });
}

function restoreOnboardingDisclosures() {
    onboardingDisclosureState.forEach((wasOpen, selector) => {
        const disclosure = document.querySelector(selector);
        if (disclosure) disclosure.open = wasOpen;
    });
}

function renderOnboardingVisual(step) {
    const host = $("onboardingVisuals");
    let hasActiveVisual = false;
    host.querySelectorAll("[data-onboarding-visual]").forEach((visual) => {
        const active = visual.dataset.onboardingVisual === step.visual;
        visual.hidden = !active;
        if (active) hasActiveVisual = true;
    });
    host.hidden = !hasActiveVisual;
    syncTourProviderPressed();
}

/* 教程卡里的服务商按钮接通右侧真实表单。按钮 data 值与 #cfgProvider 的 option value 严格同源；
   aria-pressed 只作视觉选中态，真实状态以 select 为准——渲染 provider 视觉时回读同步，重播/翻页也不漂。 */
function syncTourProviderPressed() {
    const provider = $("cfgProvider");
    const host = $("onboardingVisuals");
    if (!provider || !host) return;
    host.querySelectorAll("[data-tour-provider]").forEach((btn) => {
        btn.setAttribute("aria-pressed", btn.dataset.tourProvider === provider.value ? "true" : "false");
    });
}

/* 详情页实拍图加载失败时优雅兜底——一次性 error 监听隐藏图、显示文字说明（如实列六个子标签）。
   项目无内联 onerror 约定，故在初始化时挂监听，不回退到 HTML 内联脚本。 */
function initOnboardingDetailFallback() {
    const img = document.querySelector(".tour-detail img");
    const fallback = document.querySelector(".tour-detail-fallback");
    if (!img || !fallback) return;
    img.addEventListener("error", () => {
        img.hidden = true;
        fallback.hidden = false;
    }, { once: true });
}

/* 2026-08-13：教程里的「当场选择」机器（choice 按钮行）已退役——使用反馈的默认态
   按版本分叉（强化版默认开、主线版默认关），第 9 步改为指路设置里的管理入口。
   DOM 行与样式一并移除；若未来复活当场决策，从这里补回即可。 */

// 实时读「减弱动效」偏好：REDUCE_MOTION 是加载时取一次的常量，会话中途在系统里开启对本模块的
// 自定义 JS FLIP 缩放不生效；这里每次实时查 matchMedia，让中途开启也能立即抑制动画。
function onboardingReduceMotion() {
    try { return REDUCE_MOTION || window.matchMedia("(prefers-reduced-motion: reduce)").matches; }
    catch (_e) { return REDUCE_MOTION; }
}

function animateOnboardingResize(panel, previousRect) {
    const surface = $("onboardingSurface");
    if (!surface || !previousRect || onboardingReduceMotion()) return;
    const nextRect = panel.getBoundingClientRect();
    if (!nextRect.width || !nextRect.height) return;
    const dx = previousRect.left - nextRect.left;
    const dy = previousRect.top - nextRect.top;
    const scaleX = previousRect.width / nextRect.width;
    const scaleY = previousRect.height / nextRect.height;
    if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5 && Math.abs(scaleX - 1) < 0.005 && Math.abs(scaleY - 1) < 0.005) return;
    cancelOnboardingResize();
    surface.style.willChange = "transform";
    surface.style.transition = "none";
    surface.style.transform = `translate(${dx}px, ${dy}px) scale(${scaleX}, ${scaleY})`;
    surface.getBoundingClientRect();
    onboardingResizeStartTimer = setTimeout(() => {
        onboardingResizeStartTimer = null;
        surface.style.transition = "transform 240ms cubic-bezier(.22, 1, .36, 1)";
        surface.style.transform = "none";
        onboardingResizeTimer = setTimeout(() => {
            onboardingResizeTimer = null;
            surface.style.removeProperty("transition");
            surface.style.removeProperty("transform");
            surface.style.removeProperty("will-change");
            clampOnboardingPosition();
            syncOnboardingFocusRing();
        }, 260);
    }, 16);
}

function setOnboardingDone() {
    try { localStorage.setItem(LS.onboarding, "done"); } catch (_e) {}
}

function stopOnboarding(markDone) {
    clearOnboardingTarget();
    restoreOnboardingDisclosures();
    const panel = $("onboarding");
    if (panel) {
        cancelOnboardingResize();
        panel.hidden = true;
        panel.classList.remove("onboarding-settings");
    }
    if ($("settings").classList.contains("open")) closeSettings({ returnFocus: false });
    if (markDone) setOnboardingDone();
}

function showOnboardingStep(index) {
    onboardingIndex = Math.max(0, Math.min(ONBOARDING_STEPS.length - 1, index));
    const step = ONBOARDING_STEPS[onboardingIndex];
    const panel = $("onboarding");
    const previousRect = panel.hidden ? null : panel.getBoundingClientRect();
    panel.dataset.size = step.size || "compact";
    panel.classList.toggle("onboarding-settings", !!step.settings);
    clearOnboardingTarget();
    syncOnboardingDisclosures(step);
    if (step.settings) {
        openSettings({ moveFocus: false });
        const body = $("settings").querySelector(".drawer-body");
        if (step.top && body) body.scrollTop = 0;
    } else if ($("settings").classList.contains("open")) {
        closeSettings({ returnFocus: false });
    }

    const target = document.querySelector(step.target);
    if (target) {
        onboardingTarget = target;
        target.classList.add("onboarding-focus");
        if ((step.settings || window.innerWidth <= 780) && typeof target.scrollIntoView === "function") {
            // 窄屏把目标留在底部教程框上方；设置步骤先等抽屉就位，再校正一次。
            const block = window.innerWidth <= 780 ? "start" : "center";
            target.scrollIntoView({ block, behavior: "auto" });
            if (step.settings) {
                const scheduledIndex = onboardingIndex;
                setTimeout(() => {
                    if (onboardingIndex === scheduledIndex && onboardingTarget === target && !panel.hidden) {
                        if (!REDUCE_MOTION) target.scrollIntoView({ block, behavior: "auto" });
                        syncOnboardingFocusRing();
                    }
                }, 260);
            }
        }
    }
    $("onboardingProgress").textContent = `${onboardingIndex + 1} / ${ONBOARDING_STEPS.length}`;
    // 第 0 步 true 分支经 additive 字段 titleAccent/boldText 挂样式类（标题主题色 / 正文加粗），
    // 其余步骤字段缺省 → toggle 强拆类，视觉不变。
    $("onboardingTitle").classList.toggle("onboarding-title-accent", !!step.titleAccent);
    $("onboardingText").classList.toggle("onboarding-text-bold", !!step.boldText);
    $("onboardingTitle").textContent = step.title;
    $("onboardingText").textContent = step.text;
    renderOnboardingVisual(step);
    $("onboardingBack").disabled = onboardingIndex === 0;
    $("onboardingNext").textContent = onboardingIndex === ONBOARDING_STEPS.length - 1 ? "开始使用" : (step.nextLabel || "下一步");
    panel.hidden = false;
    clampOnboardingPosition();
    animateOnboardingResize(panel, previousRect);
    requestAnimationFrame(syncOnboardingFocusRing);
    if (step.focusTarget) {
        const scheduledIndex = onboardingIndex;
        setTimeout(() => {
            const field = document.querySelector(step.focusTarget);
            if (field && onboardingIndex === scheduledIndex && !panel.hidden) field.focus({ preventScroll: true });
        }, 280);
    } else {
        $("onboardingNext").focus({ preventScroll: true });
    }
}

function startOnboarding() {
    showView("query");
    captureOnboardingDisclosures();
    resetOnboardingPosition();
    onboardingIndex = 0;
    showOnboardingStep(0);
}

function maybeStartOnboarding() {
    try { if (localStorage.getItem(LS.onboarding)) return; } catch (_e) { return; }
    setTimeout(() => {
        // 700ms 内用户已经自己动手（输入了内容或焦点落在表单控件上）→ 放弃自动弹出，
        // 不在人家输入中途抢焦点。不写 LS 标记：下次访问仍会尝试，也可点「重播教程」手动看。
        const q = $("queryInput");
        const active = document.activeElement;
        if ((q && q.value.trim()) || (active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName))) return;
        startOnboarding();
    }, 700);
}

export function initOnboarding() {
    ensureOnboardingFocusRing();
    initOnboardingDrag();
    initOnboardingSizeObserver();
    $("onboardingClose").addEventListener("click", () => stopOnboarding(true));
    $("onboardingSkip").addEventListener("click", () => stopOnboarding(true));
    $("onboardingBack").addEventListener("click", () => showOnboardingStep(onboardingIndex - 1));
    $("onboardingNext").addEventListener("click", () => {
        if (onboardingIndex >= ONBOARDING_STEPS.length - 1) stopOnboarding(true);
        else showOnboardingStep(onboardingIndex + 1);
    });
    // 事件委托——点教程卡里的服务商按钮，等于在右侧真实设置里切换 provider：
    // 设 select 值 + dispatch change → interactions.js 的 applyPreset 自动填好接口地址和模型名，
    // 然后同步按钮选中态、把焦点移到 API Key，让用户紧接着就能填 Key。
    $("onboardingVisuals").addEventListener("click", (event) => {
        // 教程「接进你自己的 AI 助手」步：教程内「复制接入提示词」与帮助页同一实现
        //（copyAgentPrompt 单一真源）。
        const agentBtn = event.target.closest("#tourAgentPromptCopyBtn");
        if (agentBtn) { copyAgentPrompt(agentBtn); return; }
        const btn = event.target.closest("[data-tour-provider]");
        if (!btn) return;
        const provider = $("cfgProvider");
        if (!provider) return;
        provider.value = btn.dataset.tourProvider;
        provider.dispatchEvent(new Event("change", { bubbles: true }));
        syncTourProviderPressed();
        const key = $("cfgApiKey");
        if (key) key.focus({ preventScroll: true });
    });
    initOnboardingDetailFallback();
    const replay = $("tutorialReplay");
    if (replay) replay.addEventListener("click", startOnboarding);
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !$("onboarding").hidden) stopOnboarding(true);
    });
    window.addEventListener("resize", () => {
        clampOnboardingPosition();
        syncOnboardingFocusRing();
        if (onboardingViewportClampTimer !== null) clearTimeout(onboardingViewportClampTimer);
        onboardingViewportClampTimer = setTimeout(() => {
            onboardingViewportClampTimer = null;
            clampOnboardingPosition();
            syncOnboardingFocusRing();
        }, 80);
    });
    document.addEventListener("scroll", syncOnboardingFocusRing, true);
    maybeStartOnboarding();
}
