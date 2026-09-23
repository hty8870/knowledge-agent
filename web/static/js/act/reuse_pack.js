/* 复用数据出处清单 —— 把收藏的 N 个公开数据集整理成投稿材料。
 *
 * 体裁边界（决定了这个面板的全部文案）：产出的**不是** Data Availability Statement 主句。
 * 主句讲的是用户自己产出的数据去向；那句本工具帮不上，也按设计不接收相关输入。
 * 这条边界原本还有一段 `pack.boundary` 置顶陈述，2026-07-29 按产品方要求整段删除
 * （面板标题与英文段落已经把范围说清，那段是在用户没问时辩解一件不会发生的事）。后端字段一并移除。
 *
 * 隐私：走 **POST**。GET 会把每个 dataset_uid 打进 uvicorn access log = 事实上的埋点，
 * 而「要不要做使用数据采集」是用户明确保留给自己的决定。导出走前端 Blob，服务端不写盘。
 */

/* 本文件是 ES Module：core 的存储 CRUD/工具与 usage 层经 import 取（CURRENT_USER 是 core 的
   可变 let，live import 只读——本文件从不写它）。cards/act（downloadTextBlob）、accounts
   （resetReuseScope）、browse（syncReuseBar）、interactions（buildReusePack/setReuseScope）
   经 import 取本文件导出（绞杀桥已全退役）。 */
import { API, CURRENT_USER, $, copyTextAny, escapeHtml, favFolderIdOrDefault, favFolderNameById, favFolderOf, getFavFolders, getFavs, isHttp, toast } from "#core";
import { dlqFireBlob } from "#downloads";
import { USAGE_KINDS } from "#usage_core";
import { usageLog } from "#usage_log";

let _packBusy = false;

/* 清单范围：REUSE_SCOPE_ALL = 全部收藏；"" = 默认收藏夹；否则为用户夹 id。
   不持久化——换账户 / 范围夹被删都复位回「全部收藏」。范围名只用于前端展示与按钮文案，
   绝不进 POST 体（keys-only 红线）。 */
const REUSE_SCOPE_ALL = "__all__";
let _reuseScope = REUSE_SCOPE_ALL;
export function resetReuseScope() { _reuseScope = REUSE_SCOPE_ALL; }
function reuseScopeLabel() {
    if (_reuseScope === REUSE_SCOPE_ALL) return "全部收藏";
    return "「" + favFolderNameById(_reuseScope) + "」";
}
export function setReuseScope(v) {
    v = String(v ?? REUSE_SCOPE_ALL);
    if (v !== REUSE_SCOPE_ALL && v !== "" && !getFavFolders().some((f) => f.id === v)) v = REUSE_SCOPE_ALL;
    _reuseScope = v;
    syncReuseBar();
}

/* 前端 Blob 文本下载（导出的统一出口）：导出内容在浏览器里生成，服务端不落盘。
   数据集介绍页的单条「导出引文」也走这里。

   **返回是否真的存下来了**（true/false）。手点导出的路径只看那句 toast，不用返回值；
   但一句话执行层要据它渲染回执——没有返回值的话，Blob/URL 那一步抛异常时也只 toast 一句
   「下载失败」，回执却照样写「已导出引文：… 22.0 KB」，把一件没发生的事说成发生了。 */
export function downloadTextBlob(text, filename, mime, okMsg, failMsg, ctx) {
    // 2026-08-30：导出统一进浏览器下载队列（core/downloads.js）。
    // dlqFireBlob 内部已 try/catch、失败返回 false —— 照旧走 failMsg 分支，
    // 绝不把没存下来的事说成存下来了（返回值契约不变）。
    const kind = /\.(ris|bib)$/i.test(String(filename || "")) ? "cite" : "reuse";
    if (!dlqFireBlob(filename, new Blob([text], { type: mime }), { kind })) { toast(failMsg); return false; }
    toast(okMsg);
    // 同 task_pack：只在真下载成功的分支记。文件名的**后缀**足以区分引文/清单/材料，
    // 不记文件名本身（它带用户的收藏夹名）。
    // ctx（可选）：单条引文导出的归因上下文（cards.js 传 {uid}；批量导出不传）。
    // 引文（数据集详情页）与投稿材料/清单（收藏页）都不在结果页——
    // 显式 tid/iid=null，不冒领当前检索轮/当前展示（结果页的 pack/script 打点在
    // task_pack.js，不走这里，仍由全局注入当前轮）。
    usageLog(USAGE_KINDS.dl, Object.assign({ what: kind }, { tid: null, iid: null }, ctx || {}));
    return true;
}

