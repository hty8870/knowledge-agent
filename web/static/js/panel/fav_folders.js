"use strict";
/* 收藏夹 UI —— 「我的收藏」的分组能力。
 *
 * 三个部件：
 *   1. 心形 popover：未收藏 → 选夹收藏（含行内新建）；已收藏 → 取消 / 移到其它夹。
 *      轻量浮层，不进 modal 焦点体系；Esc / 点外 / 滚动 / 缩放即关闭且无副作用。
 *   2. 收藏视图 tab 条：[全部][默认收藏夹][各用户夹] +「管理收藏夹」按钮。
 *      tab 选中态不持久化——每次进视图、换账户都复位为「全部」（resetFavFolderState）。
 *   3. 管理面板：用户夹行内重命名 + 二段确认删除（仿 histClear armed 模式）+ 新建行。
 *      默认夹是内置概念，不可改名/删除；删夹后条目归回默认夹。
 *
 * 隐私约束：全部只读写 localStorage（经 core.js 的 per-account 键与 CRUD），
 * 本文件不产生任何网络请求；收藏夹名等任何内容都不会进入复用清单的 POST 体（keys-only）。
 *
 * 本文件是 ES Module：core/cards 经 import 取。cards/dataset_page（openFavPopover）、
 * shell/accounts（resetFavFolderState）、browse（renderFavFolderBar/renderFavFolderGroups/setFavRerender）、
 * interactions（toggleFavFolderManage）经 import 取本文件导出（绞杀桥已全退役）。
 *
 * 2026-08-10：原「import { renderFavorites } from "#browse"」与 browse→fav_folders
 * 的 import 互调成环。沿 core.js setHistHooks 同一范式改注册反转：browse 在模块求值期把
 * renderFavorites 注册进来，本文件只在函数体内调 _fireFavRerender，环结构性消失；未注册时
 * （dataset.html 等不加载 browse 的页面——那里本就没有收藏视图，各调用点本来也不可达）
 * 空值守卫安全空转。
 */
import { DEFAULT_FAV_FOLDER_NAME, MOTION, $, addFavFolder, deleteFavFolder, escapeHtml, favFolderIdOrDefault, favFolderNameById, favFolderOf, fastqInfo, getFavFolders, getFavs, isFav, itemKey, moveFavToFolder, renameFavFolder, setFavs, toast, toggleFav, normalizeItem } from "#core";
import { buildCard } from "#cards";
import { COPY, armTwoStepConfirm } from "../core/copy.js";

/* 收藏视图重渲钩子（browse.js 模块求值期经 setFavRerender 注册它的 renderFavorites）。 */
let _rerenderFavs = null;
export function setFavRerender(fn) { _rerenderFavs = typeof fn === "function" ? fn : null; }
function _fireFavRerender() { if (_rerenderFavs) _rerenderFavs(); }
/* 取消收藏成功的 toast：popover 与确认按钮两处共用，字面量只留一份。 */
const FAV_UNFAVED_COPY = "已取消收藏";
/* 收藏「在对话中使用」→ 设置数据集上下文 chip。projects.js 经 setCtxDataSetHandler 注册
   （注册反转防 fav_folders→projects 新环——projects→browse→fav_folders 已存在，直连 projects 成环）。 */
let _ctxSetData = null;
export function setCtxDataSetHandler(fn) { _ctxSetData = typeof fn === "function" ? fn : null; }
function _ctxSetDataset(card) { if (_ctxSetData) _ctxSetData(card); }
/* 收藏从一级视图迁进「我的库」浮窗收藏页签——「是否在收藏视图」改判本机状态（libWin 开 + favs 页签）。 */
function _inLibFavs() { const w = $("libWin"); return !!(w && !w.hidden && w.dataset.libActive === "favs"); }
/* 目录三态访问器（browse.js 模块求值期经 setCatalogLookup 注册——regist 反转防 browse↔fav_folders 环回退，
   与收藏视图重渲钩子同范式）。未注册时（dataset.html 等不加载 browse 的页面）空值守卫安全空转。 */
let _catalogLookup = null;
export function setCatalogLookup(fn) { _catalogLookup = typeof fn === "function" ? fn : null; }
function _catalogOf(uid) { try { return _catalogLookup ? _catalogLookup(uid) : null; } catch (_e) { return null; } }
/* 目录加载器同范式注册（browse.js ensureDatasetsLoaded）——「更新」遇 load_error 先拉目录再重查。 */
let _catalogEnsure = null;
export function setCatalogEnsure(fn) { _catalogEnsure = typeof fn === "function" ? fn : null; }

