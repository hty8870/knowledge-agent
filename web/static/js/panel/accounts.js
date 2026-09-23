"use strict";

/* 本文件是 ES Module：core / usage_log / memory 经 import 取绑定；
   CURRENT_USER 的写操作一律经 setCurrentUser（可变共享状态只许属主模块 core.js 写），
   读操作保留 CURRENT_USER 裸引用（ESM live binding）。 */
import { API, $, CURRENT_USER, activeView, setCurrentUser, toast, escapeHtml } from "#core";
import { usageOnAccountChanged } from "#usage_log";
import { benchfbOnAccountChanged } from "#benchfb";
import { renderMemorySuggestions, renderMemoryManager, userMemoryEnabled } from "#memory";
import { LAST_RECOMMEND_DATA, applyRecommendResult } from "#search";
import { cbClear } from "#board";
import { renderBrowse } from "#browse";
import { libRefreshActive, histRefreshActive } from "#shell";
import { resetFavFolderState } from "#fav_folders";
import { resetReuseScope } from "#reuse_pack";
import { artifactsOnProfileSwitched } from "#artifacts";

/* ---------- 账户切换反转钩子 ----------
   projects.js 经 setAccountChangedHook 注册「切账户后清课题 UI 态」回调。刻意不 import projects
   （projects 反向 import 本文件的 cbClear 等——accounts→projects 会让 projects 经
   interactions→browse→accounts 链路成环；注册反转与 core.js setHistHooks 同一模式）。 */
let _accountChangedHooks = [];
export function setAccountChangedHook(fn) {
    if (typeof fn === "function" && !_accountChangedHooks.includes(fn)) _accountChangedHooks.push(fn);
}

/* 本地账户前端：whoami / 登录 / 注册 / 登出 / 一键切换 + 账户态 UI。
   账户只让共用一台电脑的多人各自拥有私有的用户记忆 / 收藏 / 历史命名空间（见 core.js 的 nsKey）；
   记忆本身仍只存本机浏览器 localStorage、不上传（沿用记忆功能的隐私不变量，本模块不触碰记忆存储介质）。

   2026-08-02（保持登录 + 快捷切换）：
   - 服务端会话已落盘持久化（accounts.py，重启不再全体掉登录）；登录模态加「记住我」（默认勾）。
   - 账号 chip 常驻「设置 → 账户」（2026-08-03 起；此前在导航卡底部，用户判定不必占导航位）。
     点击弹账号菜单：最近账号**一键切换**（/api/account/switch）、登录其他账号、退出登录。
   - 一键切换的凭据：登录/注册成功时把 `session_token` 按用户名记进**机器级**（非 nsKey）
     localStorage 键 `biodata_known_accounts`。威胁模型（写死在这，别装看不见）：
     这是 loopback 单机工具，token 效力等同于浏览器里那片 HttpOnly cookie——能读到本机
     localStorage 的人本来就能直接用这片 cookie 发请求；换的是「点一下即切换」的便利。
     显式「退出登录」会同时销毁服务端会话并删除这条记录， token 不留复活通道。 */

const KNOWN_ACCOUNTS_KEY = "biodata_known_accounts";

/* 启动期 whoami 落定的 promise（browse.js 的 ?conv=/?fork= 找回必须等它：
   onAccountChanged 里有 cbClear()——whoami 若在找回**之后**才回来，刚重建的对话会被再清掉，
   正是「分支新标签页聊天记录为空」那条竞态）。initAccounts 启动时登记；未启动则恒 null。 */
export let ACCOUNTS_READY = null;

/* ---------- 网页版账号护栏 ----------
   服务端护栏模式（BIODATA_REQUIRE_ACCOUNT=1，由 /api/health 的 account.{required,invite} 报告）下：
   - 无有效会话 → 登录锁定（auth-locked）：整页只留登录/注册模态，关不掉（Esc/背板/✕ 全禁）；
   - 注册需邀请码 → 模态显示邀请码输入框；
   - 任何 /api 响应 401 auth_required（会话中途失效）→ 自动回登录锁定。
   闸关（本机单机形态）时以下全部 no-op，行为与旧版逐字节一致。 */
let _gate = { required: false, invite: false };
let _authLocked = false;

