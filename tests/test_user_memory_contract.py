from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_memory_is_explicit_local_transparent_and_manageable():
    html = _read("web/static/index.html")
    core = _read("web/static/js/core/core.js")
    memory = _read("web/static/js/panel/memory.js")
    search = _read("web/static/js/search/search.js")
    interactions = _read("web/static/js/core/interactions.js")

    for token in (
        # 「记住这次需求」按钮已从结果头移除；记忆能力（记忆管理弹窗 / 研究偏好 / 召回建议）仍在。
        'id="memoryEnabled"', 'id="memoryManageBtn"',
        'id="memoryModal"', 'id="memoryAddForm"', 'id="memoryList"', 'id="memoryClearBtn"',
        "只保存在当前浏览器，不上传", "不会自动发送给检索服务",
    ):
        assert token in html
    assert "biodata_user_memory_v1" in core and "biodata_user_memory_enabled_v1" in core
    assert "localStorage" in memory and "fetch(" not in memory
    assert "rememberLatestSearch" in memory and "constraintSummary" in memory
    assert "upsertMemory" in memory and "memory-delete" in memory
    assert "armTwoStepConfirm" in memory, "记忆删除/清空必须走通用二段确认核"
    # 召回=仅填入查询框、绝不自动提交（永不静默改硬过滤的载体）。
    assert "input.dispatchEvent" in memory and "runRecommend" not in memory
    assert "LAST_RECOMMEND_DATA = data" in search
    # LAST_RECOMMEND_DATA 归 search.js 所有（ESM 可变 let），interactions 的「输入即失效」写必经 setter
    assert "setLastRecommendData(null)" in interactions


def test_memory_recall_is_suggestion_only_and_can_be_disabled_without_deleting():
    memory = _read("web/static/js/panel/memory.js")
    # 召回排序改由纯核 memRankOrder 提供（旧 memoryScore 已移出，见 memory_rank.js）。
    assert "renderMemorySuggestions" in memory and "memRankOrder" in memory
    assert "memoryScore" not in memory and "function memoryTokens" not in memory
    assert 'localStorage.setItem(LS.memoryEnabled, enabled.checked ? "1" : "0")' in memory
    toggle_block = memory.split('enabled.addEventListener("change"', 1)[1].split("});", 1)[0]
    assert "saveUserMemories([])" not in toggle_block
    assert "已有内容仍保留" in memory
    assert "slice(0, 3)" in memory


def test_memory_render_escapes_user_text_and_reports_quota_failure():
    """XSS 回归护栏 + 配额失败反馈。web_smoke 只做静态字符串检查、不执行 JS，
    故这里静态锁两条不变量，防止未来重构误删转义/静默失败后 pytest 仍全绿而问题在浏览器里潜伏。"""
    memory = _read("web/static/js/panel/memory.js")
    # (1) 建议列表与管理列表两处 innerHTML 渲染的用户文本必须经 escapeHtml，且不得裸插。
    assert "escapeHtml(item.text)" in memory and "escapeHtml(item.summary)" in memory
    for ln in memory.splitlines():
        if "innerHTML" not in ln:
            continue
        if "item.text" in ln:
            assert "escapeHtml(item.text)" in ln and "${item.text}" not in ln
        if "item.summary" in ln:
            assert "escapeHtml(item.summary)" in ln and "${item.summary}" not in ln
    # (2) 写入配额满时不再静默失败：saveUserMemories 捕获异常返回布尔，显式保存入口给可见反馈。
    assert "catch (_e) { return false; }" in memory
    assert "浏览器存储空间已满" in memory


def test_ranking_core_is_a_pure_separate_module_wired_into_memory():
    """R：排序/分词/分层/淘汰抽成纯核 memory_rank.js（无 DOM/localStorage/Date、node 可单测）。
    这里静态钉住「纯」与「接线」：纯核不得触碰宿主状态；memory.js 经共享全局调纯核；加载序在 memory.js 前。
    行为正确性由 tests/test_memory_rank_behavior.py 经真实 node 断言（本静态门测不出打分对错）。"""
    rank = _read("web/static/js/panel/memory_rank.js")
    memory = _read("web/static/js/panel/memory.js")
    html = _read("web/static/index.html")

    # 纯度：纯核不得依赖 DOM / localStorage / 墙钟（now 由调用方传入）/ 网络——否则 node 无法离线单测、
    # 且破坏确定性（同一记忆集两台机器名次不同）。墙钟入口封全：Date.now( / new Date / performance.now 皆拦。
    # 这是**字面子串**守卫（连注释也不能出现这些 Latin token）——纯核里请用中文措辞（本地存储/墙钟/网络往返）。
    for forbidden in ("localStorage", "document", "Date.now(", "new Date", "performance.now", "fetch("):
        assert forbidden not in rank, f"纯核 memory_rank.js 不应出现 {forbidden!r}（破坏可测纯度）"
    # 纯核出口（ES Module）：node 行为测试（tests/js/memory_rank_spec.mjs）经 import 拿到 memRank*。
    assert "export function memRankOrder(" in rank and "module.exports" not in rank
    for fn in ("function memRankTokens(", "function memRankCorpusStats(", "function memRankIdf(",
               "function memRankRecency(", "function memRankScore(", "function memRankOrder(",
               "function memRankEvict(", "function memRankNormalize("):
        assert fn in rank, f"纯核缺函数：{fn}"
    # 分词仍走 CJK bi-gram（中文无分隔符时的相关性来源）。
    assert "run.slice(i, i + 2)" in rank and "一-鿿" in rank
    # memory.js 经 import 调纯核（召回排序 + 淘汰 + 去重归一单一真源；纯核是 ES Module）。
    assert "memRankOrder" in memory and "memRankEvict" in memory and "memRankNormalize" in memory
    # 加载序：纯核标签仍在 memory.js 之前（两者都是 ES Module，memory.js 经 importmap #memory_rank import 它）。
    assert "js/panel/memory_rank.js" in html and "js/panel/memory.js" in html
    assert html.index("js/panel/memory_rank.js") < html.index("js/panel/memory.js")


def test_layered_memory_preserves_two_tiers_and_protects_preferences():
    """两层分层（note=研究偏好=持久层；search=已存需求=工作层）+ 偏好防淘汰。
    纯核里偏好不衰减、淘汰只清工作层；memory.js 落盘统一经 memRankEvict 决定淘汰。"""
    rank = _read("web/static/js/panel/memory_rank.js")
    memory = _read("web/static/js/panel/memory.js")
    # 分层判定 + 偏好不衰减（打分用 recency=1）在纯核。
    assert "memRankIsPreference" in rank
    assert 'kind === "note"' in rank
    # 淘汰在落盘处统一决策（不再在读取处盲截断）。
    assert "memRankEvict(items, MEMORY_LIMIT)" in memory
    # 两个层级的用户可见标签仍在（研究偏好 / 已存需求）。
    assert "研究偏好" in memory and "已存需求" in memory
