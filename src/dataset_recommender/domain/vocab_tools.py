"""目录（catalog）驱动的通用词表机制：对**任意**领域包的 catalog 生效。

这些函数原属 biodata vocabulary 模块、内部直读模块级 CATALOG；现改为读当前
活动领域包的 catalog（`domain.registry.domain_catalog()`），成为骨架机制：
biodata 行为逐位不变，新领域包零代码获得同样能力。函数体与旧实现逐字一致，
唯一差异是词表数据源经注册表间接一层。
"""
from __future__ import annotations

from .registry import domain_catalog


def _is_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def suggestable_terms(dim: str, max_n: int = 8) -> list[dict]:
    """给用户当「点一下就能用」的候选说法。只读、确定性。

    **只收 display 本身也是自己 alias 的条目**。
    像 Eye（别名只有 眼睛/眼部）、Rice（只有 水稻/oryza）、Cancer 这些条目，display 并不是任何 alias，
    照抄给用户会诱导他输入一个必然触发未收录实义词、整句弃权的词——那是把系统的内部规范名当成用户词。

    每项返回 `{alias, display}`：`alias` 是**真的能打进去用**的写法（优先该条目里最长的中文别名，
    没有中文别名时退回 display），`display` 是列表上展示的规范名。
    调用方必须如实说明这是「词表里认识的说法」，不是「库里存在的取值」——能不能搜到要搜了才知道。
    """
    out: list[dict] = []
    for entry in domain_catalog().get(str(dim or "").strip(), []) or []:
        if not isinstance(entry, dict):
            continue
        display = str(entry.get("display") or "").strip()
        if not display:
            continue
        aliases = [str(a).lower().strip() for a in entry.get("aliases", []) if str(a).strip()]
        if display.lower() not in aliases:
            continue
        cjk = sorted((a for a in aliases if _is_cjk(a)), key=len, reverse=True)
        out.append({"alias": cjk[0] if cjk else display, "display": display})
        if len(out) >= max(1, int(max_n)):
            break
    return out


# ---------- 拼音索引（活动领域 catalog 派生视图，2026-09-18 rerank WP5 批） ----------
# rerank 处方层 repair 术式的拼音整词纠错数据源：把每个中文 alias（含 display）
# 转成无声调拼音连写（肺纤维化 → "feixianweihua"），让纯拼音输入的未收录词
# （feixianweihua / fei xian wei hua）能确定性地接地到词表条目。
# 同音碰撞（同一拼音 key 挂多个条目）是中文的自然属性，索引如实保留全部条目，
# 歧义裁决纪律在消费方（agent_exec `_repair_candidates`/`_repair_route`）。

_PINYIN_INDEX: dict[str, list[dict]] | None = None
#: 缓存按 catalog 对象身份键控：换领域包/测试注入新 catalog 时自动重建，不串域。
_PINYIN_INDEX_CATALOG_ID: int | None = None


def reset_vocab_caches() -> None:
    """清空词表派生缓存（测试隔离用；registry.reset_domain_cache 的配套）。"""
    global _PINYIN_INDEX, _PINYIN_INDEX_CATALOG_ID
    _PINYIN_INDEX = None
    _PINYIN_INDEX_CATALOG_ID = None


def pinyin_alias_index() -> dict[str, list[dict]]:
    """中文 alias 的无声调拼音连写索引（活动领域 catalog 派生视图，惰性构建并缓存）。

    key = alias（或 display）逐字拼音（pypinyin NORMAL 式，不带声调）拼接后再归一化
    （NFKC → 小写 → 去空白）；value = [{dim, alias, display, targets, absent}]。
    只收含 CJK 的 alias——英文 alias 本身即拉丁串，进拼音索引会与真实英文查询词混淆。
    同一 (key, entry) 经多个 alias 命中只留第一个 alias；同 key 多 entry 如实并列
    （同音词歧义交给消费方的「等距多候选一票否决」纪律）。
    pypinyin 缺失时返回空索引（拼音纠错通道整体关闭，其余纠错通道不受影响）。
    """
    global _PINYIN_INDEX, _PINYIN_INDEX_CATALOG_ID
    catalog = domain_catalog()
    if _PINYIN_INDEX is not None and _PINYIN_INDEX_CATALOG_ID == id(catalog):
        return _PINYIN_INDEX
    try:
        from pypinyin import Style, pinyin
    except Exception:  # noqa: BLE001（可选依赖缺席：通道关闭，不影响其它路径）
        _PINYIN_INDEX = {}
        _PINYIN_INDEX_CATALOG_ID = id(catalog)
        return _PINYIN_INDEX
    import unicodedata

    def _keys(text: str) -> list[str]:
        norm = "".join(unicodedata.normalize("NFKC", str(text or "")).lower().split())
        if not norm or not _is_cjk(norm):
            return []
        segs = pinyin(norm, style=Style.NORMAL, heteronym=True)
        # 多音字全读法笛卡尔积（『爪蟾』同时索引 zhaochan/zhuachan——社区读法与
        # 字典读法都接地）；组合爆炸闸：>16 种组合只留默认读法。
        keys = [""]
        for alts in segs:
            keys = [k + a for k in keys for a in dict.fromkeys(alts)]
            if len(keys) > 16:
                return ["".join(s[0] for s in segs if s)]
        return keys

    index: dict[str, list[dict]] = {}
    for dim, entries in catalog.items():
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            for alias in [entry.get("display")] + list(entry.get("aliases") or []):
                alias_s = str(alias or "").strip()
                for k in _keys(alias_s):
                    bucket = index.setdefault(k, [])
                    if any(h["display"] == str(entry.get("display") or "") and h["dim"] == dim
                           for h in bucket):
                        continue
                    bucket.append({"dim": dim, "alias": alias_s,
                                   "display": str(entry.get("display") or alias_s),
                                   "targets": [str(t) for t in (entry.get("targets") or [])],
                                   "absent": bool(entry.get("absent"))})
    _PINYIN_INDEX = index
    _PINYIN_INDEX_CATALOG_ID = id(catalog)
    return index