function syncAccountGateUI() {
    const inviteRow = $("accountInviteRow");
    if (inviteRow) inviteRow.hidden = !_gate.invite;
    const closeBtn = $("accountModalClose");
    if (closeBtn) closeBtn.hidden = _authLocked;
    const sub = $("accountModalSub");
    if (sub && _authLocked) {
        sub.textContent = "本站已开启登录保护：登录后即可使用全部功能"
            + (_gate.invite ? "；新用户请向管理员索取邀请码注册" : "");
    } else if (sub && !sub.textContent) {
        sub.textContent = "账户仅存在本机；用于让共用电脑的多人各自拥有私有记忆";
    }
}

function enterAuthLockdown() {
    if (!_gate.required) return;
    _authLocked = true;
    document.body.classList.add("auth-locked");
    syncAccountGateUI();
    openAccountModal(null);
}

function exitAuthLockdown() {
    _authLocked = false;
    document.body.classList.remove("auth-locked");
    syncAccountGateUI();
}

/* 会话中途失效的兜底：任意 /api（账号接口自身除外——登录失败也是 401，不能回环触发）
   返回 401 且体为 auth_required → 清当前用户并回登录锁定。包装一次 window.fetch，
   比逐个调用点接 401 收敛；响应体 clone 后读取，不影响原消费者。 */
let _fetchGuardInstalled = false;
function installAuthFetchGuard() {
    if (_fetchGuardInstalled || typeof window === "undefined" || typeof window.fetch !== "function") return;
    _fetchGuardInstalled = true;
    const orig = window.fetch.bind(window);
    window.fetch = function (input, init) {
        return orig(input, init).then(function (res) {
            try {
                if (_gate.required && res && res.status === 401 && !_authLocked) {
                    const url = (typeof input === "string") ? input : ((input && input.url) || "");
                    if (url.indexOf("/api/") !== -1 && url.indexOf("/api/account/") === -1) {
                        res.clone().json().then(function (data) {
                            if (data && data.error === "auth_required") {
                                setCurrentUser(null);
                                enterAuthLockdown();
                            }
                        }).catch(function () {});
                    }
                }
            } catch (_e) {}
            return res;
        });
    };
}

/* ---------- 多标签页账户态同步 ----------
   同浏览器多标签共享同一 origin 的会话 cookie，但各标签的 CURRENT_USER 是各自内存副本：
   A 标签登录/登出/切换后，B 标签仍按旧账户的命名空间读写记忆/收藏/历史（账户漂移）。
   同步靠两条通用机制，无定时轮询：
   - BroadcastChannel：本标签 deliberate 变更后广播，其他标签收到即复核身份；
   - visibilitychange：标签重新可见时去抖复核（BroadcastChannel 不可用时的兜底）。
   复核只认 whoami 的真实应答：网络/服务端异常（undefined）保持现状，绝不把取态失败当登出。 */
const _ACCOUNT_CHANNEL = (typeof BroadcastChannel === "function") ? new BroadcastChannel("biodata-account") : null;
// Node 契约测试链会真实执行本模块：Node 的 BroadcastChannel 是活动句柄，不 unref 会
// 挂住事件循环让测试进程永不退出（浏览器无此方法，typeof 闸下 no-op）。
if (_ACCOUNT_CHANNEL && typeof _ACCOUNT_CHANNEL.unref === "function") _ACCOUNT_CHANNEL.unref();
function _broadcastAccountChanged() {
    if (!_ACCOUNT_CHANNEL) return;
    try { _ACCOUNT_CHANNEL.postMessage({ type: "account-changed", at: Date.now() }); } catch (_e) {}
}
async function _whoamiQuiet() {
    try {
        const res = await fetch(API.accountWhoami);
        if (!res.ok) return undefined;
        const data = await res.json();
        return (data && data.ok && data.user) ? data.user : null;
    } catch (_e) { return undefined; }
}
let _recheckInflight = false;
let _recheckPending = false;   // 在途期间又来变更：挂起一次尾随复核，防旧应答落定中间态账户后再无人纠正
async function _recheckIdentity() {
    if (_recheckInflight) { _recheckPending = true; return; }
    _recheckInflight = true;
    try {
        do {
            _recheckPending = false;
            try { await ACCOUNTS_READY; } catch (_e) {}   // 启动 whoami 未落定前不掺和（见 ACCOUNTS_READY 注释的找回竞态）
            const prevName = CURRENT_USER ? CURRENT_USER.username : null;
            const user = await _whoamiQuiet();
            if (user === undefined) continue;   // 取态失败保持现状；有尾随变更则下一轮再试
            const nextName = user ? user.username : null;
            if (prevName === nextName) continue;
            setCurrentUser(user);
            // 锁定门随身份对齐：被动登录解开本标签的强制登录锁定；被动登出在护栏模式下回锁定（闸关 no-op）
            if (user && _authLocked) { exitAuthLockdown(); closeAccountModal(); }
            if (!user) enterAuthLockdown();
            onAccountChanged();   // 不广播：这是被动对齐，广播只在 deliberate 变更点发，避免标签间回环
        } while (_recheckPending);
    } finally {
        _recheckInflight = false;
    }
}
let _recheckTimer = 0;
function _scheduleRecheck() {
    if (document.visibilityState !== "visible") return;
    if (_recheckTimer) clearTimeout(_recheckTimer);
    _recheckTimer = setTimeout(function () { _recheckTimer = 0; _recheckIdentity(); }, 400);
}

