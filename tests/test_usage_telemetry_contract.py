# -*- coding: utf-8 -*-
"""使用反馈（埋点）的隐私与诚实性契约门 —— 2026-08-20 tl1 反转版。

## 这个功能为什么需要一道**结构性**的门

产品决定做「使用数据采集」，最初的形态是**本机记录 + 手动回传**，
代码为「零出网」付过真实成本：`/api/reuse-pack` 走 POST 而非 GET（免得 dataset_uid 进
uvicorn 的 access log）、服务端不存会话、导出走前端 Blob 不写盘。

2026-08-20 tl1 起（设计裁决见 `设计文档）：
**默认开启本地采集**；每账户独立 consent；部署配置安全 HTTPS 通道后才自动上传，
否则仅本地保存/手动导出。旧契约「全站离线、永不上传」正式作废，但安全默认仍零远程出网。

新契约在这里被钉成机械可验证的不变量，而不是靠注释和记性：

- **唯一出网通道**：遥测层只有 `usage_upload.js` 允许出现网络原语；`usage_log.js`、
  `usage_core.js`、`benchfb.js`、`benchfb_core.js` 仍**零网络原语**（各自的历史承诺
  一字节没放宽，只是把「谁可以出网」从全员禁止改成单人专责）。
- **双重门控**：上传必须同时满足 开关开（usageEnabled）+ consent 已同意
  （usageConsentGiven）；任一为假，`maybeUploadUsage` 第一行就返回。
- **脱敏是结构性的**：上传包必经 `usage_core.buildTelemetryPackage`（api_key 整键删、
  端点只留主机名、不记密码/账户名）；阈值常量存在且 ≤ 设计值。
- **关闭不删数据**（旧红线保留）；**上传绝不打断主功能**（try/catch、失败静默、无 toast）。
"""
import base64
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "web" / "static" / "js"
CORE = JS_DIR / "core" / "usage_core.js"
LOG = JS_DIR / "core" / "usage_log.js"
UPLOAD = JS_DIR / "core" / "usage_upload.js"
BENCHFB = JS_DIR / "core" / "benchfb.js"
BENCHFB_CORE = JS_DIR / "core" / "benchfb_core.js"
FEEDBACK_CORE = JS_DIR / "core" / "feedback_core.js"
INDEX = ROOT / "web" / "static" / "index.html"
DATASET_HTML = ROOT / "web" / "static" / "dataset.html"
SPEC = ROOT / "tests" / "js" / "usage_core_spec.mjs"
CONCURRENCY_SPEC = ROOT / "tests" / "js" / "telemetry_concurrency_spec.mjs"
IMPRESSION_SPEC = ROOT / "tests" / "js" / "telemetry_impression_spec.mjs"
FEEDBACK_SPEC = ROOT / "tests" / "js" / "feedback_core_spec.mjs"
PKG = ROOT / "package.json"

# 后端整棵树：埋点是纯前端的，这里任何一处出现埋点符号都说明分层被破坏了。
BACKEND = ROOT / "src" / "dataset_recommender"

