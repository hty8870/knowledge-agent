# -*- coding: utf-8 -*-
"""「一句话打包」条数解析的**真行为**门（在 node 里跑真函数，不是静态字符串检查）。

为什么需要它：`web_smoke_test.py` 只做静态字符串检查、`node --check` 只验语法，两门都证明不了
「说前5条到底给几条」。而这里正是 修掉的两个静默偏离的现场：

  · 旧 `tpLimitFromUtterance` 用 ``/(\\d{1,3})/`` 取**整句里第一个** 1-3 位数字，且只认 10/20/50 三档。
    「2020年后的人类肺癌数据，打包前20条」会咬中「2020」的前三位「202」→ 不在三档 → 落回默认 10。
    用户说了 20、拿到 10，屏幕上没有任何提示。
  · 「打包前5条」的 5 也不在三档 → 同样静默变成 10，**多给** 5 条。

新写法要求数字**带量词收尾**（条 / 个 / 份 / 项），因此天然避开 10x、COVID-19、GSE123456、2020年。

task_pack.js 是 ES Module：不能再把源文件文本拼进 CJS 脚本跑（import/export 是 CJS 语法错误，
模块级状态也无法从外部赋值）。改为临时 .mjs：先桩宿主全局（window/document/localStorage——
import 链上的 core 等在模块顶层触碰它们），再经 file:// URL import 真模块、调真函数。
「上一条查询留下的清单」也不再从外部赋值 `_tpPlan`，而是先用桩 fetch 跑一次**成功的真预览**
造出来——同一不变量，走公开面到达，比戳私有状态更强。
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TASK_PACK_JS = ROOT / "web" / "static" / "js" / "act" / "task_pack.js"


def _resolve_node() -> "str | None":
    override = os.environ.get("BIODATA_NODE")
    if override and (shutil.which(override) or Path(override).exists()):
        return override
    for candidate in ("node", "node.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _run_node_mjs(script: str, payload: object) -> object:
    """与 test_act_frontend.py 同款的 node 真行为门：脚本落临时文件再跑（WinError 206 教训）。

    后缀必须 .mjs：quality_gate 把临时目录指进仓库（outputs/），根 package.json
    是 "type": "module"，而 task_pack.js 本身是 ESM——无论落在哪里都按 ESM 跑。
    """
    node = _resolve_node()
    if not node:
        pytest.skip("未解析到 node.js —— 跳过条数解析真行为门（full 质量门的语法检查环节必有 node）。")
    fd, script_path = tempfile.mkstemp(suffix=".mjs", prefix="biodata_tp_gate_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(script)
        proc = subprocess.run(
            [node, script_path], cwd=str(ROOT),
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
    finally:
        Path(script_path).unlink(missing_ok=True)
    assert proc.returncode == 0, f"node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


#: 在 node 里 import 真 task_pack.js 所需的最小宿主桩。task_pack 经 import 链拉进
#: core/shell/search/results/interactions——core 在模块顶层读 window.matchMedia、
#: 经典脚本模块末尾的绞杀桥写 window 并被它们的裸引用经全局作用域读取（浏览器里 window
#: 就是全局对象）。所以桩 window = globalThis 本物：桥上挂的名字才成为真全局。
#: localStorage 供 readJSON（来源/时间偏好）兜底。桩完再动态 import（静态 import 会提升、
#: 先于赋值执行）。
_ESM_PRELUDE = (
    "globalThis.window = globalThis;\n"
    "const _els = new Map();\n"
    "function _el(id) {\n"
    "    if (!_els.has(id)) {\n"
    "        _els.set(id, {\n"
    "            id, value: \"\", checked: false, hidden: false, disabled: false,\n"
    "            innerHTML: \"\", textContent: \"\", style: {}, dataset: {},\n"
    "            classList: { add() {}, remove() {}, toggle() {}, contains: () => false },\n"
    "            addEventListener() {}, setAttribute() {}, scrollIntoView() {},\n"
    "        });\n"
    "    }\n"
    "    return _els.get(id);\n"
    "}\n"
    "globalThis.document = {\n"
    "    getElementById: (id) => _el(id),\n"
    "    querySelector: () => null,\n"
    "    body: { classList: { add() {}, remove() {}, toggle() {}, contains: () => false } },\n"
    "};\n"
    "const _store = new Map();\n"
    "globalThis.localStorage = {\n"
    "    getItem: (k) => (_store.has(k) ? _store.get(k) : null),\n"
    "    setItem: (k, v) => { _store.set(k, String(v)); },\n"
    "    removeItem: (k) => { _store.delete(k); },\n"
    "};\n"
    f'const ns = await import("{TASK_PACK_JS.as_uri()}");\n'
)


#: 输入 → 期望读出的条数（0 = 没从话里读出条数，调用方走默认口径）
CASES = [
    ("帮我打包前20条", 20),
    ("打包前5条", 5),
    ("打包前 20 条", 20),
    ("导出这3份", 3),
    ("打包50条", 50),
    ("下载前2个", 2),
    # ↓ 旧实现在这一条上给 10（咬中 2020 的前三位）——本门的核心回归
    ("2020年后的人类肺癌数据，打包前20条", 20),
    ("最近20年的人类数据，打包前5条", 5),
    # ↓ 没有量词收尾的数字一律不认，避免把版本号/编号/年份当条数
    ("10x Genomics 的数据，打包", 0),
    ("COVID-19 数据打包", 0),
    ("GSE123456 打包", 0),
    ("打包", 0),
    ("帮我打包一下", 0),
    # ↓ 「3个样本」的量词属于样本、不属于数据集条数：量词后面不是句读，不认
    ("3个样本的数据打包", 0),
]


def test_tp_count_from_utterance_reads_what_the_user_said():
    script = (
        _ESM_PRELUDE
        + 'import { readFileSync } from "node:fs";\n'
        + "const _cases = JSON.parse(readFileSync(0, \"utf-8\"));\n"
        + "console.log(JSON.stringify(_cases.map((t) => ns.tpCountFromUtterance(t))));\n"
    )
    got = _run_node_mjs(script, [text for text, _ in CASES])
    wrong = [
        f"「{text}」期望 {want}、实际 {actual}"
        for (text, want), actual in zip(CASES, got)
        if actual != want
    ]
    assert not wrong, "条数读错（用户说的和系统用的不一致就是静默偏离）：\n  " + "\n  ".join(wrong)


def test_failed_preview_invalidates_the_previous_plan():
    """预览失败**必须**作废上一份清单，否则「自动执行」会产出一个内容全错的真 zip。

    根因链（修复前）：`previewTaskPack` 的两条失败出口都只改面板文字就裸 return，
    `_tpPlan` / `_tpChosen` 原封不动；而 `buildTaskPack` 的全部前置条件只有 `_tpPlan && _tpChosen.size`。
    于是「先搜 A（预览成功）→ 改搜 B（预览失败）→ 产包」会拿着 **A 的候选池**打出一个货真价实的 zip，
    而四道 409 锁一个都不会响——它们锁的是「回传参数与服务端重跑自洽」，回传的整套参数都是 A 的、完全自洽。

    今天这条路走不通，只是因为出错时面板里没有渲染产包按钮；任何直接调 `buildTaskPack()` 的代码
    （「一句话直接执行」的派发器正是这样）都会绕开这个事实上的门闩。所以这一条必须在**行为**上钉死。

    「上一份清单」经公开面造：先桩 fetch 跑一次成功的真预览（候选池 A1/A2 全勾选），
    再切到失败 fetch 预览第二次——断言清单被作废、产包无从下手。
    """
    script = _ESM_PRELUDE + """