function knownAccountsRead() {
    try {
        const raw = localStorage.getItem(KNOWN_ACCOUNTS_KEY);
        const data = raw ? JSON.parse(raw) : {};
        if (!data || typeof data !== "object" || Array.isArray(data)) return {};
        const out = {};
        Object.keys(data).forEach(function (name) {
            const row = data[name];
            if (row && typeof row.token === "string" && row.token) out[name] = { token: row.token, at: Number(row.at) || 0 };
        });
        return out;
    } catch (_e) { return {}; }
}
function knownAccountsWrite(map) {
    try { localStorage.setItem(KNOWN_ACCOUNTS_KEY, JSON.stringify(map || {})); } catch (_e) {}
}
function knownAccountRemember(username, token) {
    const map = knownAccountsRead();
    map[username] = { token: token, at: Date.now() };
    knownAccountsWrite(map);
}
function knownAccountForget(username) {
    const map = knownAccountsRead();
    if (map[username]) { delete map[username]; knownAccountsWrite(map); }
}

async function accountWhoami() {
    try {
        const res = await fetch(API.accountWhoami);
        const data = await res.json();
        setCurrentUser((data && data.ok && data.user) ? data.user : null);
    // whoami 失败不再静默当匿名：用户会看到「自己的记忆/收藏不见了」，必须告知原因。
    } catch (_e) { setCurrentUser(null); toast("登录状态获取失败，暂时按未登录使用"); }
    return CURRENT_USER;
}

async function accountAuth(kind, username, password, remember, inviteCode) {
    const url = kind === "register" ? API.accountRegister : API.accountLogin;
    const body = { username, password, remember: remember !== false };
    // T3：邀请码只在注册且服务端要求时随带（闸关时后端忽略该字段，契约 additive）。
    if (kind === "register" && inviteCode) body.invite_code = inviteCode;
    const res = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || !data.ok || !data.user) throw new Error((data && data.detail) || "登录或注册失败，请稍后重试");
    setCurrentUser(data.user);
    // 公网护栏硬化：护栏模式服务端本就不下发 session_token（data.session_token
    // 为空属正常）；即使下到也不往 localStorage 记——公网共用浏览器不做「记住一键切换凭据」。
    if (data.session_token && !_gate.required) knownAccountRemember(data.user.username, data.session_token);
    return data.user;
}

async function accountLogout() {
    // 只有服务端**确实**销毁了会话（响应 ok）才清本地登录态。否则会话仍在服务端存活、
    // cookie 仍有效，若此时静默清 CURRENT_USER + 报「已退出」，用户以为已登出而离开，
    // 下次加载 whoami 又把该账户解析回来、暴露其命名空间给共用机器的下一个人。
    const res = await fetch(API.accountLogout, { method: "POST" });
    if (!res.ok) throw new Error("退出失败，请重试");
    if (CURRENT_USER) knownAccountForget(CURRENT_USER.username);   // 显式登出 = 该账号的一键切换凭据一并销毁
    setCurrentUser(null);
}

/* 一键切换：把 cookie 重设到目标账号仍存活的会话上。401（会话已失效）→ 丢掉这条记忆，
   打开登录模态并预填用户名，退回密码登录。 */
