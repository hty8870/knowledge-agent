# -*- coding: utf-8 -*-
"""课题数据层（artifacts.js）的结构契约门。

设计：课题层 设计约定/设计约定/设计约定。
本门与 usage/benchfb 契约同一套思路：**三门测不出真行为**（web_smoke 静态查字符串、
node --check 只验语法、import 图只验 import 边），所以这里静态钉死结构不变量，
真行为由 `tests/js/artifacts_spec.mjs` 在 node 里逐条断言（本文件末尾驱动）。

钉死的结构不变量：

1. **零出网**：课题内容是用户的**研究内容**（设计约定隐私红线），数据层出现网络原语即红——
   与遥测层同一套红线（唯一出网通道 usage_upload.js 是别的层的事）。
2. **零 DOM / 零 localStorage**：设计约定明确「localStorage 只存活动 tab/活动课题 id 等轻量
   UI 态」，那是 UI 层职责；数据层出现 document./window./localStorage 即红。
3. **纯依赖白名单**：只允许导入 `#usage_core` 的纯 `usagePolicyRef`，统一 provenance/遥测策略串；
   不允许 UI、存储或网络模块依赖。
4. **schema 版本与关键语义常量在场**：schema_version 字段、新候选默认「待核验」（设计约定/设计约定）、
   复合主键带 profile scope、QuotaExceededError 捕获点、profile 生命周期钩子（accounts.js
   日后接线点）——都是 diff 闭环与导出中心要直接踩的地基，漏一个后面全歪。
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "web" / "static" / "js" / "core" / "artifacts.js"
SPEC = ROOT / "tests" / "js" / "artifacts_spec.mjs"

# 与遥测层同一套出网原语（usage_upload.js 是唯一出网通道；数据层一个都不能有）。
FORBIDDEN_NETWORK_TOKENS = ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "navigator.connection")
# DOM / localStorage：数据层职责边界（UI 态存储归 UI 层，课题本体只进 IndexedDB）。
FORBIDDEN_DOM_TOKENS = ("document.", "window.", "getElementById", "localStorage")


def _resolve_node() -> str | None:
    override = os.environ.get("BIODATA_NODE")
    if override and (shutil.which(override) or Path(override).exists()):
        return override
    for cand in ("node", "node.exe"):
        found = shutil.which(cand)
        if found:
            return found
    return None


def _strip_js_comments(text: str) -> str:
    """断言只看真代码（注释里当然会出现「零网络」「localStorage 归 UI」这类说明词；
    同 usage/benchfb 契约的既有做法）。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


# ---------------------------------------------------------------- 职责边界

def test_artifacts_layer_cannot_talk_to_the_network() -> None:
    """课题内容是用户研究内容，任何出网原语都是隐私事故（设计约定红线）。"""
    code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    hits = [t for t in FORBIDDEN_NETWORK_TOKENS if t in code]
    assert not hits, f"artifacts.js 出现出网原语：{hits}（课题内容只存本机 IndexedDB，绝不上传）"


def test_artifacts_layer_has_no_dom_or_localstorage() -> None:
    """零 DOM、零 localStorage——数据层只认 IndexedDB；轻量 UI 态（活动 tab 等）是 UI 层的事。"""
    code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    hits = [t for t in FORBIDDEN_DOM_TOKENS if t in code]
    assert not hits, f"artifacts.js 出现 DOM/localStorage 访问：{hits}（设计约定：课题数据只进 IndexedDB）"


def test_artifacts_layer_only_imports_pure_policy_helper() -> None:
    """唯一允许的 import 是 usage_core 纯策略规范化 helper。"""
    text = CORE.read_text(encoding="utf-8")
    imports = re.findall(r'import\s*\{([^}]*)\}\s*from\s*"(#[^"]+)"', text)
    assert imports == [(" usagePolicyRef ", "#usage_core")]


# ---------------------------------------------------------------- schema 契约