let _favPopover = null;       // 当前打开的心形 popover 元素（同时只开一个；null = 未打开）
let _favViewTab = null;       // 收藏视图当前 tab：null=全部；""=默认收藏夹；否则用户夹 id。不持久化
let _favManageOpen = false;   // 「管理收藏夹」面板展开态。不持久化

/* ---------- 心形 popover ---------- */

function closeFavPopover() {
    if (!_favPopover) return;
    _favPopover.remove();
    _favPopover = null;
    document.removeEventListener("click", _onFavPopoverDocClick, true);
    document.removeEventListener("keydown", _onFavPopoverKeydown, true);
    window.removeEventListener("resize", _onFavPopoverPassiveClose);
    document.removeEventListener("scroll", _onFavPopoverPassiveClose, true);
}
function _onFavPopoverDocClick(e) { if (_favPopover && !_favPopover.contains(e.target)) closeFavPopover(); }
function _onFavPopoverKeydown(e) {
    if (e.key !== "Escape" || !_favPopover) return;
    e.stopPropagation();   // Esc 只关 popover，不连带触发 modal/抽屉的 Esc 收口
    closeFavPopover();
}
function _onFavPopoverPassiveClose() { closeFavPopover(); }   // 滚动/缩放后锚点已漂移，直接收起

function _positionFavPopover(pop, anchorBtn) {
    const r = anchorBtn.getBoundingClientRect();
    pop.style.visibility = "hidden";
    document.body.appendChild(pop);
    const pw = pop.offsetWidth, ph = pop.offsetHeight;
    // 心形在卡片右上角：popover 右对齐心形、向左展开；左右各留 8px 防溢出
    let left = Math.max(8, Math.min(r.right - pw, window.innerWidth - pw - 8));
    let top = r.bottom + 6;
    if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 6);   // 下方不够则翻到心形上方
    pop.style.left = Math.round(left) + "px";
    pop.style.top = Math.round(top) + "px";
    pop.style.visibility = "";
}

/* 点心形（未收藏）→ 选夹：默认夹置顶并默认高亮（焦点落在它上面，Enter 直选 = 老手感）；
   底部行内输入，Enter 建夹并直接收藏进新夹。 */
function _renderFavPopoverPick(pop, anchorBtn, it) {
    const folders = getFavFolders();
    pop.innerHTML =
        `<div class="fav-pop-title">收藏到…</div>`
        + [`<button class="fav-pop-item fav-pop-default" type="button" data-folder="">默认收藏夹</button>`]
            .concat(folders.map((f) => `<button class="fav-pop-item" type="button" data-folder="${escapeHtml(f.id)}">${escapeHtml(f.name)}</button>`))
            .join("")
        + `<div class="fav-pop-new"><input class="fav-pop-new-input" type="text" maxlength="20" placeholder="＋ 新建收藏夹" aria-label="新建收藏夹名字"></div>`;
    pop.querySelectorAll(".fav-pop-item").forEach((b) => b.addEventListener("click", () => {
        _favPopoverAdd(anchorBtn, it, b.dataset.folder || "");
    }));
    const input = pop.querySelector(".fav-pop-new-input");
    input.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" || e.isComposing) return;   // 中文输入法组词中的 Enter 是上屏，不建仓
        e.preventDefault();
        const r = addFavFolder(input.value);
        if (!r.ok) { toast(r.error); input.focus(); return; }
        _favPopoverAdd(anchorBtn, it, r.folder.id);
    });
    // `preventScroll` 不是可有可无的：本 popover 在**任何**滚动上自动收起（锚点已漂移）。
    // 裸 focus() 会把默认项滚进视口 → 触发 scroll → popover 刚开就把自己关了。
    // 心形靠近视口边缘时必然复现；2026-07-29 的真机验收里正是这么扑空的。
    setTimeout(() => { const d = pop.querySelector(".fav-pop-default"); if (d) d.focus({ preventScroll: true }); }, 0);
}

