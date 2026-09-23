# -*- coding: utf-8 -*-
"""`hidden` 属性必须真的隐藏（集成验证抓到的存量缺陷）。

现象：规则弃权 / 零结果的屏幕上，「📊 可行性概览」和「📦 下载这批数据」两个主按钮照样显示，
点「打包」只能得到一个空包。JS 侧写的是对的（`btn.hidden = true`），是 CSS 把它作废了——
`[hidden]{display:none}` 属**用户代理**样式表，作者样式里任何 display 都压过它，
而本项目的 `.btn { display: inline-flex }` 正好命中这两个按钮。

此前的补法是一个组件一条 `.X[hidden]{display:none}`（已累计 6 条：modal-backdrop / audit-banner /
sf-panel / rs-coverage / tour-visual / ds-panel），每漏一个就再犯一次。现在收成一条全局兜底。

这条测试是**静态**的（不执行 CSS），只钉住那条全局规则存在且带 `!important`；
集成行为验证过（弃权态下两个按钮 computed display 均为 none）。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "web" / "static" / "css" / "app.css"


def test_global_hidden_rule_exists_and_is_important():
    text = CSS.read_text(encoding="utf-8")
    rules = re.findall(r"(?m)^\s*\[hidden\]\s*\{([^}]*)\}", text)
    assert rules, (
        "app.css 缺少全局 `[hidden] { display: none !important; }`。"
        "没有它，任何带 display 的作者样式（如 .btn{display:inline-flex}）都会让 element.hidden 失效，"
        "空结果页上照样挂着「下载这批数据」这种主按钮。"
    )
    body = rules[0]
    assert "display" in body and "none" in body, body
    assert "!important" in body, (
        "全局兜底必须带 !important —— 否则后来新增的任何 `.foo { display: flex }` 又会压过它，"
        "这类事故就会以新面孔重来一次。"
    )


def test_primary_action_buttons_still_use_the_hidden_attribute():
    """反同义反复：如果哪天有人改成用 class 控制显隐，这条会红，提醒同步上面的兜底。"""
    js = (ROOT / "web" / "static" / "js" / "search" / "results.js").read_text(encoding="utf-8")
    assert "btn.hidden = true" in js or "hidden = true" in js, "results.js 不再用 hidden 属性控制主按钮显隐？"
