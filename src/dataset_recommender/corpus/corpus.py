# -*- coding: utf-8 -*-
"""语料装配：基础语料（`database/base/`，10x Genomics）+ 可选外部平台库（`database/external/`）。

数据来源模型（前端「按来源自由勾选」的后端支撑）：
- 每条记录归属一个**来源**：基础语料恒为 `10x Genomics`；外部平台库由其快照里的 `source` 字段声明
  （如 `CELLxGENE Discover`）。来源标签**按装载来路判定**（base 目录 vs 外部目录），不依赖记录内容，
  故基础语料即便偶带 source 键也不会被误判。
- `load_normalized_corpus(sources=...)`：
    * `sources=None` → **只装基础语料**（官方评测 / CLI 默认；确定性与历史逐位一致）。
    * `sources=[...]` → 按所选来源装配（base 仅当选中 `10x Genomics` 才并入；外部按各自 source 过滤）。
- `load_full_corpus()` → base + 全部外部（浏览页「并列展示所有库」用）。

外部库是**静态离线快照**（`scripts/ingest_cellxgene.py` 生成），运行时不联网、不改动 →
归一结果 `lru_cache` 缓存；基础语料按**内容指纹**键控缓存（2026-08-10 P1-6）：目录里任何
文件增/删/改名/就地改写都会改指纹 → 自动重载，「改动即时可见」不再靠每次现算
（实测每次分流双重现算 1548 条、median 89.5ms，命中后只剩亚毫秒级指纹扫描）。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from ..app.runtime_paths import get_app_paths, instance_data_dir_for, uses_split_layout
from .data_loader import load_raw_records
from ..retrieval.normalizer import DatasetRecord, normalize_records
from ..domain.registry import domain_base_source

EXTERNAL_DIR_NAME = "database/external"


def __getattr__(name: str):
    """兼容 shim（PEP 562）：`BASE_SOURCE` 改由活动领域包供给（biodata = "10x Genomics"）。
    模块内部一律调 `_base_source()`（LOAD_GLOBAL 不走本 shim）。"""
    if name == "BASE_SOURCE":
        return _base_source()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _base_source() -> str:
    return domain_base_source()

logger = logging.getLogger(__name__)

# 外部库的内存代际：外部快照/用户上传只在受控写路径后调
# invalidate_external_cache()，因此这个单调递增数可作为 Web 大响应缓存的
# O(1) 失效键。它不是持久化 schema，进程重启后从 0 开始正确重建缓存。
_EXTERNAL_CACHE_GENERATION = 0


def _base_fingerprint(data_dir: Path) -> tuple:
    """基础语料的内容指纹（P1-6 缓存键）：目录下全部 .json 的 (文件名, mtime_ns, size)
    排序元组——与 data_loader 的装载面（`*.json`）严格同形。任何增/删/改名/就地改写都会
    改指纹（mtime 或 size 至少其一变）→ 缓存自动失效。已知边界（2026-08-10 实测）：Windows
    NTFS 惰性时间戳下，**同一 mtime 刻度内的同尺寸改写**会共享指纹（本机紧挨两次写 47/50 次
    mtime_ns 逐位相同）——只有毫秒级连写同名文件才可能踩中；运行期没有写 database/base/ 的通路
    （上传/管护都写 external/），真遇到请调 invalidate_base_cache()。"""
    fp: list[tuple] = []
    for p in Path(data_dir).glob("*.json"):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue  # 扫描间隙蒸发的文件不参与指纹；loader 自有严格/宽容口径
        fp.append((p.name, st.st_mtime_ns, st.st_size))
    return tuple(sorted(fp))


@lru_cache(maxsize=4)
def _base_normalized(data_dir_str: str, fingerprint: tuple) -> "tuple[DatasetRecord, ...]":
    """按（路径 + 内容指纹）键控的基础语料归一缓存（P1-6）。fingerprint 只是键、函数体
    不读它。maxsize=4：指纹每变一次产生一个新键，LRU 自动淘汰旧代际。返回元组（与外部库
    缓存同形），调用方经 _load_base 拿**新建 list**——列表级改写不跨调用污染。"""
    base = normalize_records(load_raw_records(Path(data_dir_str)))
    for r in base:
        if isinstance(r.raw, dict):
            r.raw["source"] = _base_source()
    return tuple(base)


def _resolve_base_dir(data_dir: Path, project_root: Path) -> Path:
    """基础语料目录（冻结基准，只读）：frozen 布局实例根 → shipped_base_dir（随包资源层，
    覆盖调用方从 data_root 派生的 database/base——frozen 下 data_root 里没有 base）；其余
    （source/portable/测试注入根）→ data_dir 原样（历史逐字节一致）。"""
    if uses_split_layout(Path(project_root)):
        return get_app_paths().shipped_base_dir
    return Path(data_dir)


def _external_layers(project_root: Path) -> "tuple[Path, Path]":
    """双层 external 目录 (shipped, user)：
    - 官方快照层 shipped（只读）：frozen 布局实例根 = resource_root/database/external；其余 = 用户层同目录。
    - 用户层 user（写侧唯一目录）：frozen 布局实例根 = data_root/database/external；其余 = project_root/database/external。
    source/portable 与测试注入根：两层同目录 → 合并后每文件只装载一次，行为与历史单一目录逐字节一致。"""
    user = instance_data_dir_for(Path(project_root), EXTERNAL_DIR_NAME)
    if uses_split_layout(Path(project_root)):
        return get_app_paths().shipped_external_dir, user
    return user, user


def _load_base(data_dir: Path) -> list[DatasetRecord]:
    """基础语料 = data_dir（10x）。来源**按装载来路判定**：强制把每条 raw.source 打成
    BASE_SOURCE，覆盖记录内偶带的 source 键（如用户上传了 CELLxGENE/GEO 形状的 JSON 到 database/base/），
    使 source_of / 浏览分面 / 卡片徽章 / available_sources 全部一致（守住 docstring 的不变量）。
    打标在缓存函数内完成（每份缓存内容只打一次），不跨调用污染。"""
    records = list(_base_normalized(str(data_dir), _base_fingerprint(data_dir)))
    if not records:
        _warn_empty_base_once(str(data_dir))
    return records


_EMPTY_BASE_WARNED: set[str] = set()


def _warn_empty_base_once(data_dir: str) -> None:
    """基础语料装载结果为空时 warn-once（模块级集合防刷屏）。

    `_load_base` 被 available_sources / load_normalized_corpus / load_full_corpus 等多处调用，
    同一空目录会被 `_base_normalized` 缓存反复命中并返回空 list——不做去重会把同一条警告刷满
    日志。空语料最常见的成因是 `DATA_DIR`（无前缀通用名，陌生机器上易被其他软件残留）指向了
    错误目录，故消息点名它；也兼顾 `database/base` 本身缺失/为空的可能。
    """
    if data_dir in _EMPTY_BASE_WARNED:
        return
    _EMPTY_BASE_WARNED.add(data_dir)
    logger.warning(
        "基础语料装载返回 0 条记录（%s）。若非预期，请检查是否被环境变量 DATA_DIR 指向了错误目录，"
        "或 database/base 是否缺失/为空。",
        data_dir,
    )


def invalidate_base_cache() -> None:
    """受控失效入口（P1-6）：database/base/ 被非常规手段改写（如手工编辑且保留了 mtime）后调用。"""
    _base_normalized.cache_clear()


#: 外部库归一缓存（代际校验）：key=(shipped,user) → (generation, records)。
#: 不用 lru_cache：慢读取横跨 invalidate 时 lru 会把旧值重新装回已清缓存（陈旧回装竞态，
#: 上传后检索持续不可见直到下次失效/重启）；代际校验下迟到的旧结果直接丢弃不回写。
_EXTERNAL_CACHE: dict = {}


def _load_external_normalized(shipped: Path, user: Path) -> tuple:
    """双层外部库归一（纯装载，不缓存）：官方快照（shipped，只读）+ 用户上传（user，可写）。

    - **同文件名去重：user 层优先**——写侧恒落 user 层、正常不会与 shipped 撞名；手工撞名时
      以 user 层文件为准（shipped 侧按 source_file 名过滤，不重复装载）。
    - source/portable 下两层同目录 → user 文件名集合覆盖 shipped 全部文件 → 每文件只装载
      一次，行为与历史单一目录逐字节一致。
    - 宽容装载（单文件坏不连累整库，`load_raw_records(lenient=True)`）语义保持。"""
    if not shipped.is_dir() and not user.is_dir():
        return tuple()
    user_names = {p.name for p in user.glob("*.json") if p.is_file()} if user.is_dir() else set()
    raw: list = []
    if shipped.is_dir():
        # shipped 层跳过被 user 层同名覆盖的文件（user 优先）
        raw.extend(r for r in load_raw_records(shipped, lenient=True)
                   if r.source_file not in user_names)
    if user.is_dir():
        raw.extend(load_raw_records(user, lenient=True))
    return tuple(normalize_records(raw))


def _external_normalized(shipped_dir_str: str, user_dir_str: str) -> tuple:
    key = (shipped_dir_str, user_dir_str)
    hit = _EXTERNAL_CACHE.get(key)
    if hit is not None and hit[0] == _EXTERNAL_CACHE_GENERATION:
        return hit[1]
    gen_before = _EXTERNAL_CACHE_GENERATION
    value = _load_external_normalized(Path(shipped_dir_str), Path(user_dir_str))
    if gen_before == _EXTERNAL_CACHE_GENERATION:
        # 装载期间未发生失效才回写；否则丢弃（旧值绝不重新入缓存）
        if len(_EXTERNAL_CACHE) >= 2 and key not in _EXTERNAL_CACHE:
            _EXTERNAL_CACHE.clear()
        _EXTERNAL_CACHE[key] = (gen_before, value)
    return value


def _external_normalized_for(project_root: Path) -> tuple[DatasetRecord, ...]:
    """双层 external 归一入口（lru 键 = 两目录字符串；source/portable 下两键相同）。"""
    shipped, user = _external_layers(Path(project_root))
    return _external_normalized(str(shipped), str(user))


def invalidate_external_cache() -> None:
    """外部平台库目录发生变更（如网页端用户上传/手动放入新数据集文件）后调用 → 清空归一缓存，
    令下次 available_sources / load_full_corpus / load_normalized_corpus 重新读盘归一，
    保证「上传即时可见、可检索」。基础语料走 `_base_fingerprint` 键控缓存（文件变动自动失效），
    不在本函数职责内。"""
    global _EXTERNAL_CACHE_GENERATION
    _EXTERNAL_CACHE.clear()
    _EXTERNAL_CACHE_GENERATION += 1


def corpus_cache_generation(data_dir: Path, project_root: Path) -> tuple:
    """返回全库浏览响应的廉价内存缓存代际键。

    base 用既有文件指纹（增/删/改即变）；external 用受控失效入口的代际数。
    仅供同进程缓存判断，不作对外可复现指纹；对外 ETag 仍从真实响应字节计算。

    任务 3：绑定补丁作用域时在键尾追加该账户补丁文件的代际（mtime+size）——
    不同账户 / 同一账户补丁变动都得到不同键，进程内缓存绝不跨账户串视图；
    未绑定时键与历史逐位一致。
    """
    base_dir = _resolve_base_dir(Path(data_dir), Path(project_root))
    shipped, user = _external_layers(Path(project_root))
    key = (
        str(base_dir), _base_fingerprint(base_dir),
        str(shipped), str(user), _EXTERNAL_CACHE_GENERATION,
    )
    from .patch_package import current_patch_scope  # 惰性
    scope = current_patch_scope()
    if scope:
        from .patch_package import patch_generation
        return (*key, "patch", patch_generation(Path(project_root), scope))
    return key


def source_of(record: DatasetRecord) -> str:
    """记录来源标签：基础语料装载时已打成 BASE_SOURCE；外部记录取其声明的 raw.source。"""
    raw = record.raw if isinstance(record.raw, dict) else {}
    return str(raw.get("source") or "").strip() or _base_source()


def load_normalized_corpus(
    data_dir: Path,
    project_root: Path,
    sources: "list[str] | None" = None,
) -> list[DatasetRecord]:
    """按所选来源装配语料。sources=None → 仅基础语料（确定性默认）。

    任务 3（2026-08-26）：绑定补丁作用域（登录账户请求）时，在该账户视图上合并其补丁包
    （blocks 过滤 + adds 追加，adds 按自身 source 参与来源筛选）；未绑定 → 逐字节不变。"""
    base = _load_base(_resolve_base_dir(data_dir, project_root))
    if sources is None:
        return _scoped_patch_apply(base, project_root, include_adds=False)
    wanted = {str(s).strip() for s in sources if str(s).strip()}
    if not wanted:
        # 空选择 → 保守回退基础语料
        return _scoped_patch_apply(base, project_root, include_adds=False)
    out: list[DatasetRecord] = []
    if _base_source() in wanted:
        out.extend(base)
    if wanted - {_base_source()}:  # 有外部来源被选中才装载外部库
        ext = _external_normalized_for(project_root)
        out.extend(r for r in ext if source_of(r) in wanted)
    return _scoped_patch_apply(out, project_root, include_adds=True, source_filter=wanted)


def load_full_corpus(data_dir: Path, project_root: Path) -> list[DatasetRecord]:
    """基础语料 + 全部外部平台库（浏览页并列展示所有库；W1 起外部为 shipped+user 双层合并）。

    绑定补丁作用域时合并该账户补丁（blocks + 全部 adds）；未绑定 → 逐字节不变。"""
    base = _load_base(_resolve_base_dir(data_dir, project_root))
    ext = list(_external_normalized_for(project_root))
    return _scoped_patch_apply(base + ext, project_root, include_adds=True)


def _scoped_patch_apply(
    records: list[DatasetRecord],
    project_root: Path,
    *,
    include_adds: bool,
    source_filter: "set[str] | None" = None,
) -> list[DatasetRecord]:
    """绑定补丁作用域（patch_package 的 contextvar）时应用该账户补丁；未绑定 → 原样返回。

    惰性 import patch_package：冻结评测 / CLI / MCP 路径永不触发绑定，import 图与运行时
    行为均与历史逐位一致（本函数在缺省下只剩一次 contextvar 读取）。补丁文件损坏 →
    PatchError 向上抛（接口层翻 4xx/5xx 人读文案），绝不静默退回「无补丁视图」骗人。"""
    from .patch_package import current_patch_scope  # 惰性：读路径零新顶层边

    scope = current_patch_scope()
    if not scope:
        return records
    from .patch_package import apply_patch, load_patch

    patch = load_patch(Path(project_root), scope)
    return apply_patch(records, patch, include_adds=include_adds, source_filter=source_filter)


def raw_data_false_is_guess(item: dict) -> bool:
    """这条记录的 has_raw_data=False 是不是**猜的**（来源从未标注，是抓取时的保守占位）。

    显示层纪律：猜的 False 不得显示成「无 FASTQ」——那是把
    「我们没查」编码成「它没有」。目前唯一的猜测档来源是 ArrayExpress（ingest 自陈
    「保守置 False：不逐条核实」，见 corpus_curation.AE_SOURCE_LABEL 与
    provenance.SOURCE_ARRAYEXPRESS 两处既有常量）；但逐条核验过的 AE 记录
    （metadata_provenance.complete 且 fields.has_raw_data.complete）不算猜。
    其余来源的 False 均为有据档（10x 台账实测 / CELLxGENE·SCEA 库级事实 / HCA 清单核验），维持原显示。
    放在本模块而非 provenance：workflow（冻结 767 评测路径）不得 import 稿件产物模块
    （test_provenance.py 的闭包隔离钉）。"""
    if item.get("has_raw_data") is not False:
        return False
    # 裸字面量是有意的：本模块在冻结评测路径（retriever）的 import 闭包内，不得 import
    # 稿件产物模块 provenance（test_provenance.py 闭包隔离钉）；label 真源引用仅在
    # corpus_curation / CHECK_UPDATE_SOURCES 一侧成立。
    if str(item.get("source") or "").strip() != "ArrayExpress":
        return False
    mp = item.get("metadata_provenance")
    if isinstance(mp, dict) and mp.get("complete") is True:
        fields = mp.get("fields")
        if isinstance(fields, dict):
            hrd = fields.get("has_raw_data")
            if isinstance(hrd, dict) and hrd.get("complete") is True:
                return False
    return True


def locate_record(
    records: "list[DatasetRecord]",
    *,
    uid: str = "",
    url: str = "",
    name: str = "",
    source: str = "",
) -> "tuple[DatasetRecord | None, list[dict]]":
    """按严格优先级定位**单条**记录：uid 精确 > url 精确 > name 精确（source 消歧）。

    返回 ``(record, candidates)``：
    - ``record`` 非 None → 唯一命中（``candidates`` 恒空）。
    - ``record`` 为 None 且 ``candidates`` 非空 → 命中多条、source 消歧后仍歧义：
      candidates 是各候选的 {dataset_uid, dataset_name, source, url} 投影，供调用方**如实报歧义**
      （HTTP 409 / MCP ambiguous_name），绝不静默任取第一条。
    - 全空 → 查不到（HTTP 404 / MCP not_found）。

    每一档键都**全扫一遍语料**再退化到下一档。旧实现是线性扫描「uid/url/name 任一命中即早退」：
    语料里存在同名两条（如 …-ff-ultima 与 …-ff-ultima-4）时，带全参的请求扫到靠前那条就被
    name 截胡，根本走不到 uid 精确命中——介绍/FAIR 由此张冠李戴（2026-08-04 普查 P0-1）。
    Web（/api/introduction、/api/fair）与 MCP（get_dataset_introduction、assess_dataset_fair）
    共用本函数作唯一定位真源，防止「同一 bug Web 修好 MCP 依旧」。

    比较键统一做 NFC 正规化 + 去零宽字符 + casefold（**只动比较键**，展示/返回一律用原文）：
    macOS 粘贴的 NFD 形态、尾随零宽空格、大小写差异不该让「视觉相同」的键漏配。
    uid 撞库（用户上传/外部快照注入的重复 uid）与 name 同口径报歧义，不静默取第一。
    """
    uid, url, name, source = (_cmp_key(v) for v in (uid, url, name, source))
    if uid:
        matches = [r for r in records
                   if _cmp_key((r.raw if isinstance(r.raw, dict) else {}).get("dataset_uid")) == uid]
        # 单条命中 = uid 最强键直达（旧口径不变）；撞库多条才走与 name 同口径的消歧/报歧义。
        if len(matches) == 1:
            return matches[0], []
        if len(matches) > 1:
            return _disambiguate(matches, source)
    if url:
        for record in records:
            # 热循环直取 `_cmp_key_cached`：dataclass 契约下 record.url / dataset_name 恒为 str，
            # 省掉 `_cmp_key` 包装帧与 str() 各一次（5712 条 ×3 趟 ≈ 省 0.3ms，见 _cmp_key_cached 注）。
            if _cmp_key_cached(record.url) == url:
                return record, []
    if name:
        matches = [r for r in records if _cmp_key_cached(r.dataset_name) == name]
        if matches:
            return _disambiguate(matches, source)
    return None, []


#: 零宽字符（U+200B/C/D 与 BOM）：视觉不可见，但逐字判等会被它打穿。
_ZERO_WIDTH_RE = re.compile(r"[​‌‍﻿]")


def _cmp_key(value) -> str:
    """定位比较键：NFC + 去零宽 + 去首尾空白 + casefold。只用于判等，不动展示原文。"""
    # 先归一成 str 再进缓存：raw 里 dataset_uid 可能是 int/None，lru_cache 要求入参可哈希，
    # 且 str(5) 与 5 哈希不同——统一成 str 才能保证同值同槽位。
    return _cmp_key_cached(str(value or ""))


@lru_cache(maxsize=32768)
def _cmp_key_cached(text: str) -> str:
    """`_cmp_key` 的带缓存主体。按**字符串值**缓存（而非按记录对象/列表）：
    - 纯函数 → 缓存对语义透明；语料热重载后，变了的字段得到的是不同字符串，自然算新键，
      旧条目只随 LRU 淘汰，不存在脏读，无需失效钩子。
    - Web 热路径每请求重建 records（基础语料每次现算），对象身份缓存永远打不中；
      值键缓存在重建后仍全命中（5712 条 ×3 趟的最坏情形 ~1ms，2026-08-04 R2-8 P1-1）。
    maxsize 覆盖 ~2× 全语料键量（uid/url/name/source 各一）；超出即逐出重算，优雅退化。"""
    return _ZERO_WIDTH_RE.sub("", unicodedata.normalize("NFC", text)).strip().casefold()


def _disambiguate(
    matches: "list[DatasetRecord]", source: str
) -> "tuple[DatasetRecord | None, list[dict]]":
    """多条命中的统一口径：source 能消歧就消；消歧后仍多条 → 如实报候选；滤空 → 查不到。"""
    if source:
        scoped = [r for r in matches if _cmp_key(source_of(r)) == source]
        if scoped:
            matches = scoped
        else:
            # source 把命中滤空 → 该 name/uid+source 组合不存在：按查不到处理（与旧口径一致），
            # 不静默忽略调用方给的 source 去返回别的来源的记录。
            return None, []
    if len(matches) == 1:
        return matches[0], []
    return None, [
        {
            "dataset_uid": str((r.raw if isinstance(r.raw, dict) else {}).get("dataset_uid") or ""),
            "dataset_name": r.dataset_name,
            "source": source_of(r),
            "url": r.url,
        }
        for r in matches
    ]


def known_source_values(data_dir: Path, project_root: Path) -> list[str]:
    """所有**合法**来源名（顺序同 available_sources：10x 置顶）。
    供接口层（MCP/API）校验 `sources` 入参：请求了不在此集合里的来源 = 写错来源名，
    应显式报错（bad_source），而不是像 load_normalized_corpus 那样静默过滤成空结果
    （0 命中与来源名写错无法区分）。"""
    return [x["value"] for x in available_sources(data_dir, project_root)]


def available_sources(data_dir: Path, project_root: Path) -> list[dict]:
    """列出所有可选来源 + 计数（10x Genomics 恒在且置顶，其余外部库按数量降序）。
    计数口径与 /api/datasets 分面一致：均按 source_of（基础语料已打标 BASE_SOURCE）。
    W1 起外部计数基于 shipped+user 双层合并（source/portable 下与历史逐字节一致）。
    任务 3：绑定补丁作用域时按该账户视图计数（blocks 扣减、adds 按其 source 计入）。"""
    base = _load_base(_resolve_base_dir(data_dir, project_root))
    ext = _external_normalized_for(project_root)
    records = _scoped_patch_apply(list(base) + list(ext), project_root, include_adds=True)
    counts: dict[str, int] = {_base_source(): 0}
    for r in records:
        s = source_of(r)
        counts[s] = counts.get(s, 0) + 1
    ordered = [_base_source()] + sorted(
        (k for k in counts if k != _base_source()), key=lambda k: -counts[k]
    )
    return [{"value": k, "count": counts[k]} for k in ordered]


#: 参与「内容指纹」的字段。改这个元组就是改对外语义（同一份目录会算出不同指纹），
#: 必须连同用到它的产物一起评估，不能顺手加字段。
_CONTENT_FIELDS = (
    "dataset_name", "species", "tissue", "disease", "chemistry", "platform",
    "count", "unit", "has_raw_data", "url", "download_url", "filesize",
    "published_date", "collection_doi",
)


def corpus_snapshot(records, *, with_content: bool = False) -> dict:
    """确定性**语料快照描述**（可复现锚点，N9）：内容指纹 + 条数 + 来源分布。

    `snapshot_id` = 排序后全部 `dataset_uid` 的 SHA-256 前 12 位——**同一语料 → 同一 id**，
    换库 / 增删 / 上传即变。**不含日期**（日期是「检索时刻」，由调用方在检索时戳，不属于语料本身）。
    用途：让一次检索/复用清单可标注「基于哪份语料」，重跑得到不同结果时可用 id 对比（配合
    `scripts/diff_snapshots.py`）。纯只读、确定性，不被检索/评测路径调用。

    `with_content=True` 时**追加** `content_digest`：覆盖每条数据集的字段取值，而不只是编号集合。
    为什么需要第二个指纹：来源补录字段（比如给一批数据集填上组织）**不会改变编号集合**，
    `snapshot_id` 逐位不变，但产物里的叙述已经和当时不一样了。只靠编号指纹的一致性检查
    在这种「就地改字段」的更新面前是完全空转的。默认 False → 返回值逐位不变，既有调用方零影响。
    """
    import hashlib
    import json as _json

    uids: list[str] = []
    sources: dict[str, int] = {}
    content_lines: list[str] = []
    for record in records:
        raw = getattr(record, "raw", None)
        if not isinstance(raw, dict):
            raw = record if isinstance(record, dict) else {}
        uid = str(raw.get("dataset_uid") or "").strip()
        if uid:
            uids.append(uid)
        src = str(raw.get("source") or "").strip() or _base_source()
        sources[src] = sources.get(src, 0) + 1
        if with_content and uid:
            payload = {key: raw.get(key) for key in _CONTENT_FIELDS}
            blob = _json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            content_lines.append(uid + "\t" + hashlib.sha256(blob.encode("utf-8")).hexdigest())
    digest = hashlib.sha256("\n".join(sorted(uids)).encode("utf-8")).hexdigest()[:12]
    ordered = dict(sorted(sources.items(), key=lambda kv: (-kv[1], kv[0])))
    out = {"snapshot_id": digest, "n_records": len(uids), "sources": ordered}
    if with_content:
        out["content_digest"] = hashlib.sha256(
            "\n".join(sorted(content_lines)).encode("utf-8")
        ).hexdigest()[:16]
    return out
