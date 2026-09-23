# -*- coding: utf-8 -*-
"""意见反馈对话框（feedback_dialog_core.js / feedback.js）的结构契约门。

与 artifacts/projects/usage/benchfb 契约同一套思路：**三门测不出真行为**（web_smoke 静态查
字符串、node --check 只验语法、import 图只验 import 边），所以这里静态钉死结构不变量，
真行为由 `tests/js/feedback_dialog_core_spec.mjs` 在 node 里逐条断言（本文件末尾驱动）。

钉死的结构不变量：

1. **纯逻辑核心零 DOM / 零网络 / 零 localStorage**：feedback_dialog_core.js 只做确定性计算
   （校验/摘要/条目构造/剪贴板正文），队列存储属 feedback_core.js、出网属 usage_upload.js。
2. **不进静态图**：feedback_dialog_core.js 只相对 import feedback_core.js（零 `#` import）；
   feedback.js 对 usage_upload.js 只**动态 import**（静态 import 会牵动两页 importmap 与
   parity 门——usage_log.js 头部注释明令）；两模块都不得出现在 boot.js 的 import 里。
3. **入口/骨架在场**：设置「使用反馈」卡片 #feedbackSendEntryBtn、对话框 #feedbackModal、
   #feedbackText（意见）、#feedbackDiagChk（附诊断信息，默认勾选语义由 JS 保证）、
   #feedbackSendBtn / #feedbackCopyBtn（有通道 → 发送；无通道 → 复制兜底）、失败态
   #feedbackFailCopyBtn / #feedbackFailCopyCancelBtn（复制并取消自动重试）。
4. **授权与诚实语义**：发送路径必须「先入队后 sendFeedback」（feedbackEnqueue + sendFeedback
   同在 feedback.js 且 sendFeedback 只经动态 import 取）；无通道文案「当前未配置加密传输通道」
   与加密说明「请勿包含 API Key」都在场；客户端**不得**调用 usageLog 另埋文本类事件
   （feedback_sent{with_diag} 计数在接收端入库时完成）。
5. **禁碰 dataset.html**：dataset.html 由其他改动并行负责，本包不碰，importmap 只在本页登记；
   parity 门只查**被使用**的 specifier，本包不引入新的 `#` 静态 import，故不红。
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "web" / "static" / "js" / "core" / "feedback_dialog_core.js"
UI = ROOT / "web" / "static" / "js" / "core" / "feedback.js"
INDEX = ROOT / "web" / "static" / "index.html"
DATASET = ROOT / "web" / "static" / "dataset.html"
SPEC = ROOT / "tests" / "js" / "feedback_dialog_core_spec.mjs"

# 与遥测/artifacts 同一套出网原语。
FORBIDDEN_NETWORK_TOKENS = ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "navigator.connection")
# DOM / localStorage：纯逻辑核职责边界（队列存储归 feedback_core.js，UI 壳才许碰 DOM）。
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
    """断言只看真代码（注释里当然会出现「零网络」「不进静态图」这类说明词）。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


# ---------------------------------------------------------------- 职责边界

def test_dialog_core_layer_cannot_talk_to_the_network() -> None:
    """纯逻辑核零出网：意见正文/诊断快照只在入队后经 sendFeedback 加密出网（usage_upload.js）。"""
    code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    hits = [t for t in FORBIDDEN_NETWORK_TOKENS if t in code]
    assert not hits, f"feedback_dialog_core.js 出现出网原语：{hits}"


def test_dialog_core_layer_has_no_dom_or_localstorage() -> None:
    """零 DOM、零 localStorage：队列存储属 feedback_core.js；本文件只做确定性计算。"""
    code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    hits = [t for t in FORBIDDEN_DOM_TOKENS if t in code]
    assert not hits, f"feedback_dialog_core.js 出现 DOM/localStorage 访问：{hits}"


def test_dialog_core_has_no_hash_imports() -> None:
    """纯逻辑核只相对 import feedback_core.js——不进 import 图、不进环。"""
    text = CORE.read_text(encoding="utf-8")
    assert re.search(r'from\s*"#', text) is None, "feedback_dialog_core.js 出现了 # import（应相对 import feedback_core.js）"
    assert 'from "./feedback_core.js"' in text, "feedback_dialog_core.js 未相对 import feedback_core.js"


def test_usage_upload_only_dynamically_imported() -> None:
    """usage_upload.js 只许**动态** import（静态 import 会牵动两页 importmap 与 parity 门）。"""
    text = UI.read_text(encoding="utf-8")
    assert re.search(r'from\s*"(\./)?usage_upload\.js"', text) is None, (
        "feedback.js 静态 import 了 usage_upload.js——必须动态 import（usage_log.js 头部注释明令）")
    assert 'import("./usage_upload.js")' in text, "feedback.js 缺 usage_upload.js 动态 import"


