"use strict";

/* 启动收口（ES Module，全应用唯一入口）：DOMContentLoaded 时平铺串调各模块的
   init/sync。每个名字都从属主模块 import，不再靠共享全局运行时解析——某个模块把函数改名/删了，
   import 在加载期就炸（tests/test_frontend_boot_contract.py 静态双端核对同一个不变量）。
   init() 函数体保持原顺序原样：加载序契约（core 最先、boot 最后）与初始化顺序都不变。 */
import { loadDomainManifest } from "#core";
import { loadConfig, initFabTuck, initSidebar, initSidebarResize, initStrategyTooltips, showView,
    initLocalModelControl, syncAiGates, syncAgentAvailability, syncStrategyNode, initLibWin, initHistWinSkeleton } from "#shell";
import { bind, playHero, renderHeroGreeting, initHeroRot, initTimeFilter, initTimeSelector, initSourceSelector, initSourceChips,
    setSourcesReady } from "#interactions";
import { initAccounts } from "#accounts";
import { initUserMemory } from "#memory";
import { initDream } from "#dream";
import { initUsage } from "#usage_log";
import { initBenchfb } from "#benchfb";
import { initCondBoard } from "#board";
import { initTaskPack } from "#task_pack";
import { initDownloads } from "#downloads";
import { initAct } from "#act";
import { initHistWin } from "#browse";
import { initOnboarding } from "#onboarding";
import { initProjects } from "#projects";

async function init() {
    loadConfig(); initSidebar(); initSidebarResize(); initStrategyTooltips();
    // telemetry namespace 必须等 whoami 落定；否则启动上传/首轮打点会随机落匿名或登录账户。
    await initAccounts();
    bind(); initUserMemory(); initDream(); initUsage(); initBenchfb(); initLocalModelControl(); syncAiGates(); showView("query");
    renderHeroGreeting(); initHeroRot(); playHero(); initTimeFilter(); initTimeSelector(); initSourceSelector();
    setSourcesReady(initSourceChips()); initCondBoard(); initTaskPack(); initDownloads(); initAct(); initLibWin(); initHistWinSkeleton(); initHistWin();
    initOnboarding(); initFabTuck(); syncAgentAvailability();
    // 页面标题随活动领域包（biodata 默认与静态 <title> 逐位一致；fire-and-forget 不阻塞启动）。
    loadDomainManifest().then((m) => {
        const t = m && m.branding && m.branding.page_title;
        t && (document.title = t);
    });
    initProjects();   // 追踪浮窗/存为追踪/上下文 chip（全 DOM 自守卫）
}
document.addEventListener("DOMContentLoaded", init);