import { readFileSync } from "node:fs";
const { previewTaskPack, buildTaskPack } = ns;
const _mode = JSON.parse(readFileSync(0, "utf-8")).mode;
// 先造一份「上一条查询」留下的真清单：一次成功的预览（走 previewTaskPack 真代码路径）
globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: true,
    plan: { candidate_uids: ["A1", "A2"], items: [], retrieval: {}, retrieval_params: {} } }) });
const first = await previewTaskPack({});
if (_mode === "network_error") {
    globalThis.fetch = async () => { throw new Error("boom"); };
} else if (_mode === "http_error") {
    globalThis.fetch = async () => ({ ok: false, json: async () => ({ ok: false, detail: "500" }) });
} else {
    globalThis.fetch = async () => ({ ok: true, json: async () => ({
        ok: true, plan: null, message_zh: "这次检索没有命中任何数据集，没有可以打包的内容。" }) });
}
const res = await previewTaskPack({});
// 预览失败之后，产包必须无从下手
const build = await buildTaskPack();
console.log(JSON.stringify({
    first_ok: first.ok === true,
    preview_ok: res.ok === true,
    plan_cleared: ns._tpPlan === null,
    chosen_cleared: ns._tpChosen.size === 0,
    build_ok: build.ok === true,
    build_has_artifact: !!build.artifact,
}));
"""
    for mode in ("network_error", "http_error", "no_plan"):
        got = _run_node_mjs(script, {"mode": mode})
        assert got["first_ok"] is True, f"{mode}：铺路用的首次预览没成功（桩失真？）"
        assert got["preview_ok"] is False, f"{mode}：预览失败却报成功"
        assert got["plan_cleared"] is True, f"{mode}：预览失败后 _tpPlan 没有被作废——会拿上一条查询的候选池产包"
        assert got["chosen_cleared"] is True, f"{mode}：预览失败后 _tpChosen 没有被清空"
        assert got["build_ok"] is False, f"{mode}：预览失败后仍然产出了任务包"
        assert got["build_has_artifact"] is False, f"{mode}：失败路径上竟给出了产物证据"


def test_preview_and_build_report_outcomes_instead_of_swallowing_them():
    """两个函数必须**有返回值**：调用方要据此渲染回执。

    以前它们所有出口都是裸 `return`、失败吞进 toast，于是任何「执行完报告一下做了什么」的代码
    只能照「成功」渲染——断网 / 409 / 后端无命中全会被写成「已经打包好了」。

    「有清单但没勾选」也经公开面造：候选池为空的预览成功 → `_tpChosen` 为空集。
    """
    script = _ESM_PRELUDE + """