/* 播一次收藏动效：先摘旧类再强制回流，否则连点两次第二次不会重放（同名 animation 不会重启）。
   动画结束（或 500ms 兜底，`animationend` 在 reduced-motion 下不会来）自行摘类 → 心形可反复触发。
   纯 CSS 动画，不经 MOTION 门控：它不依赖 GSAP，`prefers-reduced-motion` 由 app.css 的媒体查询关掉。 */
function _favFlash(el, cls) {
    if (!el) return;
    el.classList.remove("fav-pop", "fav-unpop", "fav-landed");
    void el.offsetWidth;   // 强制回流：让浏览器认为这是一次全新的动画
    el.classList.add(cls);
    const done = () => el.classList.remove(cls);
    el.addEventListener("animationend", done, { once: true });
    setTimeout(done, 900);
}
/* 收藏落地后，「我的库」导航入口也弹一下——心形自己弹只说明「点到了」，
   这一下才说明**它去了哪里**。收藏页签里没有导航动作也无妨（找不到就跳过）。 */
function _favNavLanded() {
    _favFlash(document.querySelector('.nav-item[data-view="lib"]'), "fav-landed");
}

function _favPopoverAdd(anchorBtn, it, folderId) {
    toggleFav(it, folderId, anchorBtn);   // anchorBtn 供遥测算名次（fav 事件带 uid/pos）
    anchorBtn.classList.add("active");
    _favFlash(anchorBtn, "fav-pop");
    _favNavLanded();
    toast(`已收藏到「${favFolderNameById(folderId)}」`);
    closeFavPopover();
    if (_inLibFavs()) _fireFavRerender();
}

/* 点心形（已收藏）→ 菜单：「取消收藏」+「移到收藏夹」分组（当前夹打勾）。 */
function _renderFavPopoverManage(pop, anchorBtn, it) {
    const k = itemKey(it);
    const cur = getFavs().find((f) => itemKey(f) === k);
    const curFolder = favFolderIdOrDefault(favFolderOf(cur));
    const opts = [{ id: "", name: DEFAULT_FAV_FOLDER_NAME }].concat(getFavFolders());
    pop.innerHTML =
        `<button class="fav-pop-item fav-pop-unfav" type="button">取消收藏</button>`
        + `<div class="fav-pop-title">移到收藏夹</div>`
        + opts.map((f) => `<button class="fav-pop-item" type="button" data-folder="${escapeHtml(f.id)}">`
            + (f.id === curFolder ? `<span class="fav-pop-check" aria-hidden="true">✓</span>` : "")
            + `${escapeHtml(f.name)}</button>`).join("");
    pop.querySelector(".fav-pop-unfav").addEventListener("click", () => {
        toggleFav(it, undefined, anchorBtn);   // 取消收藏当前不打点；anchorBtn 传入保持签名一致
        anchorBtn.classList.remove("active");
        _favFlash(anchorBtn, "fav-unpop");   // 回落一下；**不给光环**——光环是「加入成功」的语汇
        toast(FAV_UNFAVED_COPY);
        closeFavPopover();
        // 就在收藏视图里取消：让这张卡自己退场再重排，而不是整格瞬间跳变（用户会怀疑点错了哪张）。
        // 退场动效走 GSAP（要在动画结束后才重排，纯 CSS 拿不到这个回调）→ 经 MOTION 门控，
        // 无 GSAP / 减弱动效时直接重排，行为与从前逐位相同。
        if (!_inLibFavs()) return;
        const card = anchorBtn.closest(".card");
        if (!card || !MOTION) { _fireFavRerender(); return; }
        gsap.to(card, { autoAlpha: 0, y: -8, scale: 0.97, duration: 0.2, ease: "power2.in", onComplete: _fireFavRerender });
    });
    pop.querySelectorAll(".fav-pop-item[data-folder]").forEach((b) => b.addEventListener("click", () => {
        const fid = b.dataset.folder || "";
        if (fid !== curFolder) { moveFavToFolder(it, fid); toast(`已移到「${favFolderNameById(fid)}」`); }
        closeFavPopover();
        if (_inLibFavs()) _fireFavRerender();
    }));
}