/* 收藏夹里的 uid（keys-only：只送键，不送内容）。空 uid 的老收藏跳过——
   它们进不了后端解析，送过去只会白白变成一条墓碑。
   folderScope：REUSE_SCOPE_ALL = 全部收藏；否则只取该夹（"" = 默认收藏夹）的条目。 */
function favUidsForPack(folderScope) {
    const scope = typeof folderScope === "string" ? folderScope : _reuseScope;
    return getFavs()
        .filter((it) => scope === REUSE_SCOPE_ALL || favFolderIdOrDefault(favFolderOf(it)) === scope)
        .map((it) => String(it && it.dataset_uid || "").trim()).filter(Boolean);
}

/* 收藏视图顶部的工具条：有收藏才出现；范围下拉 = 全部收藏 + 各夹，条数实时反映当前范围。 */
export function syncReuseBar() {
    const bar = $("reuseBar");
    if (!bar) return;
    const folders = getFavFolders();
    // 当前范围失效（夹被删）→ 回落「全部收藏」
    if (_reuseScope !== REUSE_SCOPE_ALL && _reuseScope !== "" && !folders.some((f) => f.id === _reuseScope)) _reuseScope = REUSE_SCOPE_ALL;
    const scopeSel = $("reuseScope");
    if (scopeSel) {
        // 签名未变不重建 options：每次 renderFavorites 都走到这里，重建会关掉用户正展开的下拉
        const sig = folders.map((f) => f.id + ":" + f.name).join("|");
        if (scopeSel.dataset.sig !== sig) {
            scopeSel.innerHTML = `<option value="${REUSE_SCOPE_ALL}">全部收藏</option>`
                + `<option value="">默认收藏夹</option>`
                + folders.map((f) => `<option value="${escapeHtml(f.id)}">${escapeHtml(f.name)}</option>`).join("");
            scopeSel.dataset.sig = sig;
        }
        scopeSel.value = _reuseScope;
    }
    const uids = favUidsForPack();
    // 显隐按**全部收藏**判：范围为某空夹时条仍要留着，否则用户切不回「全部收藏」
    bar.style.display = getFavs().length ? "flex" : "none";
    const btn = $("reusePackBtn");
    if (btn && !_packBusy) btn.textContent = `生成投稿材料（${reuseScopeLabel()} · ${uids.length} 条）`;   // 与 index.html 静态文案同词——运行时覆写不许把旧词换回来
    // 收藏变化后旧清单就过期了（条数对不上会误导），直接收起而不是留着一份陈旧的。
    // 陈旧判定三维：条数（dataset.n）+ 账户（dataset.u）+ 范围（dataset.f）——
    // 同账户同条数但换了收藏夹范围，旧清单同样必须判陈旧收起。
    const panel = $("reusePanel");
    const packUser = (CURRENT_USER && CURRENT_USER.id) || "";
    if (panel && panel.dataset.n && (String(uids.length) !== panel.dataset.n || packUser !== (panel.dataset.u || "") || _reuseScope !== (panel.dataset.f || ""))) {
        panel.style.display = "none";
        panel.innerHTML = "";
        delete panel.dataset.n;
        delete panel.dataset.u;
        delete panel.dataset.f;
    }
}

