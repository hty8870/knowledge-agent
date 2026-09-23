# -*- coding: utf-8 -*-
"""`corpus._cmp_key` 值键缓存的回归门。

病形：locate_record 三趟全扫都对每条记录现算归一化键（NFC+零宽+casefold），零缓存 →
5712 条最坏情形 5.8ms（退化 7.3×）。修复 = 归一化键按**字符串值** `lru_cache` 记忆化。

缓存引入的新风险必须钉死：
- 归一化语义逐位不变（NFD/零宽/大小写变体仍命中同一条）；
- 语料热重载（记录对象整个换掉）后缓存不得脏——旧 uid 不得再命中、新 uid 必须命中；
- 缓存有界且淘汰后结果仍正确（不可哈希/非 str 的 raw uid 也不炸）。
"""
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.corpus.corpus import _cmp_key, _cmp_key_cached, locate_record  # noqa: E402
from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402


def _rec(uid, name: str, url: str = "", source: str = "10x Genomics") -> DatasetRecord:
    return DatasetRecord(
        dataset_name=name, species="", tissue="", disease="", chemistry="", count="", unit="",
        has_raw_data=None, url=url, source_file="", description="",
        raw={"dataset_uid": uid, "source": source},
    )


# ---------- 缓存下的归一化语义：大小写 / 零宽 / NFD 变体仍命中 ----------

def test_cache_uid_case_variant_still_hits():
    """同一 uid 的大小写变体（不同字符串 → 不同缓存槽）必须命中同一条记录。"""
    target = _rec("GSE-12345", "某数据集")
    rec, amb = locate_record([_rec("other", "别的"), target], uid="gse-12345")
    assert rec is target and amb == []
    rec, amb = locate_record([_rec("other", "别的"), target], uid="  GSE-12345​")
    assert rec is target and amb == []


def test_cache_uid_zero_width_still_hits():
    """尾随/内嵌零宽字符（U+200B、BOM）不得打穿缓存命中。"""
    target = _rec("uid-zw", "某数据集")
    rec, _ = locate_record([target], uid="uid-zw​")
    assert rec is target
    rec, _ = locate_record([target], uid="﻿uid-zw")
    assert rec is target


def test_cache_nfd_name_hits_nfc_record():
    """macOS 粘贴的 NFD 形态查询键 vs 语料 NFC 存储：缓存两侧各算一次，仍同键命中。"""
    nfc_name = "Café 数据"
    nfd_name = unicodedata.normalize("NFD", nfc_name)
    assert nfd_name != nfc_name
    target = _rec("uid-nfd", nfc_name)
    rec, amb = locate_record([target], name=nfd_name)
    assert rec is target and amb == []


# ---------- 语料对象更换后缓存不脏（热重载安全性） ----------

def test_cache_not_dirty_after_corpus_reload():
    """同一会话先后装两份语料（模拟上传触发的热重载）：旧语料的键不得命中新语料，
    新语料的键必须命中——值键缓存的脏读只可能表现为「旧 uid 命中新记录」。"""
    old = _rec("uid-old", "旧数据")
    rec, _ = locate_record([old], uid="uid-old")
    assert rec is old
    # 热重载：对象整个换掉（不复用任何记录对象），uid-old 消失、uid-new 出现。
    new = _rec("uid-new", "新数据")
    rec, amb = locate_record([new], uid="uid-old")
    assert rec is None and amb == []
    rec, _ = locate_record([new], uid="UID-NEW")  # 顺带验大小写
    assert rec is new


def test_cache_same_uid_different_object_returns_current_record():
    """两份语料里 uid 相同（重载后同一数据集的**新**对象）：必须返回当前语料里的那条，
    而不是任何旧对象痕迹——缓存里存的是键不是记录，本条把这个不变量写死。"""
    first = _rec("uid-same", "第一版")
    rec, _ = locate_record([first], uid="uid-same")
    assert rec is first
    second = _rec("uid-same", "第二版")
    rec, _ = locate_record([second], uid="uid-same")
    assert rec is second and rec.dataset_name == "第二版"


# ---------- 缓存本体的不变量 ----------

def test_cmp_key_cache_is_bounded_and_pure():
    """缓存有界（maxsize 生效）且是纯函数：灌入超量唯一键后，老键被逐出也只是重算，
    结果逐位不变。顺手钉死一批对抗输入的归一化输出（语义快照）。"""
    info = _cmp_key_cached.cache_info()
    assert info.maxsize is not None and info.maxsize > 0
    for i in range(info.maxsize + 1024):
        _cmp_key(f"灌库键-{i}")
    info_after = _cmp_key_cached.cache_info()
    assert info_after.currsize <= info_after.maxsize
    # 淘汰后重算结果不变（语义快照，与无缓存旧实现逐位一致）
    assert _cmp_key("  Café​X ") == "caféx"
    assert _cmp_key("Straße") == "strasse"
    assert _cmp_key(None) == ""
    assert _cmp_key(12345) == "12345"  # raw 里非 str 的 uid 不得炸缓存


def test_cmp_key_unhashable_raw_uid_does_not_raise():
    """raw 里 dataset_uid 被恶意/畸形 JSON 写成 list/dict（不可哈希）：
    无缓存旧实现走 str() 能活，带缓存实现也必须有同等韧性。"""
    weird = _rec(["not", "hashable"], "畸形")
    rec, amb = locate_record([weird], uid="['not', 'hashable']")
    assert rec is weird and amb == []
    rec, amb = locate_record([weird], uid="不存在")
    assert rec is None and amb == []