export function openFavPopover(anchorBtn, it) {
    if (_favPopover && _favPopover._anchor === anchorBtn) { closeFavPopover(); return; }   // 再点同一心形 = 收起
    closeFavPopover();
    const pop = document.createElement("div");
    pop.className = "fav-popover";
    pop.setAttribute("role", "menu");
    pop._anchor = anchorBtn;
    if (isFav(it)) _renderFavPopoverManage(pop, anchorBtn, it);
    else _renderFavPopoverPick(pop, anchorBtn, it);
    _positionFavPopover(pop, anchorBtn);
    _favPopover = pop;
    // capture 阶段登记：本函数在爱心 click 的 target 阶段执行，capture 已过——当前这次点击不会误触关闭
    document.addEventListener("click", _onFavPopoverDocClick, true);
    document.addEventListener("keydown", _onFavPopoverKeydown, true);
    window.addEventListener("resize", _onFavPopoverPassiveClose);
    document.addEventListener("scroll", _onFavPopoverPassiveClose, true);
}

/* ---------- 收藏视图：tab 条 + 分组渲染 + 管理面板 ---------- */

/* 状态复位（换账户 / 每次进入收藏视图）：tab 回「全部」、管理面板收起、popover 关闭。
   收藏夹数据本身在 per-account 命名空间里，换账户后由渲染层按新命名空间重读。 */
export function resetFavFolderState() {
    _favViewTab = null;
    _favManageOpen = false;
    closeFavPopover();
}

export function renderFavFolderBar(favs) {
    const bar = $("favFolderBar"), tabs = $("favFolderTabs");
    if (!bar || !tabs) return;
    const folders = getFavFolders();
    if (!favs.length && !folders.length) {   // 空收藏且无用户夹：连 tab 条一起藏，保持旧空态干净
        bar.style.display = "none";
        const m = $("favFolderManage"); if (m) { m.style.display = "none"; m.innerHTML = ""; }
        return;
    }
    bar.style.display = "flex";
    // 选中的夹被删了 → tab 回落「全部」
    if (_favViewTab !== null && _favViewTab !== "" && !folders.some((f) => f.id === _favViewTab)) _favViewTab = null;
    const countOf = (fid) => favs.filter((it) => favFolderIdOrDefault(favFolderOf(it)) === fid).length;
    const tabsDef = [{ id: null, name: "全部", count: favs.length }, { id: "", name: DEFAULT_FAV_FOLDER_NAME, count: countOf("") }]
        .concat(folders.map((f) => ({ id: f.id, name: f.name, count: countOf(f.id) })));
    tabs.innerHTML = tabsDef.map((t) => {
        const on = _favViewTab === t.id;
        return `<button class="fav-tab ${on ? "active" : ""}" type="button" role="tab" aria-selected="${on ? "true" : "false"}" data-tab="${t.id === null ? "__all__" : escapeHtml(t.id)}">${escapeHtml(t.name)} <span class="fav-tab-n">${t.count}</span></button>`;
    }).join("");
    tabs.querySelectorAll(".fav-tab").forEach((b) => b.addEventListener("click", () => {
        const v = b.dataset.tab;
        _favViewTab = v === "__all__" ? null : v;
        _fireFavRerender();
    }));
    renderFavFolderManage();
}

export function toggleFavFolderManage() {
    _favManageOpen = !_favManageOpen;
    renderFavFolderManage();
}