export async function accountSwitchTo(username) {
    // 公网护栏硬化：护栏模式一键切换不可用（菜单本就不渲染入口，这里是纵深防御）。
    if (_gate.required) { openAccountModal(null, username); return; }
    const row = knownAccountsRead()[username];
    if (!row) { openAccountModal(null, username); return; }
    let data = null;
    try {
        const res = await fetch(API.accountSwitch, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token: row.token }),
        });
        data = await res.json();
        if (!res.ok || !data.ok || !data.user) throw new Error((data && data.detail) || "切换失败");
    } catch (err) {
        knownAccountForget(username);
        toast(String((err && err.message) || "这个账号的登录状态已失效，请重新登录"));
        openAccountModal(null, username);
        return;
    }
    setCurrentUser(data.user);
    onAccountChanged();
    _broadcastAccountChanged();
    toast("已切换到「" + data.user.username + "」");
}

/* 账户切换后（匿名↔登录、或换账户）：各用户数据视图从新命名空间重渲。 */
export function onAccountChanged() {
    // 收藏夹 UI 态（tab/管理面板/popover）与复用清单范围全部复位——它们属于上一账户的命名空间视图态
    if (typeof resetFavFolderState === "function") resetFavFolderState();
    if (typeof resetReuseScope === "function") resetReuseScope();
    renderAccountState();
    renderAccountChip();
    // 使用反馈也是 per-account 命名空间：不作废缓存，会把上一个账户攒的记录算进下一个人的反馈包。
    // usageOnAccountChanged 由 #usage_log import（起 usage_log 已转 ESM）——拼错名字模块加载期就炸。
    usageOnAccountChanged();
    benchfbOnAccountChanged();   // benchmark 采集记录同样 per-account 命名空间（同 usage 纪律）
    // 追踪：数据本体按 scope 隔离在 IndexedDB——切换只断引用不删数据。
    // artifactsOnProfileSwitched 清内存缓存与活动课题句柄；课题 UI 态（上下文卡/活动
    // 详情/首页「继续课题」条）由 projects.js 经 setAccountChangedHook 注册的回调清。
    if (typeof artifactsOnProfileSwitched === "function") artifactsOnProfileSwitched();
    _accountChangedHooks.forEach((h) => { try { h(); } catch (_e) {} });
    const en = $("memoryEnabled");
    if (en && typeof userMemoryEnabled === "function") en.checked = userMemoryEnabled();
    if (typeof renderMemorySuggestions === "function") renderMemorySuggestions();
    if (typeof renderMemoryManager === "function" && $("memoryModal") && !$("memoryModal").hidden) renderMemoryManager();
    const view = activeView();
    // 收藏迁入「我的库」浮窗收藏页签，不再是独立视图——开着且停在 favs 页签时经 shell 重渲
    // （renderFavorites 已注册为 libWin favs 渲染器）；历史同理（独立 #histWin）。
    if (typeof libRefreshActive === "function") libRefreshActive();
    if (typeof histRefreshActive === "function") histRefreshActive();
    // browse 网格与收藏共用 buildCard、心形同样按旧命名空间烤进 DOM——换账户后必须重渲。
    if (view === "browse" && typeof renderBrowse === "function") renderBrowse();
    // 结果卡的收藏心形是渲染时按旧命名空间 isFav() 烤进 DOM 的；账户切换后若不重渲，
    // 心形仍显示上一个账户的收藏态——把「谁收藏过哪些数据集」泄漏给共用机器的下一个人。
    // 结果落在 query 视图内（非独立 "results" 视图），故据 query 视图 + 存在结果快照判定重渲，
    // fromHistory 抑制历史回灌、noScroll 避免跳顶。
    if (view === "query" && LAST_RECOMMEND_DATA && typeof applyRecommendResult === "function") {
        applyRecommendResult(LAST_RECOMMEND_DATA, "", { noScroll: true, fromHistory: true });
    }
    // 条件板的撤销栈是「这个人这一串操作」，换人就清空。它只在内存里，清内存即可。
    // archive:false（A2）：命名空间已是新账户——丢弃前归档会把上一个人的对话写进新账户历史。
    cbClear({ archive: false });
}

function renderAccountState() {
    const sub = $("accountStateSub");
    if (!sub) return;
    // 2026-08-26 网页版设置卡精简（产品方裁决）：文字只保留用户名（chip 上）+
    // 一句「数据按账号隔离」，不再罗列记忆/收藏/历史细目。
    if (CURRENT_USER) {
        sub.textContent = "数据按账号隔离";
    } else {
        sub.textContent = "未登录";
    }
}

/* ---------------- 侧栏账号 chip + 菜单 ---------------- */