export async function buildReusePack() {
    if (_packBusy) return;
    const uids = favUidsForPack();
    if (!uids.length) { toast(_reuseScope === REUSE_SCOPE_ALL ? "你还没有收藏任何数据集" : "这个收藏夹还是空的"); return; }
    const btn = $("reusePackBtn");
    const panel = $("reusePanel");
    _packBusy = true;
    if (btn) { btn.disabled = true; btn.textContent = "正在整理…"; }
    try {
        const res = await fetch(API.reusePack, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ uids }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok || !data.pack) throw new Error(data.detail || "未能生成清单，请稍后重试。");
        // 范围名只进面板回显（纯前端展示），不进 POST 体
        renderReusePack(data.pack, data.markdown || "", data.ris || "", data.bibtex || "", reuseScopeLabel());
        // 生成成功即**自动**把三个文件（.md + .ris + .bib）落进浏览器下载目录；
        // 面板上方的三个「下载」按钮保留为重下入口。
        autoDownloadReuseTrio({ md: data.markdown || "", ris: data.ris || "", bib: data.bibtex || "" });
        // 同时记录生成时的账户 + 范围指纹：syncReuseBar 据此识别「条数相同但属于另一账户/另一夹」的陈旧清单。
        if (panel) { panel.dataset.n = String(uids.length); panel.dataset.u = (CURRENT_USER && CURRENT_USER.id) || ""; panel.dataset.f = _reuseScope; }
    } catch (e) {
        if (panel) {
            panel.style.display = "block";
            panel.innerHTML = `<div class="files-hint">暂时无法生成清单，请稍后重试。原因：${escapeHtml(String(e && e.message || e))}</div>`;
            // 错误面板也按**当前**收藏指纹登记：收藏一变（条数、账户或范围）syncReuseBar 就把它收起。
            // 反例（曾经 delete 掉指纹）：syncReuseBar 的 `panel.dataset.n &&` 守卫会短路 → 错误面板永不自动收起，比正常清单更黏。
            panel.dataset.n = String(uids.length);
            panel.dataset.u = (CURRENT_USER && CURRENT_USER.id) || "";
            panel.dataset.f = _reuseScope;
        }
    } finally {
        // 必须放 finally：失败时若不复位，按钮永久卡在「正在整理…」。
        _packBusy = false;
        if (btn) btn.disabled = false;
        syncReuseBar();
    }
}

/* 复用清单三件套（.md 投稿材料 + .ris/.bib 引文）的文件名唯一真源——面板自动下载、
   面板手动重下、act 执行层（引文导出/投稿材料）三处共用，改文件名只动这里。 */
export const REUSE_FILE_NAMES = {
    md: "reused-public-datasets.md",
    ris: "reused-public-datasets.ris",
    bib: "reused-public-datasets.bib",
};

/* 三件套自动下载的共用锚点（面板生成成功即落盘、act 执行层引文导出/投稿材料共用）。
   files = { md?, ris?, bib? }：空值不下载、对应键返回 false；单文件失败不阻断其余
   （各自 toast）。返回 { md, ris, bib } 三个布尔，调用方必须消费——downloadTextBlob
   内部 try/catch 只 toast，不看返回值就会在浏览器没存下文件时照样写「已下载」。 */
export function autoDownloadReuseTrio(files) {
    files = files || {};
    const out = {};
    out.md = files.md ? downloadTextBlob(files.md, REUSE_FILE_NAMES.md, "text/markdown;charset=utf-8",
        "已下载投稿材料", "下载失败") : false;
    out.ris = files.ris ? downloadTextBlob(files.ris, REUSE_FILE_NAMES.ris,
        "application/x-research-info-systems;charset=utf-8", "已下载引文（ris）", "引文下载失败") : false;
    out.bib = files.bib ? downloadTextBlob(files.bib, REUSE_FILE_NAMES.bib,
        "application/x-bibtex;charset=utf-8", "已下载引文（bib）", "引文下载失败") : false;
    return out;
}