# 遥测层各文件里被禁止的出网原语（唯一例外：usage_upload.js）。
FORBIDDEN_NETWORK_TOKENS = ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "navigator.connection")


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
    """去掉 // 与 /* * / 注释。

    解释性注释里当然会出现 `fetch`「不上传」这类词；拿注释当证据两头都会错：
    要么把说明当成违规、要么把违规当成说明。断言只看**真代码**。
    （同 tests/test_onboarding_contract.py 与 test_act_frontend.py 的既有做法。）
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


# ---------------------------------------------------------------- 唯一出网通道

def test_only_usage_upload_may_talk_to_the_network() -> None:
    """遥测层唯一出网通道反转（2026-08-20 tl1）：网络原语**只允许**出现在 usage_upload.js。

    usage_log / usage_core / benchfb / benchfb_core 仍零网络原语——回传方式的旧承诺
    一字节没放宽；新增的自动上传只有 usage_upload.js 一条口子，别处加任何出网原语即红。
    """
    for path in (CORE, LOG, BENCHFB, BENCHFB_CORE):
        code = _strip_js_comments(path.read_text(encoding="utf-8"))
        for token in FORBIDDEN_NETWORK_TOKENS:
            assert token not in code, f"{path.name} 里出现了出网原语 {token!r} —— 遥测层只有 usage_upload.js 可以出网"

    upload = UPLOAD.read_text(encoding="utf-8")
    assert "fetch(" in _strip_js_comments(upload), "usage_upload.js 必须承载唯一的出网通道（fetch 上传）"
    assert "X-Ingest-Token" in _strip_js_comments(upload), "已配置上传必须带可轮换 client credential header"


def test_the_upload_module_keeps_the_static_graph_untouched() -> None:
    """usage_upload.js 用**相对路径动态 import** 引入，不进 importmap / package.json 键表：

    新模块上键要动两页 importmap 与 parity 门；动态 import 绕开静态图
    （test_frontend_import_graph.py 只盯静态边），也不把「唯一网络模块」拖进 import 环。
    """
    log_code = LOG.read_text(encoding="utf-8")
    assert 'import("./usage_upload.js")' in log_code, "usage_log 必须经相对路径动态 import 触发上传"
    for path in sorted(JS_DIR.glob("**/*.js")):
        assert 'from "#usage_upload"' not in path.read_text(encoding="utf-8"), (
            f"{path.name} 静态 import 了 #usage_upload —— 动态通道被静态化，会牵动 importmap/parity 门")
    imports = __import__("json").loads(PKG.read_text(encoding="utf-8"))["imports"]
    assert "usage_upload" not in {k.lstrip("#") for k in imports}, "usage_upload 不得进 package.json imports（上键要动两页 importmap）"


def test_the_backend_knows_nothing_about_usage_logging() -> None:
    """后端全树不得出现埋点符号：分层一旦破裂，遥测就失去了「纯前端」的边界。"""
    hits = []
    for py in BACKEND.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        if "usageLog" in text or "biodata_usage_" in text:
            hits.append(py.relative_to(ROOT).as_posix())
    assert not hits, f"埋点符号泄漏进后端：{hits}（埋点必须是纯前端的）"


def test_the_frozen_evaluation_path_never_imports_the_usage_layer() -> None:
    """检索/排序/评测三条路径与埋点零耦合 —— 冻结基准结构性不受影响。

    只查**埋点专有符号**，不查裸 `usage` 子串：provider 的响应里本来就有 `usage`
    （token 计数），拿裸子串当判据早晚会把无关代码判成违规，然后被人一改了之。
    """
    markers = ("usageLog", "usage_core", "usage_log", "biodata_usage_")
    for name in ("retrieval/retriever.py", "app/workflow.py", "retrieval/rerank.py", "retrieval/query_parser.py"):
        text = (BACKEND / name).read_text(encoding="utf-8")
        for marker in markers:
            assert marker not in text, f"{name} 引用了 {marker!r} —— 检索路径不得与埋点耦合"


# ---------------------------------------------------------------- 默认态（单版本化：恒开）

def test_the_default_is_on_and_choice_wins() -> None:
    """显式选择永远优先；没表过态时**默认开**（2026-08-20 tl1 单版本化：主线版分叉废弃）。

    关 = "0" 为假；null/未表态 = 真（默认开）；"1" = 真。旧的主线版默认关分支已删除。
    """
    code = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    assert 'if (chosen !== null) return chosen === "1";' in code, "显式选择必须永远优先于默认"
    body = re.search(r"function usageEnabledForScope\([^)]*\)\s*\{(.*?)\n\}", code, flags=re.S)
    assert body, "未找到 usageEnabledForScope"
    assert "return true;" in body.group(1), "未表态时的默认必须是开（单版本化恒取强化版分支）"
    assert "isBenchfbBuild" not in body.group(1), "usageEnabled 不得再走构建分叉——分叉逻辑已删除"
    # 打点入口第一行就短路，关闭状态下不留任何副作用
    m = re.search(r"function usageLog\([^)]*\)\s*\{(?P<body>.*?)\n\}", code, flags=re.S)
    assert m and "if (!usageEnabledForScope(scope)) return false;" in m.group("body"), (
        "usageLog 的第一行必须是开关短路 —— 关着的时候一个字节都不该写"
    )
    html = INDEX.read_text(encoding="utf-8")
    assert re.search(r'<input type="checkbox" id="nodeUsage">', html), (
        "设置里的使用反馈开关不得带 checked —— HTML 不带默认态，由 JS 按选择如实填"
    )
    assert "本版本默认开启" in html, "设置区必须如实写明默认开启"


def test_single_versioning_keeps_is_benchfb_build_true_forever() -> None:
    """isBenchfbBuild 导出保留（onboarding.js 顶层调用它定默认分支）但恒 true：

    不再探测 <meta>/按钮 DOM，也不再有任何版本分叉判断；meta 保留只是指纹契约用。
    """
    code = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    assert "export function isBenchfbBuild()" in code, "isBenchfbBuild 导出必须保留（onboarding.js 还在用）"
    body = re.search(r"function isBenchfbBuild\(\)\s*\{(.*?)\n\}", code, flags=re.S)
    assert body, "未找到 isBenchfbBuild"
    assert "return true;" in body.group(1), "isBenchfbBuild 必须恒 true（原反馈强化版）"
    assert "biodata-build" not in code, "运行时不得再探测构建标记 meta"
    assert "benchfbExportBtn" not in code, "运行时不得再探测按钮 DOM 存废"


def test_the_onboarding_points_at_the_management_row() -> None:
    """2026-08-13：教程最后一步不再当场问开不开，改为打开设置抽屉、高亮管理入口。

    当场开关退役的理由：默认态已定（强化版默认开），教程里塞一个一次性开关只会与
    设置里的真实开关打架。这里钉死：没有任何一步携带 choice；最后一步锚定设置区
    使用反馈那一行；DOM 与 JS 里都不再有当场选择的残留。
    """
    ob = (JS_DIR / "core" / "onboarding.js").read_text(encoding="utf-8")
    assert "choice:" not in ob, "教程步骤里不应再有当场选择（choice 机器已退役）"
    assert 'target: ".usage-setting"' in ob, "教程最后一步必须锚定设置区的使用反馈行"
    assert "usageSetEnabled" not in _strip_js_comments(ob), "教程不得再直接写使用反馈开关"
    html = INDEX.read_text(encoding="utf-8")
    for node_id in ("onboardingChoice", "onboardingChoiceYes", "onboardingChoiceNo"):
        assert f'id="{node_id}"' not in html, f"当场选择的 DOM 残留：#{node_id}"


def test_not_answering_is_not_consent() -> None:
    """不回答 ≠ 同意：教程那一步不得挡路，跳过/关闭都只能留在默认态。

    `stopOnboarding` 只写 `LS.onboarding`，从不碰使用反馈开关 —— 用户点「跳过」
    或直接关掉教程，采集默认态不受影响（上传另有 consent 双重门控，见下方两测）。
    """
    code = _strip_js_comments((JS_DIR / "core" / "onboarding.js").read_text(encoding="utf-8"))
    body = re.search(r"function stopOnboarding\(markDone\)\s*\{(.*?)\n\}", code, flags=re.S)
    assert body, "未找到 stopOnboarding"
    assert "usageSetEnabled" not in body.group(1), (
        "stopOnboarding 不得写使用反馈开关 —— 跳过教程必须等于「没回答」"
    )


# ---------------------------------------------------------------- 上传门控（双重）

def test_upload_is_double_gated_by_switch_and_consent() -> None:
    """上传触发受**双重门控**：开关（usageEnabled）+ consent（usageConsentGiven）。

    任一为假，maybeUploadUsage 第一行就返回 —— 开关关闭 = 零采集零上传；
    未同意 consent（首次告知弹窗还没走完）也一个字节都不发。
    """
    code = _strip_js_comments(UPLOAD.read_text(encoding="utf-8"))
    assert "if (!usageEnabledForScope(scope) || !usageConsentGiven(scope)) return false;" in code, (
        "maybeUploadUsage 第一道门必须是「开关 && consent」双短路"
    )
    # 关闭开关这一侧：usageSetEnabled 不得自己触发上传（上传只由打点落盘/启动尾触发）
    log_code = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    set_body = re.search(r"function usageSetEnabled\(on\)\s*\{(.*?)\n\}", log_code, flags=re.S)
    assert set_body, "未找到 usageSetEnabled"
    assert "upload" not in set_body.group(1) and "_fireUpload" not in set_body.group(1), (
        "usageSetEnabled 不得直接触发上传 —— 关着的时候绝不能有上传尝试"
    )


def test_consent_is_per_account_profile() -> None:
    """共享浏览器的每个账户/匿名 profile 都必须独立同意，不能继承前一人的 consent。"""
    core_code = (JS_DIR / "core" / "core.js").read_text(encoding="utf-8")
    # v2：值为同意时刻 ISO 串（MCP 中继 since_ts 下界要用）；"0"=拒绝。
    assert "usageConsent: \"biodata_consent_v2\"" in core_code, "LS 键表必须登记 consent 键（v2=同意时刻 ISO）"
    assert "benchfbLabels: \"biodata_benchfb_labels_v1\"" in core_code, "LS 键表必须登记 benchfb label 台账键"
    assert "usageUploadMeta: \"biodata_usage_upload_meta_v1\"" in core_code, "LS 键表必须登记上传账本键"
    code = _strip_js_comments(UPLOAD.read_text(encoding="utf-8"))
    body = re.search(r"function usageConsentGiven\([^)]*\)\s*\{(.*?)\n\}", code, flags=re.S)
    assert body, "未找到 usageConsentGiven"
    assert "usageConsentGivenForScope" in body.group(1), "consent 必须委托 per-scope 单一真源"
    assert "nsKeyFor(LS.usageConsent, scope)" in code, "consent 必须按账户 scope 隔离"
    assert "export function setUsageConsent(" in code, "必须提供 consent 写入接口（S5 弹窗要用）"


def test_training_consent_is_separate_opt_in_and_contract_is_versioned() -> None:
    """产品改进授权不得偷换成训练授权；训练默认开启（2026-08-25 产品方决策，
    显式 opt-out "0" 永远优先）、独立 per-profile、可机械过滤。"""
    html = INDEX.read_text(encoding="utf-8")
    core_js = (JS_DIR / "core" / "core.js").read_text(encoding="utf-8")
    usage_core = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    upload = _strip_js_comments(UPLOAD.read_text(encoding="utf-8"))
    log = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    assert 'trainingConsent: "biodata_training_consent_v1"' in core_js
    assert 'id="usageTrainingToggle"' in html and 'id="consentTrainingOptIn"' in html
    assert "这项授权本身不包含训练或微调模型" in html
    assert "以更细颗粒度采集数据" in html
    assert "usageTrainingConsentGivenForScope" in log and "usageSetTrainingConsent" in log
    # 默认开启语义钉：只有显式 "0"（opt-out）才为否；键缺失 = 从未表态 = 同意。
    assert 'return v !== "0";' in log, "训练授权默认开启：缺失键必须按同意计，仅显式 opt-out 拒绝"
    assert "usageSetTrainingConsent(!!(training && training.checked), scope)" in upload
    assert "TELEMETRY_CONTRACT_VERSION = 2" in usage_core
    for field in ("contract_version:", "prompt_version:", "experiment_id:", "experiment_arm:",
                  "propensity:", "training_consent:"):
        assert field in usage_core, f"合同 v2 缺字段 {field}"
    assert "opts.trainingConsent === true" in usage_core, "训练授权必须 fail-closed，不能 truthy 放行"


def test_optional_experiment_assignment_is_executable_and_default_off() -> None:
    """不只留 nullable schema：配置后须真改请求参数；空配置不产生 control 假标签。"""
    html = INDEX.read_text(encoding="utf-8")
    detail = (ROOT / "web" / "static" / "dataset.html").read_text(encoding="utf-8")
    core = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    log = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    search = _strip_js_comments((JS_DIR / "search" / "search.js").read_text(encoding="utf-8"))
    for page in (html, detail):
        assert 'name="biodata-experiment-arms" content=""' in page
    assert "telemetryExperimentAssign" in core and "usageExperimentContext" in log
    assert "if (armsText) return null" in log, "非法动态分臂配置必须 fail-closed，不能落到遗留静态臂"
    assert "if (experiment.overrides) Object.assign(params, experiment.overrides)" in search
    assert "params.experiment_id = experiment.experimentId" in search


def test_queue_drops_are_observable_and_ack_safe() -> None:
    """FIFO 取舍保留，但 usage/benchfb/存储失败必须进下一包计数，ACK 后才扣除。"""
    core_js = (JS_DIR / "core" / "core.js").read_text(encoding="utf-8")
    log = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    bench = _strip_js_comments(BENCHFB.read_text(encoding="utf-8"))
    upload = _strip_js_comments(UPLOAD.read_text(encoding="utf-8"))
    package = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    assert 'telemetryDrops: "biodata_telemetry_drops_v1"' in core_js
    for symbol in ("usageNoteDropsForScope", "usageDropSnapshotForScope", "usageAckDropSnapshotForScope"):
        assert symbol in log
    assert 'usageNoteDropsForScope(scope, "usage", overflow)' in log
    assert 'usageNoteDropsForScope(scope, "benchfb"' in bench
    assert 'usageNoteDropsForScope(scope, "storage_error", 1)' in log
    assert "drop_report" in package and "dropped_count" in package
    ack_at = upload.find("usageAckDropSnapshotForScope(scope, dropSnapshot)")
    ok_at = upload.find("usageRemoveEventsForScope(scope, snap.usageIds)")
    assert ack_at > ok_at > 0, "丢弃账本只能在 HTTP 成功后的精确 ACK 段扣除"
    assert '"drop:" + String(dropSnapshot.revision)' in upload, "packet_id 必须纳入丢弃快照 revision"


def test_upload_thresholds_stay_at_or_below_design() -> None:
    """触发阈值常量存在且 **≤ 设计值**。2026-08-22 激进上传改版：

    benchfb ≥1 轮 / usage ≥10 条（原 3 轮/50 条——benchfb 单条即一轮完整检索现场）；
    起 usage 默认降到 **2 条**（无 hint 时几乎实时），阈值/间隔随服务器
    server_hint 动态调整（见 test_adaptive_upload_threshold_hint_pins）。
    「启动距上次成功 >6h」与 2 分钟评估节流退役，改为启动有待发即传 + 30s trailing 防抖
    + 5 分钟周期兜底 + pagehide/hidden 的 fetch keepalive 尽力一发。这些常量与钩子
    在这里钉成机械不变量，防回潮。"""
    code = _strip_js_comments(UPLOAD.read_text(encoding="utf-8"))

    m = re.search(r"const UPLOAD_BENCHFB_MIN\s*=\s*(\d+);", code)
    assert m and int(m.group(1)) <= 1, f"benchfb 阈值必须 ≤ 1，实际 {m and m.group(1)}"
    m = re.search(r"const UPLOAD_EVENTS_MIN\s*=\s*(\d+);", code)
    assert m and int(m.group(1)) <= 2, f"usage 默认阈值必须 ≤ 2，实际 {m and m.group(1)}"

    def _const_ms(name: str, design: int) -> None:
        mm = re.search(rf"const {name}\s*=\s*([^;]+);", code)
        assert mm, f"未找到 {name} 常量"
        # 常量可以是字面毫秒数或乘法表达式（如 30 * 1000）；把数字连乘后必须 ≤ 设计值
        ms = 1
        for part in re.findall(r"\d+", mm.group(1)):
            ms *= int(part)
        assert ms <= design, f"{name} 必须 ≤ {design}，实际 {mm.group(1)}"

    _const_ms("UPLOAD_DEBOUNCE_MS", 30 * 1000)      # 常规打点触发的 trailing 防抖
    _const_ms("UPLOAD_PERIODIC_MS", 5 * 60 * 1000)  # 周期兜底
    assert "UPLOAD_RETRY_HOURS_MS" not in code, "6h 启动等待已随激进上传退役（启动有待发即传）"
    assert "UPLOAD_CHECK_INTERVAL_MS" not in code, "2 分钟评估节流已由 30s trailing 防抖接替"
    assert "pagehide" in code, "页面关闭前必须有尽力一发（pagehide 钩子）"
    assert 'visibilitychange' in code, "页面隐藏前必须有尽力一发（visibilitychange 钩子）"
    assert "keepalive" in code, "尽力档必须走 fetch keepalive（sendBeacon 带不了 X-Ingest-Token 自定义头）"

    # 端点与令牌不再硬编码进可下载 JS：部署从 HTML meta 注入；公网只许 HTTPS。
    raw = UPLOAD.read_text(encoding="utf-8")
    assert "<server-ip>" not in raw and "<your-ingest-token>" not in raw
    assert "biodata-telemetry-endpoint" in raw and "biodata-telemetry-token" in raw
    assert 'u.protocol !== "https:"' in code and 'u.protocol === "http:" && loopback' in code
    html = INDEX.read_text(encoding="utf-8")
    detail = DATASET_HTML.read_text(encoding="utf-8")

    # 合并裁决：生产明文 HTTP 端点以显式配置启用（风险已知、用户知情授权）。
    # 两页 meta 显式配好生产 endpoint + 可轮换 client credential + allow-insecure 主机白名单，
    # 客户端开箱即上传（不静默断流）；白名单只含该主机，其它明文公网主机仍 fail-closed。
    assert '<meta name="biodata-telemetry-endpoint" content="http://<server-ip>:8471/v1/ingest">' in html, (
        "首页必须显式配置生产遥测端点（合并裁决，不得静默断流）")
    for page in (html, detail):
        for name, value in (
            ("biodata-telemetry-endpoint", "http://<server-ip>:8471/v1/ingest"),
            ("biodata-telemetry-token", "<your-ingest-token>"),
            ("biodata-telemetry-allow-insecure", "<server-ip>"),
        ):
            assert re.search(rf'<meta name="{name}" content="{re.escape(value)}">', page), (
                f"{name} 必须显式配置为 {value}")

    # JS 侧机制钉：allow-insecure 是逗号分隔主机白名单，非布尔开关；未登记主机必须拒绝。
    assert "biodata-telemetry-allow-insecure" in raw, "JS 必须读 allow-insecure 白名单 meta"
    assert "allowInsecureHosts.split(\",\")" in code, "白名单必须按逗号解析主机列表"
    assert "hosts.indexOf(host) < 0" in code, "明文公网主机不在白名单必须拒绝（fail-closed）"


def test_runtime_telemetry_meta_is_injected_without_source_rewrite(monkeypatch) -> None:
    from dataset_recommender.app import webapp

    monkeypatch.setenv("BIODATA_TELEMETRY_ENDPOINT", "https://telemetry.example/v1/ingest?a=1&b=2")
    monkeypatch.setenv("BIODATA_TELEMETRY_TOKEN", 'token-"quoted"')
    monkeypatch.setenv("BIODATA_TELEMETRY_ALLOW_INSECURE", "telemetry.example")
    html = webapp._index_html()
    assert "https://telemetry.example/v1/ingest?a=1&amp;b=2" in html
    assert "token-&quot;quoted&quot;" in html
    assert "<your-ingest-token>" not in html
    assert "<your-ingest-token>" in (webapp.STATIC_DIR / "index.html").read_text(encoding="utf-8"), (
        "运行时注入不得改写源 HTML")


def test_adaptive_upload_threshold_hint_pins() -> None:
    """：上传阈值/节奏随服务器 server_hint 自适应（结构性钉子，断真代码）：

    - 默认（无 hint）usage 阈值 = 2、最小间隔 = 3 分钟常量（见上一测）；
    - 200 响应带 server_hint{batch_threshold,min_interval_ms} → 采用并钳制到 [2,50]/[15s,10min]，
      持久化到上传 meta（hintThreshold/hintIntervalMs，跨刷新生效）；
    - 429 → 临时高档（RATE_LIMIT_THRESHOLD=20 / RATE_LIMIT_INTERVAL_MS=5min），直到下次 200 hint 覆盖；
    - 老接收端（无 server_hint 字段）→ _adoptServerHint 返回 null，维持当前动态值（fail-safe）；
    - 闸门必须经 _dynamicThreshold / _dynamicIntervalMs 读取动态值，而不是直接读常量。
    """
    code = _strip_js_comments(UPLOAD.read_text(encoding="utf-8"))

    for name, want in (
        ("HINT_THRESHOLD_MIN", 2),
        ("HINT_THRESHOLD_MAX", 50),
        ("HINT_INTERVAL_MIN_MS", 15000),
        ("HINT_INTERVAL_MAX_MS", 600000),
        ("RATE_LIMIT_THRESHOLD", 20),
        ("RATE_LIMIT_INTERVAL_MS", 300000),
    ):
        m = re.search(rf"const {name}\s*=\s*([^;]+);", code)
        assert m, f"未找到 {name} 常量"
        ms = 1
        for part in re.findall(r"\d+", m.group(1)):
            ms *= int(part)
        assert ms == want, f"{name} 必须 = {want}，实际 {m.group(1)}"

    assert "server_hint" in code, "必须消费 200 响应的 server_hint 字段"
    assert "hintThreshold" in code and "hintIntervalMs" in code, "动态值必须持久化到上传 meta"
    assert "_dynamicThreshold(" in code and "_dynamicIntervalMs(" in code, "闸门必须经动态读取函数取值"
    assert 'if (!serverHint || typeof serverHint !== "object") return null;' in code, (
        "老接收端/形状非法必须 fail-safe（不采用、维持当前值）")
    assert "status === 429" in code, "429 必须触发临时高档升级"
    assert "RATE_LIMIT_THRESHOLD" in code and "RATE_LIMIT_INTERVAL_MS" in code, "429 高档必须来自常量"
    assert "KEEPALIVE_MIN_GAP_MS" in code, "keepalive 尽力档最小间隔闸必须保留（不随 hint 调整）"


def test_client_server_body_budget_contract() -> None:
    """机械钉死两侧预算并要求 413 重组，防止常量再次独立漂移。"""
    upload = _strip_js_comments(UPLOAD.read_text(encoding="utf-8"))
    receiver = (ROOT / "services" / "telemetry-receiver" / "app.py").read_text(encoding="utf-8")
    client = re.search(r"const INGEST_BODY_MAX_BYTES\s*=\s*(\d+)", upload)
    server = re.search(r"MAX_BODY_BYTES\s*=\s*(\d+)\s*\*\s*1024\s*\*\s*1024", receiver)
    assert client and server
    client_bytes = int(client.group(1))
    server_bytes = int(server.group(1)) * 1024 * 1024
    assert client_bytes < server_bytes * 0.95
    assert "max_body_bytes" in upload and "_bodyBudgetAfter413" in upload
    assert "status === 413" in upload and "BODY_RETRY_MAX" in upload


def test_upload_acks_exact_ids_in_captured_scope() -> None:
    """成功后按开工 scope + event/record id 精确 ACK，不按当前账户或数组长度截断。"""
    code = _strip_js_comments(UPLOAD.read_text(encoding="utf-8"))
    ok_at = code.find("if (!sent.ok)")
    usage_at = code.find("usageRemoveEventsForScope(scope, snap.usageIds)")
    bench_at = code.find("benchfbRemoveRecordsForScope(scope, snap.benchIds)")
    assert ok_at >= 0 and usage_at > ok_at and bench_at > ok_at
    assert "usageList.splice(" not in code and "rest.splice(" not in code, "长度截断会删错账户/新事件，必须退役"
    assert "try {" in code and "catch" in code, "全程必须包在 try/catch 里（绝不打断主功能）"
    assert "toast(" not in code, "上传不得弹 toast 打扰用户"


def test_payload_goes_through_the_sanitizer() -> None:
    """上传包必经 usage_core.buildTelemetryPackage（结构性脱敏）：

    包里有 schema / install_id / app / usage_events / benchfb_records；构造时做
    防御性剔除（api_key 整键删、端点只留主机、不记密码/账户名）——与 benchfb 同一条红线。
    """
    code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    assert "export function buildTelemetryPackage(" in code, "上传包构造函数必须存在"
    assert '"biodata-telemetry/1"' in code, "schema 必须是 biodata-telemetry/1（设计 §2）"
    for key in ("packet_id:", "install_id:", "client_id:", "profile_id:", "exported_at:", "usage_events:", "benchfb_records:"):
        assert key in code, f"上传包必须包含 {key} 字段"
    strip = re.search(r"const TELEMETRY_STRIP_KEY_RE\s*=\s*(/[^/]+/[a-z]*);", code)
    assert strip, "未找到 TELEMETRY_STRIP_KEY_RE 脱敏正则"
    for pat in ("api", "key", "password", "username", "account"):
        assert pat in strip.group(1), f"脱敏正则必须覆盖 {pat!r}（api_key/密码/账户名红线）"
    assert "TELEMETRY_STRIP_KEY_RE.test(k)" in code, "构造包时必须实际执行剔除（不只是定义）"
    # 2026-08-22：值级遮蔽（键级剔除挡不住「值里夹带」的自由文本 PII）
    # 与 MCP 中继记录字段（mcp_records 同样过整条脱敏链）。
    assert "export function telemetryMaskString(" in code, "值级遮蔽函数必须存在"
    for pat in ("1[3-9]", "[手机号]", "[证件号]", "[邮箱]"):
        assert pat in code, f"值级遮蔽必须覆盖 {pat!r}"
    assert "pkg.mcp_records" in code, "mcp_records 中继字段必须能进包（接收端配套放开后收编）"


def test_activation_ping_is_one_shot_double_gated_and_stays_in_the_upload_module() -> None:
    """激活 ping（tl1 追加）：consent 同意即发一次性 hello 包。

    - **一次性**：幂等 ACK 后写 profile 级键 `biodata_ping_sent_v1`（LS.pingSent）=1，不再发；
      失败静默、下次触发点重试（绝不打扰主功能、无 toast）。
    - **双重门控**：与常规上传同款 `usageEnabled() && usageConsentGiven()` 双短路，任一为假不发。
    - **唯一出网通道红线不破**：ping 仍在 usage_upload.js 内 fetch；遥测层其余模块不得出现
      sendActivationPing 符号（唯一网络出口仍是本模块）。
    - **触发点**：(a) consent 同意落盘后立即 fire-and-forget；(b) maybeUploadUsage 启动路径
      已同意但未 ping 成功过时补发（覆盖「旧版本同意过、升级到带 ping 版本」的机器）。
    """
    core_code = (JS_DIR / "core" / "core.js").read_text(encoding="utf-8")
    assert "pingSent: \"biodata_ping_sent_v1\"" in core_code, "LS 键表必须登记 ping 一次性键"
    code = _strip_js_comments(UPLOAD.read_text(encoding="utf-8"))
    body = re.search(r"function _sendPingLocked\([^)]*\)\s*\{(.*?)\n\}", code, flags=re.S)
    assert body, "未找到 _sendPingLocked"
    assert "if (!usageEnabledForScope(scope) || !usageConsentGiven(scope)) return false;" in body.group(1), (
        "ping 第一道门必须是「开关 && consent」双短路（与常规上传同款）"
    )
    assert "LS.pingSent" in body.group(1), "ping 一次性键必须在 sendActivationPing 内读写"
    assert 'k: "hello"' in body.group(1), "ping 包必须含 {k: 'hello'} 事件（usage_events 单条、benchfb 空）"
    assert "localStorage.setItem(pingKey, \"1\")" in body.group(1), (
        "ping 必须在 HTTP 200（成功）后才落一次性键"
    )
    assert "toast(" not in body.group(1), "ping 不得弹 toast 打扰用户"
    # 触发点 (a)：同意落盘后立即发（fire-and-forget；用同意行之后的片段，避免命中启动路径那处调用）
    raw = UPLOAD.read_text(encoding="utf-8")
    agree_at = raw.find("setUsageConsent(true, scope)")
    assert agree_at >= 0 and "sendActivationPing(scope)" in raw[agree_at:], "同意落盘后必须立即触发该 profile ping"
    # 触发点 (b)：maybeUploadUsage 启动路径补发（覆盖升级机）
    assert "if (startup === true) sendActivationPing(scope);" in code, (
        "maybeUploadUsage 启动路径必须补发 ping（覆盖「旧版本同意过、升级到带 ping 版本」的机器）"
    )
    # 唯一出网通道红线：ping 只能在 usage_upload.js 内发
    for path in (CORE, LOG, BENCHFB, BENCHFB_CORE):
        assert "sendActivationPing" not in path.read_text(encoding="utf-8"), (
            f"{path.name} 不得承载激活 ping —— 唯一出网通道红线：ping 只能在 usage_upload.js 内发")


# ---------------------------------------------------------------- 不记什么

def test_secrets_and_identities_are_never_recorded() -> None:
    """记录层不得触碰密钥、密码与账户身份；脱敏剔除兜底在构造侧。

    查询原话确实会被记（不记它这个功能就没有价值），但那是用户自己写的、
    发出前逐行可见可删的内容；密钥和账户名不是，也永远不该进这条管线。
    """
    code = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    for token in ("api_key", "apiKey", "password", "accountUsername", "LS.cfg", "cfgKey"):
        assert token not in code, f"使用反馈记录层引用了 {token!r} —— 密钥/密码/账户信息绝不进埋点"
    # 账户隔离：事件与开关必须按 per-account nsKey 读写
    assert "nsKeyFor(LS.usage" in code and "nsKeyFor(LS.usageEnabled" in code, (
        "事件与开关都必须按账户 nsKey 隔离，否则共用一台电脑的人会看到彼此的查询")
    # 构造侧防御性剔除（与 benchfb 红线一致，见 test_payload_goes_through_the_sanitizer）
    core_code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    assert "TELEMETRY_STRIP_KEY_RE" in core_code, "构造侧必须带脱敏剔除兜底"


def test_the_install_id_is_random_and_not_derived_from_anything() -> None:
    """安装码只是个随机短码，不得由账户名、时间或任何可反查的东西推出来。"""
    code = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    body = re.search(r"function usageInstallId\(\)\s*\{(.*?)\n\}", code, flags=re.S)
    assert body, "未找到 usageInstallId"
    assert "Math.random()" in body.group(1), "安装码必须是随机生成的"
    for token in ("CURRENT_USER", "username", "navigator.userAgent"):
        assert token not in body.group(1), f"安装码不得包含 {token!r}"


# ---------------------------------------------------------------- 诚实性

def test_the_report_never_truncates_silently() -> None:
    """省略与截断都必须在产物正文里写明。

    一份看着干净、实则悄悄少了一半的反馈，比明说「省了 30 条」的有害得多 ——
    读它的人会拿它当全集下判断。
    """
    code = CORE.read_text(encoding="utf-8")
    assert "为了长度这次省略" in code, "查询列表超限时必须在正文明说省略了多少"
    assert "为了能直接粘贴这里截断了" in code, "硬截断必须在正文留痕"


def test_ai_failures_are_never_folded_into_ai_being_off() -> None:
    """反馈包里「AI 真用上了」与「AI 没能完成」必须分开报。

    2026-07-29 修过的病根就是把故障说成「本次未启用」，于是接口坏了好几天也
    没人看得出来。反馈包是同一个陷阱的下一个入口：把两者合成一个数，
    我这边看到的就还是一切正常。
    """
    code = CORE.read_text(encoding="utf-8")
    assert "真的用上了：" in code and "没能完成：" in code, "AI 两档必须分行报告"
    log_code = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    assert 's.status === "used"' in log_code and 's.status === "fallback"' in log_code, (
        "打点必须分别处理 used / fallback"
    )
    assert '"skipped"' not in log_code, (
        "skipped（用户根本没开这一层）不该记 —— 记了就是拿「没开」冲淡「坏了」"
    )


def test_the_ai_label_keys_match_the_real_trace_step_ids() -> None:
    """标签表的键必须逐字等于后端 search_trace 的 step id。

    记录层用 `USAGE_AI_LABELS[s.id]` 当准入判据：键要是对不上，后果不是报错，
    而是**一条 AI 事件都记不进去、还没有任何提示**（同 FRONTEND.md §4.3 那类静默短路）。
    本轮初稿就踩过这个，靠人读代码才发现 —— 所以补一道机械门。
    """
    labels = re.search(r"const USAGE_AI_LABELS = \{([^}]*)\}", CORE.read_text(encoding="utf-8"))
    assert labels, "未找到 USAGE_AI_LABELS"
    keys = set(re.findall(r"(\w+):", labels.group(1)))
    trace_ids = {"local_semantic", "llm_rerank", "llm_polish"}
    assert trace_ids <= keys, f"标签表缺少真实 trace step id：{trace_ids - keys}"

    # 反向：这几个 id 在后端确实是这么拼的（防止哪天后端改名而前端悄悄失灵）
    workflow = (BACKEND / "app" / "workflow.py").read_text(encoding="utf-8")
    for step_id in sorted(trace_ids):
        assert f'"{step_id}"' in workflow, f"后端 workflow.py 里找不到 step id {step_id!r}"


def test_the_feedback_channel_matches_the_build() -> None:
    """单版本化（2026-08-20 tl1）：交付恒为原反馈强化版，回传通道钉死不漂移——

    「生成反馈」聚合文字弹窗在岗则红（已退役）；benchfb「导出反馈包」入口必须健在；
    usage_log.js 里也不得回潮退役弹窗符号。旧的主线版 else 分支已随分叉删除。
    """
    html = INDEX.read_text(encoding="utf-8")
    code = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    for node_id in ("usageReportBtn", "usageModal", "usageText", "usageCopyBtn", "usageClearBtn"):
        assert f'id="{node_id}"' not in html, f"退役的聚合文字反馈弹窗在 index.html 回潮：#{node_id}"
    for token in ("usageModal", "usageText", "usageCopyReport", "usageBuildReport"):
        assert token not in code, f"退役弹窗符号在 usage_log.js 回潮：{token}"
    for node_id in ("benchfbExportBtn", "benchfbModal"):
        assert f'id="{node_id}"' in html, f"强化版缺采集反馈入口：#{node_id}"


def test_turning_it_off_does_not_delete_what_was_already_collected() -> None:
    """关开关只停止继续记，不静默删已有数据；删除必须是用户显式点「清空」。

    截断只发生在**上传成功之后**（见 test_upload_truncates_only_the_sent_snapshot），
    关闭动作本身绝不碰任何已采集数据。
    """
    code = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    body = re.search(r"function usageSetEnabled\(on\)\s*\{(.*?)\n\}", code, flags=re.S)
    assert body, "未找到 usageSetEnabled"
    assert "removeItem(nsKey(LS.usage))" not in body.group(1), (
        "关闭开关不得删除既有记录 —— 静默销毁用户数据同样是不诚实")
    assert "LS.benchfb" not in body.group(1), "关闭开关不得碰 benchfb 记录"


def test_logging_can_never_break_the_actual_product() -> None:
    """写入失败只能安静少记一条，绝不弹错、绝不打断检索。"""
    code = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    body = re.search(r"function usagePersist\(list, scope\)\s*\{(.*?)\n\}", code, flags=re.S)
    assert body, "未找到 usagePersist"
    assert "try {" in body.group(1) and "catch" in body.group(1), "写入必须包在 try 里"
    assert "toast(" not in body.group(1), "写入失败不得打扰用户 —— 埋点不许喧宾夺主"


# ---------------------------------------------------------------- 真行为

def test_usage_core_behavior_spec_passes_under_node() -> None:
    """聚合出来的那段文字对不对，只有真跑一遍才知道（同 memory_rank 的做法）。"""
    assert SPEC.is_file(), f"缺少行为规格文件：{SPEC}"
    node = _resolve_node()
    if not node:
        pytest.skip("未解析到 node.js —— 跳过纯核行为门（full 质量门的 javascript-syntax 环境必有 node）。")
    proc = subprocess.run(
        [node, str(SPEC)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert proc.returncode == 0, f"usage_core 行为规格失败：\n{combined}"
    assert "USAGE_CORE_SPEC_OK" in (proc.stdout or ""), f"未见成功标记：\n{combined}"


def test_telemetry_concurrency_spec_passes_under_node() -> None:
    """真实模块跑 single-flight、跨账户 ACK、per-profile consent、poison 隔离与清空。"""
    assert CONCURRENCY_SPEC.is_file(), f"缺少并发规格文件：{CONCURRENCY_SPEC}"
    node = _resolve_node()
    if not node:
        pytest.skip("未解析到 node.js —— 跳过遥测并发行为门。")
    proc = subprocess.run(
        [node, str(CONCURRENCY_SPEC)], cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", timeout=60,
    )
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert proc.returncode == 0, f"遥测并发行为规格失败：\n{combined}"
    assert "PASS bob queue preserved" in (proc.stdout or ""), f"未见跨账户成功标记：\n{combined}"


def test_the_pure_core_stays_pure() -> None:
    """纯核不得依赖 DOM / localStorage / 墙钟 / 网络，否则 node 就跑不了它。

    上传包构造函数（buildTelemetryPackage）也在纯核里：时间与环境信息由调用方注入，
    脱敏是纯变换，node 规格能逐字段断言。
    """
    code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    for token in ("document.", "localStorage", "window.", "Date.now()", "fetch("):
        assert token not in code, f"usage_core.js 出现 {token!r} —— 纯核必须零依赖"


# ------------------------------------------------- schema v3

def test_schema_v3_impression_label_and_seen_wiring() -> None:
    """ 遥测缺陷修复的结构性钉子（断真代码，全走注释剥离后的源码）：

    - usage_core：USAGE_SCHEMA=3、imp/label 新 kind、曝光「看过」500ms 状态机三件套；
    - usage_log：ImpressionContext 归因三件套 + search 事件 policy_id 优先；
    - usage_upload：MCP 中继改**相对同源**（带 limit/max_bytes/since_ts，不再误取
      ingest 端点 origin）、配额双闸、ack 只前进、consent v2 写 ISO、截断保留标注条目；
    - benchfb：benchfbRateRecord 唯一评分入口 + label 事件；
    - results.js：曝光走状态机 + 历史回看保轮次（keepTurn）。
    """
    core = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    assert "export const USAGE_SCHEMA = 3;" in core, "USAGE_SCHEMA 必须升到 3"
    assert 'imp: "imp"' in core, "USAGE_KINDS 必须登记 imp（展示内容快照）"
    assert 'label: "label"' in core, "USAGE_KINDS 必须登记 label（评分标签）"
    assert "USAGE_SEEN_MIN_MS" in core, "曝光「看过」判据常量必须存在"
    for fn in ("usageSeenCreate", "usageSeenTick", "usageSeenPause"):
        assert f"export function {fn}(" in core, f"曝光状态机缺 {fn}"

    log = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    for fn in ("usageMakeImpression", "usageBindImpression", "usageLogCardAction"):
        assert f"export function {fn}(" in log, f"卡级归因缺 {fn}"
    assert "usagePolicyRef(data" in log, "search 事件必须走统一策略规范化 helper"
    assert "[object Object]" in core and "sorted-key JSON" in CORE.read_text(encoding="utf-8"), (
        "helper 必须显式兜住结构化 policy_id，不能再隐式 String(object)")

    # policy 消费点（必须走统一 helper 规范化）。results.js 不在正钉列：2026-09-14 收编后
    # 它只透传 search.js 已规范化的 imp.policy，自身不再读 data.policy_id（唯一消费点
    # applyRelaxation 随救回预览链退役）；但它仍受负钉约束，不得把结构体压成 [object Object]。
    consumers = {
        "search.js": ROOT / "web/static/js/search/search.js",
        "projects.js": ROOT / "web/static/js/core/projects.js",
        "artifacts.js": ROOT / "web/static/js/core/artifacts.js",
    }
    for name, path in consumers.items():
        source = _strip_js_comments(path.read_text(encoding="utf-8"))
        assert "usagePolicyRef(" in source, f"{name} 漏接统一 policy helper"
    for name, path in {**consumers, "results.js": ROOT / "web/static/js/search/results.js"}.items():
        source = _strip_js_comments(path.read_text(encoding="utf-8"))
        assert "String(data.policy_id" not in source and "String((data && data.policy_id)" not in source, (
            f"{name} 仍会把结构体压成 [object Object]")

    upload = _strip_js_comments(UPLOAD.read_text(encoding="utf-8"))
    assert 'fetch("/api/telemetry/mcp-calls?' in upload, "MCP 中继必须走相对同源 GET"
    assert "limit=100" in upload and "max_bytes=500000" in upload and "since_ts=" in upload, (
        "MCP 中继必须带分页与 consent 时刻下界参数")
    assert "new URL(config.endpoint).origin" not in upload, (
        "不得再从 ingest 端点取 origin（端点指向他机时中继永远 404/跨域）")
    assert "_preserved_idx" in upload, "截断点之外的用户标注条目必须带原名次保留"
    assert "UPLOAD_MIN_INTERVAL_MS" in upload, "常规上传 3 分钟最小间隔闸必须存在"
    assert "KEEPALIVE_MIN_GAP_MS" in upload, "keepalive 30s 最小间隔闸必须存在"
    assert "Math.max(Number(offset)" in upload, "MCP ack 只许前进（与本地 offset 取大）"
    assert "new Date().toISOString()" in upload, "consent v2 同意必须落 ISO 时刻串"

    benchfb = _strip_js_comments(BENCHFB.read_text(encoding="utf-8"))
    assert "export function benchfbRateRecord(" in benchfb, "评分写入必须有统一入口（无论记录在否都发 label）"
    assert "USAGE_KINDS.label" in benchfb, "benchfb 评分必须发 label 事件"

    results = _strip_js_comments((JS_DIR / "search" / "results.js").read_text(encoding="utf-8"))
    assert "usageSeenTick(" in results, "结果页曝光判定必须走 500ms 状态机"
    assert "keepTurn: true" in results, "历史回看/换批重渲必须保住原轮次（keepTurn）"


def test_telemetry_impression_spec_passes_under_node() -> None:
    """卡级归因真行为门：快照不串号、imp 形状、policy_id 优先、label rev/台账兜底。"""
    assert IMPRESSION_SPEC.is_file(), f"缺少卡级归因规格文件：{IMPRESSION_SPEC}"
    node = _resolve_node()
    if not node:
        pytest.skip("未解析到 node.js —— 跳过遥测卡级归因行为门。")
    proc = subprocess.run(
        [node, str(IMPRESSION_SPEC)], cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", timeout=60,
    )
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert proc.returncode == 0, f"遥测卡级归因行为规格失败：\n{combined}"
    assert "TELEMETRY_IMPRESSION_SPEC_OK" in (proc.stdout or ""), f"未见成功标记：\n{combined}"


def test_feedback_core_spec_passes_under_node() -> None:
    """意见反馈核心真行为门——队列（幂等/遮蔽/上限/状态流转/隔离）、
    buildDiagSnapshot（allowlist 聚合 + 遥测关闭语义）、WebCrypto 加解密往返。"""
    assert FEEDBACK_SPEC.is_file(), f"缺少意见反馈规格文件：{FEEDBACK_SPEC}"
    node = _resolve_node()
    if not node:
        pytest.skip("未解析到 node.js —— 跳过意见反馈核心行为门。")
    proc = subprocess.run(
        [node, str(FEEDBACK_SPEC)], cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", timeout=60,
    )
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert proc.returncode == 0, f"意见反馈核心行为规格失败：\n{combined}"
    assert "FEEDBACK_CORE_SPEC_OK" in (proc.stdout or ""), f"未见成功标记：\n{combined}"


def test_feedback_channel_is_isolated_and_independent() -> None:
    """意见反馈通道隔离结构性钉子（断真代码，防回潮）：
    - `feedback_core.js` 是纯逻辑核心：公钥配置点（FEEDBACK_PUBKEY_B64 醒目注释）、
      per-profile 队列、hasSendChannel 门、buildDiagSnapshot；
    - `usage_upload.js` 的 `sendFeedback()` 是独立入口：只发 feedback_pending 已授权记录，
      **绝不捎带** usage/benchfb/mcp 待发队列；复用既有退避/429 升档；
    - 公钥已于 2026-08-22 收口填生产值（65B raw 未压缩点）；清空即回退
      「未配置 = 零出网（UI 复制兜底）」，该兜底由 JS 规格钉住 hasSendChannel("")=false。
    """
    raw_core = FEEDBACK_CORE.read_text(encoding="utf-8")
    assert "FEEDBACK_PUBKEY_B64 —— 开发者公钥配置点" in raw_core, "必须有公钥配置点醒目注释"
    assert "已填生产公钥" in raw_core, "公钥配置点注释必须如实说明生产公钥已填"
    core = _strip_js_comments(raw_core)
    m = re.search(r'export const FEEDBACK_PUBKEY_B64 = "([^"]+)"', core)
    assert m, "生产公钥必须已配置（2026-08-22 收口填入；清空=未配置零出网的兜底由 JS 规格钉住）"
    raw_pub = base64.b64decode(m.group(1))
    assert len(raw_pub) == 65 and raw_pub[0] == 4, (
        '生产公钥必须是 P-256 raw 未压缩点（65B、0x04 前缀，与 importKey("raw") 口径一致）')
    assert "export function hasSendChannel(" in core, "必须暴露发送通道能力判断"
    assert "async function feedbackEncrypt(" in core, "必须暴露加密函数（ECDH+HKDF+AES-256-GCM）"
    assert "export function buildDiagSnapshot(" in core, "必须暴露诊断快照组装"
    assert "export function feedbackPendingForScope(" in core, "必须暴露 per-profile 队列读取"
    assert "feedback_id" in core, "队列条目必须带不可变 feedback_id"
    assert "biodata-feedback-v1" in core and "biodata-feedback/1" in core, "HKDF salt/info 必须与接收端同源"
    # 纯逻辑核心零 DOM/零网络（node 规格可跑）
    for token in ("document.", "fetch(", "XMLHttpRequest", "sendBeacon"):
        assert token not in core, f"feedback_core.js 出现 {token!r} —— 核心必须零 DOM/零网络"

    upload = _strip_js_comments(UPLOAD.read_text(encoding="utf-8"))
    assert "export function sendFeedback(" in upload, "must expose sendFeedback 独立入口"
    assert "feedback_records" in upload, "sendFeedback 组包必须带顶层 feedback_records"
    assert "feedbackEncrypt(" in upload, "sendFeedback 必须逐条加密（不发送明文）"
    assert "feedbackMarkSentForScope(" in upload, "发送成功必须标 sent（保留记录供导出）"
    assert "feedbackHasSendChannel()" in upload, "公钥/WebCrypto 不可用必须不发（复制兜底）"
    assert "_backoffMeta(" in upload, "sendFeedback 必须复用既有退避机制"
    assert "RATE_LIMIT_THRESHOLD" in upload and "RATE_LIMIT_INTERVAL_MS" in upload, "429 升档与常规上传同款"
    # 独立队列绝不捎带：sendFeedback 组包内不得出现 usage/benchfb/mcp 字段
    seg = upload[upload.index("export function sendFeedback("):]
    assert "usage_events" not in seg and "benchfb_records" not in seg and "mcp_records" not in seg, (
        "sendFeedback 包体不得捎带 usage/benchfb/mcp 任何待发队列")
    assert "_fitBody(" not in seg, "sendFeedback 不得复用 usage/benchfb 的组包/装载逻辑"