export function renderAccountChip() {
    const name = $("accountChipName"), avatar = $("accountAvatar");
    if (!name || !avatar) return;
    const uname = CURRENT_USER ? CURRENT_USER.username : "";
    name.textContent = uname || "未登录";
    avatar.textContent = uname ? uname.slice(0, 1).toUpperCase() : "?";
}

let _accountMenuOpen = false;
function closeAccountMenu() {
    const menu = $("accountMenu"), chip = $("accountChip");
    if (menu) menu.hidden = true;
    if (chip) chip.setAttribute("aria-expanded", "false");
    _accountMenuOpen = false;
}
function toggleAccountMenu() {
    const menu = $("accountMenu"), chip = $("accountChip");
    if (!menu || !chip) return;
    if (_accountMenuOpen) { closeAccountMenu(); return; }
    // 公网护栏硬化：护栏模式隐藏一键切换账号项（后端 /api/account/switch 已 403，
    // 本地也不再持有任何账号 token）。
    const known = _gate.required ? {} : knownAccountsRead();
    const names = Object.keys(known).sort(function (a, b) { return (known[b].at || 0) - (known[a].at || 0); });
    const cur = CURRENT_USER ? CURRENT_USER.username : "";
    let html = "";
    names.forEach(function (uname) {
        const isCur = uname === cur;
        html += '<button type="button" role="menuitem" class="acct-menu-item' + (isCur ? " is-current" : "") + '"'
            + ' data-acct-switch="' + escapeHtml(uname) + '"' + (isCur ? " disabled" : "") + '>'
            + '<span class="acct-avatar small" aria-hidden="true">' + escapeHtml(uname.slice(0, 1).toUpperCase()) + "</span>"
            + '<span class="acct-menu-name">' + escapeHtml(uname) + "</span>"
            + (isCur ? '<span class="acct-menu-mark">当前</span>' : "") + "</button>";
    });
    html += '<div class="acct-menu-div"></div>';
    html += '<button type="button" role="menuitem" class="acct-menu-item" data-acct-action="login">登录 / 注册其他账号…</button>';
    if (CURRENT_USER) html += '<button type="button" role="menuitem" class="acct-menu-item danger" data-acct-action="logout">退出登录</button>';
    menu.innerHTML = html;
    menu.hidden = false;
    /* chip 在设置抽屉**顶部**（账户块是 drawer-body 第一块），一味上弹会压住抽屉头、
       越出抽屉顶不可达。按抽屉内的真实剩余空间选方向：下方够就下弹（drop-down），否则维持上弹；
       菜单本体另有 max-height + 内部滚动兜底（账号再多也不越界）。 */
    const body = chip.closest(".drawer-body");
    if (body) {
        const zr = chip.getBoundingClientRect(), br = body.getBoundingClientRect();
        const below = br.bottom - zr.bottom, above = zr.top - br.top;
        menu.classList.toggle("drop-down", below >= 150 || below >= above);
    }
    chip.setAttribute("aria-expanded", "true");
    _accountMenuOpen = true;
}
function accountMenuClick(event) {
    const switchBtn = event.target.closest("[data-acct-switch]");
    const actionBtn = event.target.closest("[data-acct-action]");
    closeAccountMenu();
    if (switchBtn) { accountSwitchTo(switchBtn.getAttribute("data-acct-switch")); return; }
    if (!actionBtn) return;
    if (actionBtn.getAttribute("data-acct-action") === "login") { openAccountModal(null); return; }
    if (actionBtn.getAttribute("data-acct-action") === "logout") {
        accountLogout().then(function () { onAccountChanged(); _broadcastAccountChanged(); toast("已退出登录"); })
            .catch(function (err) { toast(String((err && err.message) || "退出失败，请重试")); });
    }
}

let _accountReturnFocus = null;
function setAccountError(msg) {
    const box = $("accountError"); if (!box) return;
    box.textContent = msg || ""; box.hidden = !msg;
}
function openAccountModal(trigger, prefillUsername) {
    _accountReturnFocus = trigger || document.activeElement;
    const modal = $("accountModal"); if (!modal) return;
    setAccountError("");
    syncAccountGateUI();   // T3：邀请码行/锁定态在每次打开时按最新 gate 快照同步
    $("accountUsername").value = prefillUsername || ""; $("accountPassword").value = "";
    modal.hidden = false; document.body.classList.add("modal-lock");
    if (prefillUsername) $("accountPassword").focus(); else $("accountUsername").focus();
}
function closeAccountModal() {
    if (_authLocked) return;   // T3 登录锁定：Esc/背板/✕ 都关不掉——登录成功才解锁
    const modal = $("accountModal"); if (!modal || modal.hidden) return;
    modal.hidden = true; document.body.classList.remove("modal-lock");
    if (_accountReturnFocus && document.body.contains(_accountReturnFocus)) _accountReturnFocus.focus();
}