def test_boot_does_not_import_the_dialog_modules() -> None:
    """两模块不进 boot 的静态 import 表（dataset.html 归另一并行改动，新增 # 键会红 parity 门）。"""
    boot = (ROOT / "web" / "static" / "js" / "core" / "boot.js").read_text(encoding="utf-8")
    assert "feedback" not in boot, "boot.js 出现 feedback 相关 import（本包自接线，不进 boot）"


# ---------------------------------------------------------------- 入口/骨架

def test_entry_points_and_skeleton_present() -> None:
    """设置「使用反馈」卡片按钮 + 对话框骨架必须在 index.html。"""
    index = INDEX.read_text(encoding="utf-8")
    for token in ('id="feedbackSendEntryBtn"', 'id="feedbackModal"', 'id="feedbackText"',
                  'id="feedbackDiagChk"', 'id="feedbackSendBtn"', 'id="feedbackCopyBtn"',
                  'id="feedbackFailCopyBtn"', 'id="feedbackFailCopyCancelBtn"', 'id="feedbackCloseBtn"'):
        assert token in index, f"index.html 缺 {token}"


def test_no_channel_copy_fallback_copy_is_present() -> None:
    """公钥未配置/WebCrypto 不可用 → 复制兜底文案与按钮语义必须在场（设计约定）。
    按钮文案在 index.html 骨架；「当前未配置加密传输通道」说明由 feedback.js 开窗时注入。"""
    index = INDEX.read_text(encoding="utf-8")
    assert "复制意见（含诊断信息）到剪贴板" in index, "无通道复制按钮文案缺失"
    ui = UI.read_text(encoding="utf-8")
    assert "当前未配置加密传输通道" in ui, "无通道兜底说明缺失"


def test_encryption_transmission_copy_is_present() -> None:
    """传输方式如实说明：内容经开发者公钥加密后发送 + 请勿包含 API Key/密码。"""
    index = INDEX.read_text(encoding="utf-8")
    assert "开发者公钥加密" in index, "加密传输说明缺失"
    assert "请勿包含 API Key、密码" in index, "「请勿包含 API Key、密码」提示缺失"


def test_send_authorization_semantics_present() -> None:
    """发送 = 明示单次授权：先 feedbackEnqueue 入队、再 sendFeedback 发送；关遥测也能发。"""
    code = _strip_js_comments(UI.read_text(encoding="utf-8"))
    assert "feedbackEnqueue(" in code, "feedback.js 未入队（授权语义缺失）"
    assert "sendFeedback(" in code, "feedback.js 未调 sendFeedback（独立入口缺失）"
    assert "feedbackRemoveForScope(" in code, "「复制并取消自动重试」撤单路径缺失"
    assert "feedbackHasSendChannel()" in code, "发送通道探测缺失"


def test_no_client_side_telemetry_for_feedback_text() -> None:
    """客户端不另埋文本类事件：feedback_sent{with_diag} 计数在接收端入库时完成（设计约定）。"""
    for path in (UI, CORE):
        code = _strip_js_comments(path.read_text(encoding="utf-8"))
        assert "usageLog(" not in code, f"{path.name} 调用了 usageLog（埋点应只在接收端计数）"


def test_importmap_registration_stays_on_index_page_only() -> None:
    """本包 importmap 登记只在本页 + package.json（dataset.html 归另一并行改动，不碰）。"""
    pkg = (ROOT / "package.json").read_text(encoding="utf-8")
    for key in ('"#feedback_dialog_core"', '"#feedback"'):
        assert key in pkg, f"package.json 缺 {key}"
        assert key in INDEX.read_text(encoding="utf-8"), f"index.html importmap 缺 {key}"
    ds = DATASET.read_text(encoding="utf-8")
    assert '"#feedback_dialog_core"' not in ds and '"#feedback"' not in ds, (
        "dataset.html 出现本包键——该页归另一并行改动，本包不得碰")


# ---------------------------------------------------------------- node 规格驱动

def test_dialog_core_spec_passes_in_node() -> None:
    """纯逻辑核心真行为：node 直跑 feedback_dialog_core_spec.mjs（断言失败 → 非零退出）。"""
    node = _resolve_node()
    assert node, "未找到 node（BIODATA_NODE 或 PATH）"
    r = subprocess.run([node, str(SPEC)], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, f"feedback_dialog_core_spec.mjs 失败：\n{r.stdout}\n{r.stderr}"
