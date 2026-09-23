# -*- coding: utf-8 -*-
"""由 10x 平台信息补充表（Visium-10x.xlsx / Xenium-10x.xlsx）生成 by-uid 旁挂账本。

背景（使用者反馈）：同学手工把 Visium/Xenium 两平台每个数据集的重要信息
从 10x 官方数据集页刨成两份 Excel。**冻结 base 767 一个字不动**（红线），本脚本把补充信息
生成到 `src/dataset_recommender/data/sample_supplement.by_uid.json`（照
`download_links.by_uid.json` 先例：不放 `database/` 下，避免被语料 loader 误读），
运行时由 `sample_supplement.py` 以 dataset_uid（= 10x url slug）join，只补缺、不覆盖。

零第三方依赖（xlsx = zip + XML，stdlib 解析），可重复执行、输出确定性（键排序）。

用法：
    python scripts/build_sample_supplement.py --visium <Visium-10x.xlsx> --xenium <Xenium-10x.xlsx>
    # 可选：--base database/base/10x-Visium.json --out src/dataset_recommender/data/sample_supplement.by_uid.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

_SSML = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REPO = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- xlsx 读取（stdlib）


def _read_xlsx_rows(path: Path) -> list[dict[str, str]]:
    """读 sheet1，返回 [{列字母: 文本}]（含表头行）。单元格坐标列字母作键，缺格为 ''。"""
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(_SSML + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_SSML + "t")))
        root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows: list[dict[str, str]] = []
    for row in root.iter(_SSML + "row"):
        cells: dict[str, str] = {}
        for c in row.findall(_SSML + "c"):
            ref = c.get("r") or ""
            m = re.match(r"[A-Z]+", ref)
            if not m:
                continue
            col = m.group(0)
            t = c.get("t")
            v = c.find(_SSML + "v")
            isv = c.find(_SSML + "is")
            if isv is not None:
                val = "".join(x.text or "" for x in isv.iter(_SSML + "t"))
            elif v is None or v.text is None:
                val = ""
            elif t == "s":
                val = shared[int(v.text)]
            else:
                val = v.text
            cells[col] = val
        rows.append(cells)
    return rows


def _cell(row: dict[str, str], col: str) -> str:
    return re.sub(r"\s+", " ", (row.get(col) or "").strip())


# ---------------------------------------------------------------- 平台列映射
# Visium: A 名称 B 链接 C 物种 D 组织 E 技术 F 有效Spot/细胞数 G 检测基因数 H 测序深度
#         I 转录本计数(Median UMIs) J 空间分辨率(Bin Metrics) K 发布日期 L 保存方法 M 供体数量
# Xenium: A 名称 B 链接 C 物种 D 组织 E 技术 F 细胞分割模式 G 有效细胞数 H 检测基因数
#         I 高质量转录本中位数 J 转录本密度 K 解码成功转录本总数 L 区域大小 M 单位区域细胞数
#         N 单细胞基因中位数 O 发布日期 P 保存方法 Q 供体数量 R 样本名称 S 样本信息链接
_PLATFORMS = {
    "visium": {"count": "F", "gene": "G",
               "facts": [("测序深度（mean reads）", "H"), ("转录本计数中位数（UMI）", "I"),
                          ("空间分辨率（bin 指标）", "J"), ("供体数量", "M")],
               "seg": None},
    "xenium": {"count": "G", "gene": "H",
               "facts": [("高质量转录本中位数", "I"), ("单细胞基因中位数", "N"), ("供体数量", "Q")],
               "seg": "F"},
}

_NUM_RE = re.compile(r"^([\d,]+(?:\.\d+)?)")


def _leading_number(text: str) -> str:
    """取单元格前导数字（去千分位逗号）；'无'/空/非数字开头 → ''。"""
    m = _NUM_RE.match((text or "").strip())
    return m.group(1).replace(",", "") if m else ""


def _norm_name(text: str) -> str:
    """名称归一（join 兜底键）：去 '| 10x Genomics' 尾巴、小写、非字母数字折叠成单空格。"""
    s = re.sub(r"\|\s*10x\s*genomics\s*$", "", (text or "").strip(), flags=re.IGNORECASE)
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _slug(url: str) -> str:
    m = re.search(r"/datasets/([^/?#]+)", url or "")
    return (m.group(1) if m else "").strip()


# 源表勘误（入库时核实）：Visium-10x.xlsx 中以下两行的 B 列链接互相贴反
# （A 列名称与 F/G 等数值列自洽，仅链接错位）。已逐页抓取 10x 官方页面证实：
#   breast-cancer-tma 页面 = Human Breast Cancer TMA（单乳腺癌，117,730 cells）；
#   human-TA 页面 = 肺/乳腺/结肠三组织阵列（506,400 cells）。
# 证据为抓取的两页 metrics CSV（历史管线产物，不随本仓分发）。
# 未改动用户源表，在此按 slug 对调纠正后再 join；源表若日后修正，本映射成为恒等无害。
_URL_CORRECTIONS = {
    "visium-hd-cytassist-11mm-human-breast-cancer-tma": "visium-hd-cytassist-11mm-human-TA",
    "visium-hd-cytassist-11mm-human-TA": "visium-hd-cytassist-11mm-human-breast-cancer-tma",
}


def _range_or_single(values: list[str], n_sections: int) -> str:
    """逐切片指标：全部相同 → 单值；不同 → 'min ~ max（N 张切片）'（数值比较，展示原串）。"""
    vals = [v for v in values if v]
    if not vals:
        return ""
    uniq = sorted(set(vals))
    if len(uniq) == 1:
        return uniq[0]
    numeric = [(float(_leading_number(v) or "nan"), v) for v in uniq]
    numeric = [p for p in numeric if p[0] == p[0]]  # 丢 NaN
    if len(numeric) < 2:
        return uniq[0]
    numeric.sort()
    return f"{numeric[0][1]} ~ {numeric[-1][1]}（{n_sections} 张切片）"


def build_groups(rows: list[dict[str, str]]) -> list[tuple[str, str, list[dict[str, str]]]]:
    """(数据集名称, 链接或名称键, 行组)。续行（名称/链接为空）归入上一组；全空行丢弃。"""
    groups: list[tuple[str, str, list[dict[str, str]]]] = []
    cur_name = cur_key = ""
    for row in rows[1:]:  # 去表头
        name = _cell(row, "A")
        link = _cell(row, "B")
        if name or link:
            cur_name = name or cur_name
            cur_key = link or name or cur_key
            groups.append((cur_name, cur_key, []))
        if not groups:
            continue
        if any(v.strip() for v in row.values()):
            groups[-1][2].append(row)
    return groups


def aggregate(platform: str, name: str, rows: list[dict[str, str]]) -> dict:
    """把一个数据集的行组（Xenium 多切片 / Visium 多文库）聚合成一条补充记录。"""
    cfg = _PLATFORMS[platform]
    counts = [_leading_number(_cell(r, cfg["count"])) for r in rows]
    counts = [c for c in counts if c]
    genes = [_leading_number(_cell(r, cfg["gene"])) for r in rows]
    genes = [g for g in genes if g]
    n = len([r for r in rows if _cell(r, cfg["count"]) or _cell(r, cfg["gene"])])

    rec: dict[str, object] = {"platform": platform, "name_from_excel": name}
    if counts:
        total = sum(int(float(c)) for c in counts)
        rec["count"] = str(total)
        if n > 1:
            phrase = f"{n} 张切片" if platform == "xenium" else f"{n} 个文库"
            each = "逐切片" if platform == "xenium" else "逐文库"
            rec["count_note"] = f"样本量为 {phrase}的合计（按 10x 官方页面{each}数值相加）"
            rec["n_sections"] = n
    if genes:
        rec["gene_count"] = genes[0]
    if cfg["seg"]:
        seg = next((_cell(r, cfg["seg"]) for r in rows if _cell(r, cfg["seg"])), "")
        if seg:
            rec["seg_mode"] = seg

    facts: list[dict[str, str]] = []
    if rec.get("seg_mode"):
        facts.append({"label": "细胞分割模式", "value": str(rec["seg_mode"])})
    for label, col in cfg["facts"]:
        value = _range_or_single([_cell(r, col) for r in rows], n) if n > 1 else _cell(rows[0], col) if rows else ""
        if value:
            facts.append({"label": label, "value": value})
    if facts:
        rec["extra_facts"] = facts
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 10x 平台信息补充 by-uid 旁挂账本")
    ap.add_argument("--visium", required=True, type=Path)
    ap.add_argument("--xenium", required=True, type=Path)
    ap.add_argument("--base", type=Path, default=_REPO / "database" / "base" / "10x-Visium.json")
    ap.add_argument("--out", type=Path, default=_REPO / "src" / "dataset_recommender" / "data" / "sample_supplement.by_uid.json")
    args = ap.parse_args()

    base_records = json.loads(args.base.read_text(encoding="utf-8"))
    base_by_uid = {r["dataset_uid"]: r for r in base_records}
    name_index: dict[str, list[str]] = {}
    for r in base_records:
        name_index.setdefault(_norm_name(r.get("dataset_name", "")), []).append(r["dataset_uid"])

    out: dict[str, dict] = {}
    stats: dict[str, dict[str, int]] = {}
    unmatched: list[str] = []
    fill_candidates = 0

    for platform, path in (("visium", args.visium), ("xenium", args.xenium)):
        groups = build_groups(_read_xlsx_rows(path))
        st = {"datasets": len(groups), "by_url": 0, "by_name": 0, "unmatched": 0}
        for name, key, rows in groups:
            uid = _URL_CORRECTIONS.get(_slug(key), _slug(key))
            how = "by_url" if uid else ""
            if not uid or uid not in base_by_uid:
                # 兜底：B 列不是链接（个别 Excel 行填的是页面标题）→ 按归一名称唯一匹配
                cands = name_index.get(_norm_name(name), [])
                if len(cands) == 1:
                    uid, how = cands[0], "by_name"
                else:
                    st["unmatched"] += 1
                    unmatched.append(f"{platform}: {name[:70]}")
                    continue
            st[how] += 1
            rec = aggregate(platform, name, rows)
            rec["url"] = base_by_uid[uid].get("url", "")
            rec["dataset_name"] = base_by_uid[uid].get("dataset_name", "")
            rec["source_file"] = path.name
            # 两表交集（post-xenium 交叉比较数据集，两边都收录）：base unit=spots 时以 Visium 表为准，
            # 否则以 Xenium 表为准（cells 口径与 base unit 一致）。默认 xenium 后写覆盖 visium。
            prev = out.get(uid)
            if prev is not None:
                base_unit = str(base_by_uid[uid].get("unit") or "").lower()
                if platform == "visium" and base_unit != "spots":
                    continue  # xenium 版本已在，且单位口径一致，保留
                if platform == "xenium" and base_unit == "spots":
                    continue  # visium 版本已在，spots 口径一致，保留
            out[uid] = rec
            if base_by_uid[uid].get("total_records") in (None, "") and rec.get("count"):
                fill_candidates += 1
        stats[platform] = st

    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {uid: out[uid] for uid in sorted(out)}
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    print(f"base 记录数: {len(base_records)}")
    for platform, st in stats.items():
        print(f"{platform}: Excel 数据集 {st['datasets']}，按 url 匹配 {st['by_url']}，按名称兜底 {st['by_name']}，未匹配 {st['unmatched']}")
    print(f"补充记录总数: {len(payload)}；其中可回填 base 缺失样本量: {fill_candidates}")
    if unmatched:
        print("未匹配（不进账本，仅供核对）:")
        for line in unmatched:
            print("  -", line)
    print(f"已写出: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