function renderReusePack(pack, markdown, ris, bibtex, scopeLabel) {
    const panel = $("reusePanel");
    if (!panel) return;
    const rows = Array.isArray(pack.table) ? pack.table : [];
    const gaps = Array.isArray(pack.gaps) ? pack.gaps : [];
    // 范围回显（纯前端展示）：这份清单是按「全部收藏」还是某个收藏夹生成的
    const scopeHtml = scopeLabel ? `<p class="rp-scope">范围：${escapeHtml(scopeLabel)}</p>` : "";
    // 复现信息：检索日期 + 数据集目录快照 ID + 条数。让重跑可对比、可复现。
    const retr = (pack.retrieval && typeof pack.retrieval === "object") ? pack.retrieval : null;
    const retrHtml = retr
        ? `<p class="rp-retrieval">本次检索信息（供复现）：检索于 ${escapeHtml(retr.date || "—")} · 数据集目录快照 `
          + `<code>${escapeHtml(retr.snapshot_id || "")}</code> · 快照内共 ${Number(retr.n_corpus_records || 0)} 条数据集</p>`
        : "";

    const tableRows = rows.map((r) => {
        const ident = r.identifier
            ? `<span class="rp-ident-kind">${escapeHtml(r.identifier_label_en || "")}</span> ${escapeHtml(r.identifier)}`
            : `<span class="rp-none">来源未登记编号</span>`;
        const rawCls = r.raw_state === "listed" ? "ok" : (r.raw_state === "not_listed" ? "partial" : "unknown");
        // isHttp 守卫必须在：url 是**数据派生值**，存储型 `javascript:` / `data:` URL
        // 会变成可点击的危险链接。非 http(s) → 退化成不可点文本，而不是静默丢掉
        // （丢掉会让用户以为这条没链接；显示出来他才知道该去来源核实）。
        const url = isHttp(r.url)
            ? `<a href="${escapeHtml(r.url)}" target="_blank" rel="noopener noreferrer">打开来源页</a>`
            : (r.url ? `<span class="rp-none" title="这不是 http/https 链接，出于安全未做成可点击；请到来源网站核对">${escapeHtml(r.url)}</span>`
                     : `<span class="rp-none">无</span>`);
        return `<tr>`
            + `<td>${escapeHtml(r.dataset_name)}</td>`
            + `<td>${escapeHtml(r.source)}</td>`
            + `<td>${ident}</td>`
            + `<td>${url}</td>`
            + `<td><span class="rp-raw ${rawCls}">${escapeHtml(r.raw_note_zh)}</span></td>`
            + `</tr>`;
    }).join("");

    panel.style.display = "block";
    panel.innerHTML =
        `<section class="rp-box">`
        + scopeHtml
        + retrHtml
        + `<div class="rp-head"><h4>投稿段落（英文 · 可直接粘贴）</h4>`
        + `<div class="rp-acts"><button class="btn rp-copy" type="button">复制段落</button>`
        + `<button class="btn rp-copy-md" type="button">复制 Markdown</button>`
        + `<button class="btn btn-primary rp-dl" type="button">下载 .md</button>`
        + `<button class="btn rp-dl-ris" type="button">下载 .ris</button>`
        + `<button class="btn rp-dl-bib" type="button">下载 .bib</button></div></div>`
        + `<blockquote class="rp-para">${escapeHtml(pack.paragraph || "")}</blockquote>`
        + `<h4 class="rp-sub">数据集清单（${rows.length} 条）</h4>`
        + `<div class="rp-table-wrap"><table class="rp-table">`
        + `<thead><tr><th>数据集</th><th>来源</th><th>编号</th><th>链接</th><th>原始数据</th></tr></thead>`
        + `<tbody>${tableRows}</tbody></table></div>`
        + (gaps.length
            ? `<h4 class="rp-sub">投稿前你需要自己核实（${gaps.length} 项）</h4>`
              + `<ul class="rp-gaps">${gaps.map((g) => `<li>${escapeHtml(g)}</li>`).join("")}</ul>`
            : `<p class="rp-clean">本清单没有发现需要额外核实的项。</p>`)
        + `</section>`;

    const copy = (text, okMsg) => copyTextAny(text, { okMsg });
    const btnCopy = panel.querySelector(".rp-copy");
    if (btnCopy) btnCopy.addEventListener("click", () => copy(pack.paragraph || "", "已复制投稿段落"));
    const btnMd = panel.querySelector(".rp-copy-md");
    if (btnMd) btnMd.addEventListener("click", () => copy(markdown, "已复制 Markdown（含补充表）"));
    // downloadTextBlob 已提为文件级共享函数（见本文件顶部）：介绍弹窗的单条「导出引文」复用它。
    const downloadText = downloadTextBlob;
    const btnDl = panel.querySelector(".rp-dl");
    if (btnDl) btnDl.addEventListener("click", () =>
        downloadText(markdown, REUSE_FILE_NAMES.md, "text/markdown;charset=utf-8",
            "已下载 " + REUSE_FILE_NAMES.md, "下载失败，请改用「复制 Markdown」"));
    // N10 引文导出：数据集条目（RIS TY-DATA / BibTeX @misc），非论文类型。
    const btnRis = panel.querySelector(".rp-dl-ris");
    if (btnRis) btnRis.addEventListener("click", () => {
        if (!ris) { toast("暂无可下载的引文"); return; }
        downloadText(ris, REUSE_FILE_NAMES.ris, "application/x-research-info-systems;charset=utf-8",
            "已下载 " + REUSE_FILE_NAMES.ris, "下载失败");
    });
    const btnBib = panel.querySelector(".rp-dl-bib");
    if (btnBib) btnBib.addEventListener("click", () => {
        if (!bibtex) { toast("暂无可下载的引文"); return; }
        downloadText(bibtex, REUSE_FILE_NAMES.bib, "application/x-bibtex;charset=utf-8",
            "已下载 " + REUSE_FILE_NAMES.bib, "下载失败");
    });
}