/* 管理面板：默认夹只读行（不可改名/删除）+ 每个用户夹一行（行内重命名、二段确认删除）+ 新建行。 */
function renderFavFolderManage() {
    const panel = $("favFolderManage");
    if (!panel) return;
    if (!_favManageOpen) { panel.style.display = "none"; panel.innerHTML = ""; return; }
    const folders = getFavFolders();
    panel.style.display = "block";
    panel.innerHTML =
        `<div class="fav-mng-row"><span class="fav-mng-name">默认收藏夹</span><span class="fav-mng-hint">内置，不可改名 / 删除；删除其他收藏夹时，里面的条目会回到这里</span></div>`
        + folders.map((f) =>
            `<div class="fav-mng-row" data-id="${escapeHtml(f.id)}">`
            + `<input class="fav-mng-rename" type="text" maxlength="20" value="${escapeHtml(f.name)}" aria-label="重命名收藏夹">`
            + `<button class="btn fav-mng-del" type="button">删除</button></div>`).join("")
        + `<div class="fav-mng-row fav-mng-new"><input class="fav-mng-new-input" type="text" maxlength="20" placeholder="新收藏夹名字（最多 20 字）" aria-label="新收藏夹名字">`
        + `<button class="btn fav-mng-add" type="button">新建收藏夹</button></div>`;

    panel.querySelectorAll(".fav-mng-row[data-id]").forEach((row) => {
        const id = row.dataset.id;
        const input = row.querySelector(".fav-mng-rename");
        const commit = () => {
            const orig = getFavFolders().find((f) => f.id === id);
            if (!orig) return;
            if (input.value.trim() === orig.name) return;   // 没改动：blur 不打扰
            const r = renameFavFolder(id, input.value);
            if (!r.ok) { toast(r.error); input.value = orig.name; return; }
            toast(`已重命名为「${input.value.trim()}」`);
            _fireFavRerender();
        };
        input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); input.blur(); } });
        input.addEventListener("blur", commit);
        // 删除二段确认（仿 histClear 的 armed 模式）：3 秒内再点才执行，超时自动复位
        const del = row.querySelector(".fav-mng-del");
        del.addEventListener("click", () => {
            if (!armTwoStepConfirm(del, { idleText: COPY.common.delete })) return;
            const name = favFolderNameById(id);
            deleteFavFolder(id);
            if (_favViewTab === id) _favViewTab = null;
            toast(`已删除收藏夹「${name}」，条目已归到默认收藏夹`);
            _fireFavRerender();
        });
    });

    const newInput = panel.querySelector(".fav-mng-new-input");
    const doAdd = () => {
        const r = addFavFolder(newInput.value);
        if (!r.ok) { toast(r.error); newInput.focus(); return; }
        toast(`已新建收藏夹「${r.folder.name}」`);
        _fireFavRerender();
    };
    panel.querySelector(".fav-mng-add").addEventListener("click", doAdd);
    newInput.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); doAdd(); } });
}

/* 收藏卡片运维条（视觉 spec §4.1：卡片级动作用文字 .btn——更新 / 在对话中使用 / 删除）：
   由调用方 buildCard 外包 .lib-card-wrap；**不动卡片工厂 API**（buildCard 只加 variant:"library"）。 */
function _favCardNode(it) {
    const wrap = document.createElement("div");
    wrap.className = "lib-card-wrap";
    wrap.dataset.uid = String(it.dataset_uid || "");
    wrap.appendChild(buildCard(it, { variant: "library" }));
    const acts = document.createElement("div");
    acts.className = "card-acts";
    acts.innerHTML = '<button class="btn" type="button" data-fav-update>更新</button>'
        + '<button class="btn" type="button" data-fav-use>在对话中使用</button>'
        + '<button class="btn card-act-del" type="button" data-fav-del>删除</button>';
    wrap.appendChild(acts);
    const up = acts.querySelector("[data-fav-update]");
    if (up) up.addEventListener("click", (e) => { e.stopPropagation(); _favUpdate(it, up); });
    const use = acts.querySelector("[data-fav-use]");
    if (use) use.addEventListener("click", (e) => { e.stopPropagation(); _favUse(it); });
    const del = acts.querySelector("[data-fav-del]");
    if (del) del.addEventListener("click", (e) => { e.stopPropagation(); _favDel(it, del); });
    return wrap;
}
/* 收藏「更新」（设计 §4 独立指纹）：{uid, title, sample_size, raw_data_status}。found 且指纹变 →
   重渲该卡 + toast「已更新」；指纹同 → toast「已是最新」；not_found → 「已下架」；load_error → 「目录未加载」。
   load_error 先经注册的加载器拉一次目录再重查（没进过浏览视图时目录本就没加载，
   直接 toast 会让「稍后重试」永远落空）；拉取在途给按钮「更新中…」忙态（视觉 spec §4.2）。 */