async function submitAccount(kind) {
    const username = ($("accountUsername").value || "").trim();
    const password = $("accountPassword").value || "";
    const remember = !!($("accountRemember") && $("accountRemember").checked);
    // T3：服务端要求邀请码时注册必须先填（后端还会再校一遍，这里先拦空值省一次往返）。
    const invite = (_gate.invite && $("accountInvite")) ? ($("accountInvite").value || "").trim() : "";
    if (!username || !password) { setAccountError("请填写用户名和密码。"); return; }
    if (kind === "register" && _gate.invite && !invite) { setAccountError("请填写邀请码（向管理员索取）。"); return; }
    const btnLogin = $("accountLoginBtn"), btnReg = $("accountRegisterBtn");
    btnLogin.disabled = true; btnReg.disabled = true;
    try {
        await accountAuth(kind, username, password, remember, invite);
        if (_authLocked) exitAuthLockdown();   // 登录锁定下认证成功 → 解锁整页
        closeAccountModal();
        onAccountChanged();
        _broadcastAccountChanged();
        toast(kind === "register" ? "注册成功，已登录" : "登录成功");
    } catch (err) {
        setAccountError(String((err && err.message) || "认证失败"));
    } finally {
        btnLogin.disabled = false; btnReg.disabled = false;
    }
}

export function initAccounts() {
    renderAccountState();
    renderAccountChip();
    installAuthFetchGuard();   // T3：401 auth_required → 自动回登录锁定（护栏模式外 no-op）
    // 多标签页同步：他标签 deliberate 变更（广播）或本标签重新可见（去抖复核）→ whoami 对账
    if (_ACCOUNT_CHANNEL) _ACCOUNT_CHANNEL.onmessage = function () { _recheckIdentity(); };
    document.addEventListener("visibilitychange", _scheduleRecheck);
    // 账号 chip / 菜单（2026-08-03 起在设置·账户块内，登录/注册/登出/切换全走这颗 chip 的菜单）
    const chip = $("accountChip");
    if (chip) chip.addEventListener("click", toggleAccountMenu);
    const menu = $("accountMenu");
    if (menu) menu.addEventListener("click", accountMenuClick);
    document.addEventListener("click", (e) => {
        if (!_accountMenuOpen) return;
        const zone = $("acctZone");
        if (zone && !zone.contains(e.target)) closeAccountMenu();
    });
    const form = $("accountForm");
    if (form) form.addEventListener("submit", (e) => { e.preventDefault(); submitAccount("login"); });
    const regBtn = $("accountRegisterBtn");
    if (regBtn) regBtn.addEventListener("click", () => submitAccount("register"));
    const closeBtn = $("accountModalClose");
    if (closeBtn) closeBtn.addEventListener("click", closeAccountModal);
    const modal = $("accountModal");
    if (modal) modal.addEventListener("click", (e) => { if (e.target === modal) closeAccountModal(); });
    document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        if ($("accountModal") && !$("accountModal").hidden) closeAccountModal();
        else if (_accountMenuOpen) closeAccountMenu();
    });
    // T3：先取护栏快照（/api/health 的 account.{required,invite}，失败按闸关处理 = 本机形态不受影响），
    // 再 whoami；护栏模式且无有效会话 → 启动即进登录锁定（只见登录/注册视图）。
    ACCOUNTS_READY = fetch(API.health)
        .then(function (res) { return res.json(); })
        .then(function (h) {
            if (h && h.account) _gate = { required: !!h.account.required, invite: !!h.account.invite };
            // 公网护栏硬化：护栏模式下若发现历史遗留的一键切换 token 记录
            // （升级前写入的），立即清除——网页版不持有任何「免密码换号」凭据。
            if (_gate.required) {
                try { localStorage.removeItem(KNOWN_ACCOUNTS_KEY); } catch (_e) {}
            }
            syncAccountGateUI();
        })
        .catch(function () { /* health 失败按闸关处理，绝不把本机形态锁死 */ })
        .then(accountWhoami)
        .then(function (user) {
            onAccountChanged();
            if (_gate.required && !user) enterAuthLockdown();
        });
    return ACCOUNTS_READY;
}