def test_schema_and_status_constants_are_present() -> None:
    text = CORE.read_text(encoding="utf-8")
    assert "ARTIFACTS_SCHEMA" in text and 'export const ARTIFACTS_SCHEMA = 1;' in text, "ARTIFACTS_SCHEMA 常量缺失"
    assert 'export const ARTIFACTS_DB_NAME = "biodata-artifacts";' in text, "库名常量缺失（设计约定的 biodata-artifacts）"
    assert 'export const DEFAULT_CANDIDATE_STATUS = PROJECT_STATUS.PENDING;' in text, "新候选默认待核验常量缺失（设计约定硬性）"
    # schema_version 字段必备：规整函数必须在每条记录上落当前版本
    assert "schema_version: ARTIFACTS_SCHEMA" in text, "记录 schema_version 字段缺失（课题/导出建库前提）"


def test_quota_error_is_captured_honestly() -> None:
    """写路径必须捕获 QuotaExceededError 并如实上报（设计约定：不静默吞掉「写不进去」）。"""
    text = CORE.read_text(encoding="utf-8")
    assert "QuotaExceededError" in text, "未显式处理 QuotaExceededError"
    # 捕获点必须在写包装里（put 请求的 error 分支）
    assert re.search(r"req\.onerror\s*=.*QuotaExceededError|_requestError\(req", text), "写路径未见错误上报"
    assert "artifactsStorageEstimate" in text, "存储预估预警函数缺失（navigator.storage.estimate 仅作预警）"


def test_profile_lifecycle_hook_is_present() -> None:
    """profile 生命周期接口（accounts.js 日后接线点）：清内存缓存 + 活动课题句柄，不删数据。"""
    text = CORE.read_text(encoding="utf-8")
    for symbol in ("artifactsOnProfileSwitched", "artifactsActiveProjectId", "artifactsSetActiveProjectId"):
        assert f"export function {symbol}" in text, f"缺 profile 生命周期接口 {symbol}"
    assert "accounts.js" in text, "钩子注释应写明 accounts.js 接线点"


def test_composite_key_carries_profile_scope() -> None:
    """复合主键带 profile scope（设计约定）：scope + project_id 确定性编码 + by_scope 索引。"""
    text = CORE.read_text(encoding="utf-8")
    assert "artifactsKey(scope, projectId)" in text or "artifactsKey(" in text, "缺复合主键编码函数"
    assert "by_scope" in text, "缺 by_scope 索引（profile 列课题依赖它）"


def test_importmap_registration_done_by_ui_batch() -> None:
    """#artifacts 的 importmap/package.json 登记在课题 UI 批完成——
    数据层的「不登记」守卫已退役（那段守卫防的是数据层越界登记；UI 批收口登记是设计约定的
    既定顺序）。登记必须三处齐全且两页映射一致，否则 test_frontend_import_graph 会红。"""
    pkg = (ROOT / "package.json").read_text(encoding="utf-8")
    assert '"#artifacts"' in pkg, "package.json 缺 #artifacts——已登记，缺失即漂移"
    maps = {}
    for page in ("index.html", "dataset.html"):
        html = (ROOT / "web" / "static" / page).read_text(encoding="utf-8")
        assert '"#artifacts"' in html, f"{page} importmap 缺 #artifacts——已登记，缺失即漂移"
        m = re.search(r'"#artifacts"\s*:\s*"([^"]+)"', html)
        assert m, f"{page} #artifacts 映射缺失"
        maps[page] = m.group(1).split("?")[0]
    pkg_m = re.search(r'"#artifacts"\s*:\s*"([^"]+)"', pkg)
    assert pkg_m, "package.json #artifacts 映射缺失"
    assert pkg_m.group(1).lstrip("./") == "web/static/js/core/artifacts.js", "package.json #artifacts 映射指向不符"
    assert maps["index.html"] == maps["dataset.html"], f"两页 #artifacts 映射不一致：{maps}"


# ---------------------------------------------------------------- node 行为规格

def test_artifacts_behavior_spec() -> None:
    """数据层真行为规格：CRUD/隔离/配额/备份/生命周期逐条断言（node 跑，非零退出即红）。"""
    node = _resolve_node()
    if not node:
        import pytest
        pytest.skip("本机没有 node")
    proc = subprocess.run([node, str(SPEC)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"artifacts 行为规格失败：\n{proc.stdout}\n{proc.stderr}"
    assert "OK artifacts_spec.mjs" in proc.stdout, f"规格缺少 OK 标记（驱动门失效？）：\n{proc.stdout}"
