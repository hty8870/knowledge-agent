# -*- coding: utf-8 -*-
"""dream 前端静态门：ESM 试点接线、端点声明、记忆 kind 扩展、诚实标识。"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
CORE = (ROOT / "web/static/js/core/core.js").read_text(encoding="utf-8")
DREAM = (ROOT / "web/static/js/panel/dream.js").read_text(encoding="utf-8")
MEMORY = (ROOT / "web/static/js/panel/memory.js").read_text(encoding="utf-8")
BOOT = (ROOT / "web/static/js/core/boot.js").read_text(encoding="utf-8")


def test_dream_js_loaded_as_es_module_with_cache_token() -> None:
    """dream.js 是前端模块化改造的第一个 ES Module——必须 type=module 且带缓存令牌。"""
    m = re.search(r'<script type="module" src="(/static/js/panel/dream\.js\?v=[0-9a-z-]+)"></script>', HTML)
    assert m, "index.html 里没有以 type=module + ?v= 加载 dream.js"


def test_dream_core_loaded_before_memory() -> None:
    """dream_core.js（纯核）必须先于 dream.js 可用；经典脚本段在 memory_rank 之后。"""
    assert HTML.index("dream_core.js?v=") < HTML.index('type="module" src="/static/js/panel/dream.js')


def test_dream_endpoint_declared_in_api_table() -> None:
    assert 'dream: "/api/dream"' in CORE, "core.js 的 API 表没有 dream 端点"


def test_dream_block_and_controls_present() -> None:
    for frag in ('id="dreamBlock"', 'id="dreamRunBtn"', 'id="dreamConsent"', 'id="dreamStatus"', 'id="dreamPreview"'):
        assert frag in HTML, f"记忆模态缺 {frag}"


def test_boot_calls_init_dream() -> None:
    assert "initDream();" in BOOT, "boot.js 没有调 initDream"


def test_dream_js_is_a_pure_module_and_boot_imports_it() -> None:
    """dream.js 不挂 window，boot.js（同为 ESM）经 import 取 initDream。"""
    assert "export function initDream" in DREAM, "dream.js 必须导出 initDream（boot 经 import 取）"
    assert "Object.assign(window" not in DREAM, "dream.js 的 window 桥接已退役"
    assert re.search(r'import\s*\{[^}]*\binitDream\b[^}]*\}\s*from\s*"#dream"', BOOT), (
        "boot.js 没有从 #dream import initDream"
    )


def test_dream_js_never_writes_without_user_pick() -> None:
    """诚实语义：写入只能发生在预览勾选之后——upsertMemory 只许出现在 dreamWriteAccepted 里。"""
    bodies = re.findall(r"function (\w+)\([^)]*\)\s*\{", DREAM)
    assert "dreamWriteAccepted" in bodies
    write_fn = re.search(r"function dreamWriteAccepted\([^)]*\)\s*\{(.*?)\n\}", DREAM, re.S)
    assert write_fn and 'upsertMemory("dream"' in write_fn.group(1)
    assert DREAM.count("upsertMemory(") == 1, "dream.js 里 upsertMemory 出现了不止一次（绕过预览的写入？）"


def test_memory_accepts_dream_kind_and_badges_it() -> None:
    assert 'item.kind === "dream"' in MEMORY, "getUserMemories 不接受 dream kind"
    assert 'dream: "AI 整理"' in MEMORY, "dream 条目必须徽标「AI 整理」（诚实标识，不冒充手写）"
    assert "memory-promote" in MEMORY, "dream 条目缺「存为偏好」转正入口"


def test_dream_consent_is_namespaced_and_explains_network_send() -> None:
    assert "dreamConsent" in CORE, "core.js LS 表缺 dreamConsent"
    assert "nsKey(LS.dreamConsent)" in DREAM, "知情同意标记必须按账户命名空间存"
    assert "历史对话" in DREAM or "历史对话" in HTML