async function _favUpdate(it, btn) {
    let lookup = _catalogOf(it.dataset_uid || "");
    if (!lookup) return;
    if (lookup.status === "load_error" && _catalogEnsure) {
        if (btn) { btn.disabled = true; btn.textContent = "更新中…"; }
        try { await _catalogEnsure(); } catch (_e) { /* 失败由下方重查的 load_error 分支如实提示 */ }
        if (btn) { btn.disabled = false; btn.textContent = "更新"; }
        lookup = _catalogOf(it.dataset_uid || "");
    }
    if (!lookup) return;
    if (lookup.status === "load_error") { toast("目录未加载，请稍后重试"); return; }
    if (lookup.status === "not_found") { toast("已下架：该数据集已不在目录中"); _markGone(btn, it); return; }
    const cur = normalizeItem(lookup.item);
    /* 两处修正：
       ① raw_data_status 走 fastqInfo 语义口径——结果页收藏的条目存的是后端旧式 emoji 串
          （「✅ 包含 FASTQ」），目录侧是「含 FASTQ」，裸比字符串会每次误报「已更新」；
       ② 指纹变要把目录新值落回收藏条目（按 uid 定位、无 uid 回退 itemKey，设计 §4 身份口径；
          folder 是收藏侧专属字段，merge 后保留原值）——光 toast 不写记录，重渲的卡还是旧数据，
          且同一条每点必报「已更新」（狼来了）。 */
    const fp = [it.dataset_uid, it.dataset_name, it.sample_size, fastqInfo(it.raw_data_status).label].join("|");
    const newFp = [cur.dataset_uid, cur.dataset_name, cur.sample_size, fastqInfo(cur.raw_data_status).label].join("|");
    if (fp !== newFp) {
        const favs = getFavs();
        const uid = String(it.dataset_uid || "");
        const i = favs.findIndex((f) => (uid && String(f.dataset_uid || "") === uid) || itemKey(f) === itemKey(it));
        if (i >= 0) { favs[i] = Object.assign({}, favs[i], cur, { folder: favs[i].folder }); setFavs(favs); }
        toast("已更新"); _fireFavRerender();
    }
    else toast("已是最新");
}
function _markGone(btn, it) {
    const wrap = btn && btn.closest(".lib-card-wrap");
    const card = wrap && wrap.querySelector(".card");
    if (card && card.querySelector(".badges")) {
        const badge = document.createElement("span");
        badge.className = "gone-badge";
        badge.textContent = "已下架";
        card.querySelector(".badges").appendChild(badge);
    }
}
/* 在对话中使用（数据集上下文，kind="dataset"）：经 projects.js 的注册口设 ctx chip。 */
function _favUse(it) {
    const n = normalizeItem(it);
    const text = "数据集：" + (n.dataset_name || "未命名")
        + "\n数据来源：" + (n.source || "未说明")
        + "\n样本量：" + (n.sample_size || "未说明")
        + (n.dataset_uid ? "\n编号：" + n.dataset_uid : "");
    _ctxSetDataset({ kind: "dataset", id: n.dataset_uid || n.url || n.dataset_name, name: n.dataset_name || "数据集", text, omitted: 0, note: "" });
}
/* 删除收藏：二段确认（armed），取消收藏 + 重渲分组。 */
function _favDel(it, btn) {
    if (!armTwoStepConfirm(btn, { idleText: COPY.common.delete })) return;
    toggleFav(it);   // 取消收藏（返回 false 表示移除；这里只需动作，不依赖返回值）
    toast(FAV_UNFAVED_COPY);
    if (_inLibFavs()) _fireFavRerender();
}

/* 收藏内容渲染：「全部」tab 按夹分组（默认夹组在前、用户夹按创建序，组标题=夹名+条数）；
   单夹 tab 平铺该夹。老收藏无 folder 字段 → favFolderOf 归一到默认夹组。 */
export function renderFavFolderGroups(favs, grid) {
    const effId = (it) => favFolderIdOrDefault(favFolderOf(it));
    if (_favViewTab !== null) {
        const items = favs.filter((it) => effId(it) === _favViewTab);
        if (!items.length) { grid.innerHTML = `<div class="muted-block">这个收藏夹还是空的。</div>`; return; }
        const g = document.createElement("div"); g.className = "grid";
        items.forEach((it) => g.appendChild(_favCardNode(it)));
        grid.appendChild(g);
        return;
    }
    const order = [""].concat(getFavFolders().map((f) => f.id));
    order.forEach((fid) => {
        const items = favs.filter((it) => effId(it) === fid);
        if (!items.length) return;
        const sec = document.createElement("section");
        sec.className = "fav-group";
        sec.innerHTML = `<h3 class="fav-group-title">${escapeHtml(favFolderNameById(fid))} <span class="fav-group-n">${items.length} 条</span></h3>`;
        const g = document.createElement("div"); g.className = "grid";
        items.forEach((it) => g.appendChild(_favCardNode(it)));
        sec.appendChild(g);
        grid.appendChild(sec);
    });
}