import { readFileSync } from "node:fs";
JSON.parse(readFileSync(0, "utf-8"));
const { previewTaskPack, buildTaskPack } = ns;
const noPlan = await buildTaskPack();
// 预览成功但候选池为空 → 一个勾选都没有
globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: true,
    plan: { candidate_uids: [], items: [], retrieval: {}, retrieval_params: {} } }) });
const pre = await previewTaskPack({});
const noPick = await buildTaskPack();
console.log(JSON.stringify({
    no_plan_is_object: noPlan !== undefined && typeof noPlan === "object",
    no_plan_ok: noPlan && noPlan.ok,
    no_plan_has_reason: !!(noPlan && noPlan.error),
    pre_ok: pre.ok === true,
    no_pick_ok: noPick && noPick.ok,
    no_pick_has_reason: !!(noPick && noPick.error),
}));
"""
    got = _run_node_mjs(script, {})
    assert got["no_plan_is_object"] is True, "buildTaskPack 没有返回值 → 回执只能照「成功」渲染"
    assert got["no_plan_ok"] is False and got["no_plan_has_reason"] is True
    assert got["pre_ok"] is True, "候选池为空的预览应当成功（桩失真？）"
    assert got["no_pick_ok"] is False and got["no_pick_has_reason"] is True


def test_pool_tier_covers_any_count_up_to_fifty():
    """候选池只有 10/20/50 三档，但勾选可以是任意子集（`plan_spec` 刻意不含 selected_uids）。
    所以任意 1..50 条都必须能被兑现：取**恰好够用**的那一档，不多取。"""
    script = (
        _ESM_PRELUDE
        + 'import { readFileSync } from "node:fs";\n'
        + "const _n = JSON.parse(readFileSync(0, \"utf-8\"));\n"
        + "console.log(JSON.stringify(_n.map((k) => ns.tpPoolFor(k))));\n"
    )
    counts = [1, 5, 10, 11, 20, 21, 50]
    got = _run_node_mjs(script, counts)
    assert got == [10, 10, 10, 20, 20, 50, 50]
